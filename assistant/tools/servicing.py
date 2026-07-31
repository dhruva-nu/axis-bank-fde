"""Authentication, service requests, complaints, escalation and goodwill actions."""

from __future__ import annotations

from .. import config
from ..bank import BANK, days_from_now, inr, not_found, today, working_days_from_now


# --------------------------------------------------------------------------- #
# OTP
# --------------------------------------------------------------------------- #


def send_otp(purpose: str) -> dict:
    """Send a 6-digit OTP to the customer's registered mobile before a sensitive action. purpose is a short description shown to the customer, e.g. 'card limit change'."""
    code = BANK.issue_otp(purpose)
    BANK.log("otp_sent", purpose, otp=code)
    return {
        "ok": True,
        "sent_to": BANK.customer["mobile"],
        "purpose": purpose,
        "valid_for": "5 minutes",
        "attempts_allowed": 3,
        "instruction": "Ask the customer to read out the 6-digit code, then call verify_otp.",
        "_demo_note": f"DEMO ONLY — the OTP is {code}. Never state this in the chat; the customer 'receives' it on their phone.",
    }


def verify_otp(code: str) -> dict:
    """Verify the 6-digit OTP the customer read out. Must succeed before any sensitive action goes through."""
    ok, message = BANK.check_otp(code)
    BANK.log("otp_verified" if ok else "otp_failed", message)
    return {"ok": ok, "verified": ok, "message": message}


# --------------------------------------------------------------------------- #
# Service requests and complaints
# --------------------------------------------------------------------------- #


def list_complaint_categories() -> dict:
    """List the valid complaint/service-request categories with their turnaround times."""
    return {"ok": True, "categories": config.knowledge()["complaint_categories"]}


def raise_service_request(category: str, description: str) -> dict:
    """Log a service request or complaint and return the SR number and committed resolution date. Use a category from list_complaint_categories."""
    cats = config.knowledge()["complaint_categories"]
    matched = next((k for k in cats if k.lower() == (category or "").strip().lower()), None)
    if not matched:
        matched = next(
            (k for k in cats if any(w in k.lower() for w in (category or "").lower().split() if len(w) > 3)),
            "Other",
        )
    meta = cats[matched]

    sr = {
        "sr_no": BANK.ref("SR"),
        "category": matched,
        "description": description,
        "raised": today().isoformat(),
        "status": "Open",
        "priority": meta["priority"],
        "tat_date": working_days_from_now(meta["tat_days"]),
        "channel": "Assistant",
    }
    BANK.customer["service_requests"].insert(0, sr)
    BANK.log("sr_raised", f"{matched}: {description}", ref=sr["sr_no"])
    return {
        "ok": True,
        "sr_number": sr["sr_no"],
        "category": matched,
        "priority": meta["priority"],
        "status": "Open",
        "resolution_by": sr["tat_date"],
        "tat": f"{meta['tat_days']} working days",
        "updates": "SMS and email updates go to the registered contact at every status change.",
        "escalation": "If it is not resolved by the committed date, escalate_to_human moves it to the Nodal Officer.",
    }


def check_service_request(sr_number: str) -> dict:
    """Check the status of a service request or complaint. Pass an empty string to list all of them."""
    srs = BANK.customer["service_requests"]
    if not sr_number:
        return {"ok": True, "count": len(srs), "service_requests": srs}
    sr = next((s for s in srs if s["sr_no"].lower() == sr_number.strip().lower()), None)
    if not sr:
        return not_found("service request", sr_number)
    return {"ok": True, **sr}


def escalate_to_human(reason: str, conversation_summary: str) -> dict:
    """Hand the customer to a human agent. Use when they ask for a person, for disputes over ₹50,000, suspected account takeover, legal/deceased/regulatory matters, or after two failed attempts at the same request."""
    ref = BANK.ref("ESC")
    urgent = any(
        w in f"{reason} {conversation_summary}".lower()
        for w in ("fraud", "takeover", "unauthorised", "unauthorized", "legal", "deceased", "ombudsman")
    )
    BANK.log("escalated", f"{reason}", ref=ref)
    return {
        "ok": True,
        "case_id": ref,
        "reason": reason,
        "queue": "Priority fraud desk" if urgent else f"{BANK.customer['segment']} service desk",
        "callback_window": "Within 2 hours" if urgent else "Within 4 working hours",
        "callback_number": BANK.customer["mobile"],
        "context_transferred": conversation_summary,
        "note": "The agent picks up with the full transcript — the customer will not have to repeat anything.",
    }


