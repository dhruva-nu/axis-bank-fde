"""Debit and credit card servicing."""

from __future__ import annotations

from ..bank import (
    BANK,
    days_from_now,
    inr,
    mask_account,
    mask_card,
    need_otp,
    not_found,
    today,
    working_days_from_now,
)

_FRAUD_REASONS = {"lost", "stolen", "fraud", "unauthorised", "unauthorized", "misuse", "compromised"}


def list_cards() -> dict:
    """List all of the customer's debit and credit cards with status."""
    return {
        "ok": True,
        "cards": [
            {
                "card_id": c["card_id"],
                "number": mask_card(c["last4"]),
                "kind": c["kind"],
                "product": c["product"],
                "network": c["network"],
                "status": c["status"],
                "expiry": c["expiry"],
                "linked_account": mask_account(c["linked_account"]) if c["linked_account"] else None,
            }
            for c in BANK.customer["cards"]
        ],
    }


def get_card_details(card_ref: str) -> dict:
    """Get full details of one card: status, limits, feature switches, and credit outstanding if it's a credit card. card_ref can be the last 4 digits, card id, or product name."""
    c = BANK.card(card_ref)
    if not c:
        return not_found("card", card_ref)
    out = {
        "ok": True,
        "card_id": c["card_id"],
        "number": mask_card(c["last4"]),
        "product": c["product"],
        "kind": c["kind"],
        "network": c["network"],
        "status": c["status"],
        "expiry": c["expiry"],
        "issued": c["issued"],
        "features": {
            "international": c["international_enabled"],
            "contactless": c["contactless_enabled"],
            "online_ecom": c["ecom_enabled"],
            "atm": c["atm_enabled"],
        },
        "limits": {k: inr(v) for k, v in c["limits"].items()},
    }
    if c["kind"] == "Credit":
        out.update(
            {
                "total_outstanding": inr(c["total_outstanding"]),
                "available_credit": inr(c["available_credit"]),
                "min_due": inr(c["min_due"]),
                "statement_date": c["statement_date"],
                "due_date": c["due_date"],
                "autopay": c["autopay"],
            }
        )
    return out


def block_card(card_ref: str, reason: str) -> dict:
    """Block a card. reason='lost'/'stolen'/'fraud' hotlists it permanently and needs NO OTP (customer safety first). Any other reason is a reversible temporary block and DOES need OTP."""
    c = BANK.card(card_ref)
    if not c:
        return not_found("card", card_ref)
    if c["status"].lower().startswith("blocked") or c["status"] == "Hotlisted":
        return {"ok": True, "already": True, "card": mask_card(c["last4"]), "status": c["status"]}

    permanent = any(w in (reason or "").lower() for w in _FRAUD_REASONS)
    if not permanent and not BANK.otp_ok():
        return need_otp("temporary card block")

    c["status"] = "Hotlisted" if permanent else "Temporarily blocked"
    if not permanent:
        BANK.consume_otp()
    ref = BANK.ref("BLK")
    BANK.log("card_blocked", f"{mask_card(c['last4'])} ({reason}) -> {c['status']}", ref=ref)

    out = {
        "ok": True,
        "reference": ref,
        "card": mask_card(c["last4"]),
        "product": c["product"],
        "status": c["status"],
        "reason": reason,
        "effective": "Immediately — the card cannot be used from now",
        "reversible": not permanent,
    }
    if permanent:
        out["replacement"] = {
            "auto_issued": True,
            "charge": "Free (lost/stolen/fraud)",
            "expected_delivery": working_days_from_now(7),
            "delivery_address": BANK.customer["address"],
        }
        out["liability"] = (
            "Reported within 3 working days of an unauthorised transaction, "
            "the customer's liability is zero under RBI rules."
        )
    return out


def unblock_card(card_ref: str) -> dict:
    """Lift a temporary block on a card. Requires verified OTP. A hotlisted (lost/stolen) card can never be unblocked."""
    c = BANK.card(card_ref)
    if not c:
        return not_found("card", card_ref)
    if c["status"] == "Hotlisted":
        return {
            "ok": False,
            "error": "permanently_blocked",
            "message": "This card is hotlisted and cannot be unblocked. A replacement has been issued.",
        }
    if c["status"] == "Active":
        return {"ok": True, "already": True, "card": mask_card(c["last4"]), "status": "Active"}
    if not BANK.otp_ok():
        return need_otp("card unblock")

    c["status"] = "Active"
    BANK.consume_otp()
    ref = BANK.ref("UNB")
    BANK.log("card_unblocked", mask_card(c["last4"]), ref=ref)
    return {"ok": True, "reference": ref, "card": mask_card(c["last4"]), "status": "Active", "effective": "Immediately"}


