"""Disputes, fraud reporting and failed-transaction reversals.

This is the highest-emotion part of bank support. Every tool here returns a
reference number and a concrete date, because that is what the customer actually
wants to hear.
"""

from __future__ import annotations

from ..bank import BANK, days_from_now, inr, mask_card, not_found, today, working_days_from_now


def raise_dispute(txn_ref: str, reason: str, description: str) -> dict:
    """Raise a dispute / chargeback on a transaction. reason examples: 'not received', 'duplicate charge', 'wrong amount', 'cancelled order', 'unauthorised'."""
    t = BANK.transaction(txn_ref)
    if not t:
        return not_found("transaction", txn_ref)
    if any(d["transaction_id"] == t["id"] for d in BANK.customer["disputes"]):
        existing = next(d for d in BANK.customer["disputes"] if d["transaction_id"] == t["id"])
        return {"ok": True, "already_raised": True, **existing}

    intl = "INTL" in t["channel"].upper()
    provisional = t["amount"] >= 5000
    dispute = {
        "dispute_id": BANK.ref("DSP"),
        "transaction_id": t["id"],
        "description": t["description"],
        "amount": t["amount"],
        "reason": reason,
        "customer_note": description,
        "raised_on": today().isoformat(),
        "status": "Under investigation",
        "provisional_credit": provisional,
        "provisional_credit_by": working_days_from_now(7) if provisional else None,
        "resolution_by": days_from_now(90 if intl else 45),
        "network": "International network rules" if intl else "Domestic",
    }
    BANK.customer["disputes"].append(dispute)
    BANK.log("dispute_raised", f"{t['description']} {inr(t['amount'])} — {reason}", ref=dispute["dispute_id"])
    return {
        "ok": True,
        "dispute_id": dispute["dispute_id"],
        "transaction": t["description"],
        "amount": inr(t["amount"]),
        "reason": reason,
        "status": "Under investigation",
        "provisional_credit": (
            f"Yes — {inr(t['amount'])} will be credited by {dispute['provisional_credit_by']}"
            if provisional
            else "Not applicable below ₹5,000"
        ),
        "final_resolution_by": dispute["resolution_by"],
        "documents_needed": (
            "Invoice and proof you contacted the merchant, if this is a goods/services dispute. "
            "Nothing needed for an unauthorised transaction."
        ),
        "track_with": "check_dispute_status",
    }


def report_unauthorized_transaction(txn_ref: str, description: str) -> dict:
    """Report a transaction the customer did not make. Immediately hotlists the card, raises a high-priority fraud dispute and files a fraud case. No OTP needed — speed protects the customer."""
    t = BANK.transaction(txn_ref)
    if not t:
        return not_found("transaction", txn_ref)

    # Hotlist the card the transaction was made on, if we can identify it.
    blocked = None
    card = next((c for c in BANK.customer["cards"] if c["card_id"] == t["account"]), None)
    if card is None and t["channel"] in ("POS", "ECOM", "ECOM-INTL", "ATM"):
        card = next((c for c in BANK.customer["cards"] if c["linked_account"] == t["account"]), None)
    if card and card["status"] != "Hotlisted":
        card["status"] = "Hotlisted"
        blocked = card

    case = {
        "dispute_id": BANK.ref("FRD"),
        "transaction_id": t["id"],
        "description": t["description"],
        "amount": t["amount"],
        "reason": "Unauthorised transaction",
        "customer_note": description,
        "raised_on": today().isoformat(),
        "status": "Fraud investigation - High priority",
        "provisional_credit": True,
        "provisional_credit_by": working_days_from_now(7),
        "resolution_by": days_from_now(90 if "INTL" in t["channel"].upper() else 45),
        "network": "Fraud desk",
    }
    BANK.customer["disputes"].append(case)
    BANK.log(
        "fraud_reported",
        f"{t['description']} {inr(t['amount'])}" + (f", hotlisted {mask_card(blocked['last4'])}" if blocked else ""),
        ref=case["dispute_id"],
    )

    out = {
        "ok": True,
        "case_id": case["dispute_id"],
        "transaction": t["description"],
        "amount": inr(t["amount"]),
        "date": t["date"],
        "status": "Fraud investigation opened — high priority",
        "provisional_credit_by": case["provisional_credit_by"],
        "final_resolution_by": case["resolution_by"],
        "liability": (
            "Reported within 3 working days of the transaction, the customer's liability is zero "
            "under RBI's limited-liability framework."
        ),
        "next_steps": [
            "The fraud desk will call on the registered mobile within 24 hours.",
            "Do not share the OTP or CVV with anyone, including anyone claiming to be from the bank.",
            "File a cybercrime complaint at cybercrime.gov.in or 1930 if the customer wants to — it strengthens the case.",
        ],
    }
    if blocked:
        out["card_blocked"] = {
            "card": mask_card(blocked["last4"]),
            "product": blocked["product"],
            "status": "Hotlisted immediately",
            "replacement": "Issued free of charge",
            "expected_delivery": working_days_from_now(7),
        }
    return out