def request_callback(reason: str, preferred_time: str) -> dict:
    """Book a callback from the service team at a time the customer chooses."""
    ref = BANK.ref("CB")
    BANK.log("callback_booked", f"{reason} at {preferred_time}", ref=ref)
    return {
        "ok": True,
        "reference": ref,
        "reason": reason,
        "scheduled_for": preferred_time,
        "number": BANK.customer["mobile"],
        "note": "The call comes from 1860-419-5555. The bank never asks for OTP, CVV or PIN on a call.",
    }


def book_appointment(purpose: str, branch: str, preferred_date: str) -> dict:
    """Book a branch or relationship-manager appointment (lockers, large cash, wealth advice, documentation)."""
    branches = [b["name"] for b in config.knowledge()["branches"]]
    chosen = next((b for b in branches if (branch or "").lower() in b.lower()), BANK.customer["home_branch"].split(",")[0])
    ref = BANK.ref("APT")
    BANK.log("appointment_booked", f"{purpose} at {chosen} on {preferred_date}", ref=ref)
    return {
        "ok": True,
        "reference": ref,
        "purpose": purpose,
        "branch": chosen,
        "date": preferred_date or days_from_now(2),
        "duration": "30 minutes",
        "carry": "PAN or Aadhaar for in-branch identification.",
        "confirmation": "SMS and calendar invite sent to the registered contact.",
    }


def waive_charge(transaction_ref: str, reason: str) -> dict:
    """Waive a fee or charge as a goodwill gesture (AMB non-maintenance, EMI bounce, SMS alert, card replacement). Use where the customer is otherwise in good standing."""
    t = BANK.transaction(transaction_ref)
    amount = t["amount"] if t else 0.0
    label = t["description"] if t else transaction_ref

    if t:
        a = BANK.account(t["account"])
        if a:
            a["balance"] += amount
            a["available_balance"] = a["balance"]
            BANK.customer["transactions"].insert(
                0,
                {
                    "id": BANK.ref("REV"),
                    "date": today().isoformat(),
                    "account": a["account_no"],
                    "description": f"REVERSAL/GOODWILL WAIVER/{label[:30]}",
                    "channel": "Adjustment",
                    "type": "credit",
                    "amount": amount,
                    "balance_after": a["balance"],
                    "status": "Success",
                },
            )

    ref = BANK.ref("WVR")
    BANK.log("charge_waived", f"{label} {inr(amount)} — {reason}", ref=ref)
    return {
        "ok": True,
        "reference": ref,
        "charge": label,
        "amount_waived": inr(amount),
        "reason": reason,
        "credited": "Immediately" if t else "Within 2 working days",
        "policy": "One goodwill waiver per financial year per charge type, at the bank's discretion.",
        "note": "Tell the customer this is a one-time goodwill waiver so the expectation is set.",
    }


# --------------------------------------------------------------------------- #
# KYC
# --------------------------------------------------------------------------- #


def get_kyc_status() -> dict:
    """Check the customer's KYC status, expiry and whether re-KYC is due."""
    c = BANK.customer
    due = c["kyc_status"] != "Verified"
    return {
        "ok": True,
        "status": c["kyc_status"],
        "valid_till": c["kyc_expiry"],
        "re_kyc_due": due,
        "impact_if_not_done": (
            "Debits on the account are restricted after the due date; credits continue." if due else "None"
        ),
        "how_to_complete": "Video KYC on the mobile app in about 10 minutes, or any branch with PAN and Aadhaar.",
    }


def start_video_kyc() -> dict:
    """Send the customer a secure Video KYC link to complete re-KYC without visiting a branch."""
    ref = BANK.ref("VKYC")
    BANK.log("vkyc_link_sent", BANK.customer["mobile"], ref=ref)
    return {
        "ok": True,
        "reference": ref,
        "link_sent_to": BANK.customer["mobile"],
        "valid_for": "24 hours",
        "keep_ready": ["Original PAN card", "Aadhaar", "A blank sheet and pen for the signature capture"],
        "duration": "About 10 minutes",
        "availability": "9:00 AM to 7:00 PM, Monday to Saturday",
        "activation": "KYC updates within 2 working hours of a successful call.",
    }