def reissue_card(card_ref: str, reason: str) -> dict:
    """Order a replacement card (damaged, worn out, name change, or after a hotlist)."""
    c = BANK.card(card_ref)
    if not c:
        return not_found("card", card_ref)
    free = any(w in (reason or "").lower() for w in _FRAUD_REASONS) or BANK.customer["segment"] == "Burgundy"
    c["delivery"] = {
        "courier": "Blue Dart",
        "awb": BANK.ref("AWB"),
        "status": "Card personalisation in progress",
        "expected": working_days_from_now(7),
        "last_scan": "Request received",
    }
    ref = BANK.ref("REI")
    BANK.log("card_reissued", f"{mask_card(c['last4'])} ({reason})", ref=ref)
    return {
        "ok": True,
        "reference": ref,
        "card": mask_card(c["last4"]),
        "reason": reason,
        "charge": "Free" if free else ("₹200 + GST" if c["kind"] == "Debit" else "₹100 + GST"),
        "expected_delivery": c["delivery"]["expected"],
        "delivery_address": BANK.customer["address"],
        "tracking_awb": c["delivery"]["awb"],
        "note": "The new card carries a new number and CVV. Any standing e-mandates must be updated.",
    }


def track_card_delivery(card_ref: str) -> dict:
    """Track the courier status of a card that has been dispatched."""
    c = BANK.card(card_ref)
    if not c:
        return not_found("card", card_ref)
    if not c.get("delivery"):
        return {
            "ok": True,
            "card": mask_card(c["last4"]),
            "status": c["status"],
            "message": "No dispatch in progress for this card.",
        }
    return {"ok": True, "card": mask_card(c["last4"]), "product": c["product"], **c["delivery"]}


def set_card_limit(card_ref: str, limit_type: str, amount: float) -> dict:
    """Change a card's transaction limit. limit_type: 'atm_daily', 'pos_daily', 'ecom_daily' or 'cash_limit'. Requires verified OTP."""
    c = BANK.card(card_ref)
    if not c:
        return not_found("card", card_ref)
    key = (limit_type or "").strip().lower()
    if key not in c["limits"]:
        return {
            "ok": False,
            "error": "bad_limit_type",
            "message": f"Available limits on this card: {', '.join(c['limits'])}",
        }
    if not BANK.otp_ok():
        return need_otp("card limit change")

    old = c["limits"][key]
    c["limits"][key] = amount
    BANK.consume_otp()
    ref = BANK.ref("LMT")
    BANK.log("limit_changed", f"{mask_card(c['last4'])} {key}: {old} -> {amount}", ref=ref)
    return {
        "ok": True,
        "reference": ref,
        "card": mask_card(c["last4"]),
        "limit_type": key,
        "old_limit": inr(old),
        "new_limit": inr(amount),
        "effective": "Immediately",
    }


def toggle_card_feature(card_ref: str, feature: str, enable: bool) -> dict:
    """Switch a card feature on or off. feature: 'international', 'contactless', 'ecom' (online) or 'atm'. Requires verified OTP."""
    c = BANK.card(card_ref)
    if not c:
        return not_found("card", card_ref)
    field = {
        "international": "international_enabled",
        "contactless": "contactless_enabled",
        "ecom": "ecom_enabled",
        "online": "ecom_enabled",
        "atm": "atm_enabled",
    }.get((feature or "").strip().lower())
    if not field:
        return {"ok": False, "error": "bad_feature", "message": "feature must be international, contactless, ecom or atm"}
    if not BANK.otp_ok():
        return need_otp(f"{feature} toggle")

    c[field] = enable
    BANK.consume_otp()
    ref = BANK.ref("FTR")
    BANK.log("feature_toggled", f"{mask_card(c['last4'])} {feature}={enable}", ref=ref)
    out = {
        "ok": True,
        "reference": ref,
        "card": mask_card(c["last4"]),
        "feature": feature,
        "enabled": enable,
        "effective": "Immediately",
    }
    if field == "international_enabled" and enable:
        out["forex_markup"] = "2% on Magnus, 3.5% on other cards"
        out["tip"] = "Always pay in the local currency abroad — DCC at the merchant costs more."
    return out