def report_failed_transaction(txn_ref: str) -> dict:
    """Log a failed transaction where money was debited but not credited, and give the customer the RBI-mandated reversal date."""
    t = BANK.transaction(txn_ref)
    if not t:
        return not_found("transaction", txn_ref)
    if "fail" not in t["status"].lower() and "pending" not in t["status"].lower():
        return {
            "ok": False,
            "error": "not_failed",
            "message": f"'{t['description']}' shows as {t['status']} — it did not fail.",
        }

    fast = t["channel"] in ("UPI", "IMPS")
    ref = BANK.ref("SR")
    sr = {
        "sr_no": ref,
        "category": "Account - failed transaction not reversed",
        "description": f"{t['description']} {inr(t['amount'])} on {t['date']}",
        "raised": today().isoformat(),
        "status": "Open - tracking auto-reversal",
        "tat_date": days_from_now(1 if fast else 5),
        "channel": "Assistant",
    }
    BANK.customer["service_requests"].insert(0, sr)
    BANK.log("failed_txn_logged", f"{t['description']} {inr(t['amount'])}", ref=ref)
    return {
        "ok": True,
        "reference": ref,
        "transaction": t["description"],
        "amount": inr(t["amount"]),
        "date": t["date"],
        "rrn": t.get("rrn"),
        "auto_reversal_by": sr["tat_date"],
        "rule": (
            "RBI Harmonisation of TAT: UPI/IMPS reverse by T+1, ATM and card failures by T+5."
        ),
        "compensation": "If the reversal is late, ₹100 per day of delay is credited automatically — no claim needed.",
        "action_needed_from_customer": "None. This tracks itself; the SR number is for reference.",
    }


def check_dispute_status(dispute_id: str) -> dict:
    """Check the status of a dispute or fraud case. Pass an empty string to list all of the customer's open cases."""
    disputes = BANK.customer["disputes"]
    if not disputes:
        return {"ok": True, "count": 0, "message": "No disputes on this customer's record."}
    if not dispute_id:
        return {
            "ok": True,
            "count": len(disputes),
            "disputes": [
                {
                    "dispute_id": d["dispute_id"],
                    "transaction": d["description"],
                    "amount": inr(d["amount"]),
                    "status": d["status"],
                    "resolution_by": d["resolution_by"],
                }
                for d in disputes
            ],
        }
    d = next((x for x in disputes if x["dispute_id"].lower() == dispute_id.strip().lower()), None)
    if not d:
        return not_found("dispute", dispute_id)
    return {"ok": True, **{**d, "amount": inr(d["amount"])}}


def withdraw_dispute(dispute_id: str, reason: str) -> dict:
    """Withdraw a dispute the customer no longer wants to pursue (e.g. they recognised the charge)."""
    d = next((x for x in BANK.customer["disputes"] if x["dispute_id"].lower() == (dispute_id or "").lower()), None)
    if not d:
        return not_found("dispute", dispute_id)
    d["status"] = "Withdrawn by customer"
    BANK.log("dispute_withdrawn", f"{d['dispute_id']}: {reason}")
    return {
        "ok": True,
        "dispute_id": d["dispute_id"],
        "status": "Withdrawn",
        "reason": reason,
        "note": "Any provisional credit already given will be reversed on the next statement.",
    }