def reset_card_pin(card_ref: str) -> dict:
    """Start a PIN reset. Requires verified OTP. Sends a secure link; the PIN is never spoken or shown in chat."""
    c = BANK.card(card_ref)
    if not c:
        return not_found("card", card_ref)
    if not BANK.otp_ok():
        return need_otp("PIN reset")

    BANK.consume_otp()
    ref = BANK.ref("PIN")
    BANK.log("pin_reset_link_sent", mask_card(c["last4"]), ref=ref)
    return {
        "ok": True,
        "reference": ref,
        "card": mask_card(c["last4"]),
        "sent_to": BANK.customer["mobile"],
        "link_valid_for": "15 minutes",
        "note": "The customer sets the new PIN themselves on the secure page. Never ask for or state a PIN in chat.",
    }


def get_credit_card_bill(card_ref: str) -> dict:
    """Get the current credit card statement: total outstanding, minimum due, due date and available credit."""
    c = BANK.card(card_ref)
    if not c:
        return not_found("credit card", card_ref)
    if c["kind"] != "Credit":
        return {"ok": False, "error": "not_credit_card", "message": f"{mask_card(c['last4'])} is a debit card."}
    return {
        "ok": True,
        "card": mask_card(c["last4"]),
        "product": c["product"],
        "statement_date": c["statement_date"],
        "due_date": c["due_date"],
        "total_outstanding": inr(c["total_outstanding"]),
        "minimum_due": inr(c["min_due"]),
        "credit_limit": inr(c["limits"]["credit_limit"]),
        "available_credit": inr(c["available_credit"]),
        "last_payment": c.get("last_payment"),
        "autopay": c["autopay"],
        "interest_warning": (
            "Paying only the minimum due avoids the late fee but attracts 3.6% per month "
            "on the full outstanding from the transaction date."
        ),
    }


def pay_credit_card_bill(card_ref: str, amount: float, from_account: str) -> dict:
    """Pay a credit card bill from a savings account. Requires verified OTP."""
    c = BANK.card(card_ref)
    if not c or c["kind"] != "Credit":
        return not_found("credit card", card_ref)
    a = BANK.account(from_account)
    if not a:
        return not_found("account", from_account)
    if not BANK.otp_ok():
        return need_otp("credit card payment")
    if amount > a["available_balance"]:
        return {
            "ok": False,
            "error": "insufficient_funds",
            "message": f"Available balance is {inr(a['available_balance'])}, payment is {inr(amount)}.",
        }

    a["balance"] -= amount
    a["available_balance"] -= amount
    c["total_outstanding"] = max(0.0, c["total_outstanding"] - amount)
    c["available_credit"] = min(c["limits"]["credit_limit"], c["available_credit"] + amount)
    c["min_due"] = round(c["total_outstanding"] * 0.05, 2)
    c["last_payment"] = {"amount": amount, "date": today().isoformat()}
    BANK.consume_otp()
    ref = BANK.ref("CCP")
    BANK.log("cc_payment", f"{inr(amount)} to {mask_card(c['last4'])} from {mask_account(a['account_no'])}", ref=ref)
    return {
        "ok": True,
        "reference": ref,
        "paid": inr(amount),
        "card": mask_card(c["last4"]),
        "from_account": mask_account(a["account_no"]),
        "remaining_outstanding": inr(c["total_outstanding"]),
        "new_account_balance": inr(a["balance"]),
        "posting": "Instant. Credit limit released immediately.",
    }


def convert_transaction_to_emi(txn_ref: str, tenure_months: int) -> dict:
    """Convert an eligible credit card transaction (₹2,500+, within 30 days) into EMIs. Tenure: 3, 6, 9, 12, 18 or 24 months."""
    t = BANK.transaction(txn_ref)
    if not t:
        return not_found("transaction", txn_ref)
    if t["amount"] < 2500:
        return {"ok": False, "error": "ineligible", "message": "Only transactions of ₹2,500 or more can be converted."}
    tenure = tenure_months if tenure_months in (3, 6, 9, 12, 18, 24) else 6

    rate = {3: 13.0, 6: 14.0, 9: 14.5, 12: 15.0, 18: 15.5, 24: 16.0}[tenure]
    r = rate / 1200
    emi = round(t["amount"] * r * (1 + r) ** tenure / ((1 + r) ** tenure - 1), 2)
    fee = max(199.0, round(t["amount"] * 0.01, 2))
    ref = BANK.ref("EMI")
    BANK.log("emi_conversion", f"{t['description']} {inr(t['amount'])} -> {tenure}m", ref=ref)
    return {
        "ok": True,
        "reference": ref,
        "transaction": t["description"],
        "amount": inr(t["amount"]),
        "tenure_months": tenure,
        "interest_rate": f"{rate}% p.a.",
        "monthly_emi": inr(emi),
        "total_payable": inr(emi * tenure + fee),
        "processing_fee": inr(fee),
        "first_emi_on": "Next statement",
        "note": "The converted amount stays blocked against the credit limit and is released as EMIs are paid.",
    }


def setup_autopay(card_ref: str, autopay_type: str, from_account: str) -> dict:
    """Set up credit card autopay. autopay_type: 'full' (total due) or 'minimum'. Requires verified OTP."""
    c = BANK.card(card_ref)
    if not c or c["kind"] != "Credit":
        return not_found("credit card", card_ref)
    a = BANK.account(from_account)
    if not a:
        return not_found("account", from_account)
    kind = "minimum" if "min" in (autopay_type or "").lower() else "full"
    if not BANK.otp_ok():
        return need_otp("autopay setup")

    c["autopay"] = {"enabled": True, "type": kind, "account": a["account_no"]}
    BANK.consume_otp()
    ref = BANK.ref("APY")
    BANK.log("autopay_set", f"{mask_card(c['last4'])} {kind} from {mask_account(a['account_no'])}", ref=ref)
    return {
        "ok": True,
        "reference": ref,
        "card": mask_card(c["last4"]),
        "autopay_type": kind,
        "debit_account": mask_account(a["account_no"]),
        "debit_day": "3 days before the due date",
        "starts_from": "Next statement cycle",
    }


def request_credit_limit_increase(card_ref: str, requested_limit: float) -> dict:
    """Request a credit limit increase. Checks for a pre-approved offer first; otherwise raises an assessment request."""
    c = BANK.card(card_ref)
    if not c or c["kind"] != "Credit":
        return not_found("credit card", card_ref)

    for o in BANK.customer.get("offers", []):
        if o.get("type") == "Credit Limit Increase" and o.get("card") == c["card_id"]:
            if requested_limit <= o["new_limit"]:
                old = c["limits"]["credit_limit"]
                c["limits"]["credit_limit"] = requested_limit
                c["available_credit"] += requested_limit - old
                ref = BANK.ref("CLI")
                BANK.log("limit_increase_instant", f"{mask_card(c['last4'])} {old} -> {requested_limit}", ref=ref)
                return {
                    "ok": True,
                    "reference": ref,
                    "pre_approved": True,
                    "card": mask_card(c["last4"]),
                    "old_limit": inr(old),
                    "new_limit": inr(requested_limit),
                    "effective": "Immediately",
                    "documents_required": "None — this was a pre-approved offer.",
                }

    ref = BANK.ref("SR")
    BANK.log("limit_increase_requested", f"{mask_card(c['last4'])} -> {requested_limit}", ref=ref)
    return {
        "ok": True,
        "reference": ref,
        "pre_approved": False,
        "status": "Under assessment",
        "requested_limit": inr(requested_limit),
        "documents_required": "Latest 3 months' salary slips or last 2 years' ITR",
        "tat": "7 working days after documents are received",
        "upload_via": "Internet Banking > Cards > Limit Enhancement",
    }


def get_reward_points() -> dict:
    """Get the customer's EDGE reward points balance, cash value, tier and any points expiring soon."""
    r = BANK.customer["rewards"]
    return {
        "ok": True,
        "program": r["program"],
        "points": r["points_balance"],
        "cash_value": inr(r["points_value_inr"]),
        "tier": r["tier"],
        "expiring_soon": r["expiring_soon"],
        "redemption_options": [
            "Statement credit at 1 point = ₹0.20",
            "Vouchers and catalogue at 1 point = ₹0.20",
            "Airline/hotel partner transfer at up to 1 point = ₹1.00 (Magnus/Reserve)",
        ],
        "minimum_redemption": 500,
    }


def redeem_reward_points(points: int, mode: str) -> dict:
    """Redeem EDGE points. mode: 'statement_credit', 'voucher' or 'partner_transfer'. Minimum 500 points."""
    r = BANK.customer["rewards"]
    if points < 500:
        return {"ok": False, "error": "below_minimum", "message": "Minimum redemption is 500 points."}
    if points > r["points_balance"]:
        return {"ok": False, "error": "insufficient_points", "message": f"Balance is {r['points_balance']} points."}

    rate = 1.0 if "partner" in (mode or "").lower() and r["tier"] == "Burgundy" else 0.20
    value = round(points * rate, 2)
    r["points_balance"] -= points
    r["points_value_inr"] = round(r["points_balance"] * 0.20, 2)
    ref = BANK.ref("RDM")
    BANK.log("points_redeemed", f"{points} pts via {mode} = {inr(value)}", ref=ref)
    return {
        "ok": True,
        "reference": ref,
        "points_redeemed": points,
        "mode": mode,
        "value": inr(value),
        "points_remaining": r["points_balance"],
        "credit_in": "3 working days" if "statement" in (mode or "").lower() else "24 hours",
    }
