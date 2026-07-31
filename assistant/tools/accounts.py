"""Savings/current account servicing."""

from __future__ import annotations

from ..bank import BANK, days_from_now, inr, mask_account, need_otp, not_found, working_days_from_now


def get_customer_profile() -> dict:
    """Get the logged-in customer's profile: name, CIF, segment, contact details, KYC status."""
    c = BANK.customer
    return {
        "ok": True,
        "name": c["name"],
        "cif": c["cif"],
        "segment": c["segment"],
        "mobile": c["mobile"],
        "email": c["email"],
        "address": c["address"],
        "home_branch": c["home_branch"],
        "customer_since": c["relationship_since"],
        "kyc_status": c["kyc_status"],
        "kyc_expiry": c["kyc_expiry"],
        "preferred_language": c["preferred_language"],
    }


def list_accounts() -> dict:
    """List all the customer's deposit accounts with balances and status."""
    return {
        "ok": True,
        "accounts": [
            {
                "account_no": mask_account(a["account_no"]),
                "full_account_no": a["account_no"],
                "type": a["type"],
                "branch": a["branch"],
                "ifsc": a["ifsc"],
                "balance": inr(a["balance"]),
                "available": inr(a["available_balance"]),
                "status": a["status"],
                "nominee": a["nominee"],
            }
            for a in BANK.customer["accounts"]
        ],
    }


def get_account_balance(account_no: str) -> dict:
    """Get the balance of one account. Pass an empty string for the primary account."""
    a = BANK.account(account_no)
    if not a:
        return not_found("account", account_no)
    BANK.log("balance_enquiry", f"{mask_account(a['account_no'])}")
    return {
        "ok": True,
        "account_no": mask_account(a["account_no"]),
        "type": a["type"],
        "balance": inr(a["balance"]),
        "available_balance": inr(a["available_balance"]),
        "lien_amount": inr(a["lien_amount"]),
        "status": a["status"],
        "amb_required": inr(a["avg_monthly_balance_required"]),
    }


def get_mini_statement(account_no: str, count: int) -> dict:
    """Get the last N transactions on an account. Pass count=0 for the default of 10."""
    a = BANK.account(account_no)
    if not a:
        return not_found("account", account_no)
    n = count if count and count > 0 else 10
    txns = [t for t in BANK.customer["transactions"] if t["account"] == a["account_no"]][:n]
    BANK.log("mini_statement", f"{mask_account(a['account_no'])}, {len(txns)} rows")
    return {
        "ok": True,
        "account_no": mask_account(a["account_no"]),
        "closing_balance": inr(a["balance"]),
        "transactions": [
            {
                "id": t["id"],
                "date": t["date"],
                "description": t["description"],
                "channel": t["channel"],
                "direction": t["type"],
                "amount": inr(t["amount"]),
                "status": t["status"],
                "note": t.get("note", ""),
            }
            for t in txns
        ],
    }


def search_transactions(query: str, account_no: str) -> dict:
    """Find transactions matching a merchant, amount, channel or keyword (e.g. 'swiggy', 'atm', 'failed', '12499')."""
    q = (query or "").strip().lower()
    acct = BANK.account(account_no) if account_no else None
    hits = []
    for t in BANK.customer["transactions"]:
        if acct and t["account"] != acct["account_no"]:
            continue
        hay = (
            f"{t['description']} {t.get('merchant', '')} {t['channel']} "
            f"{t['status']} {t['amount']} {t.get('note', '')} {t.get('rrn', '')}"
        ).lower()
        if q in hay:
            hits.append(t)
    return {
        "ok": True,
        "query": query,
        "count": len(hits),
        "transactions": [
            {
                "id": t["id"],
                "date": t["date"],
                "account": t["account"],
                "description": t["description"],
                "channel": t["channel"],
                "direction": t["type"],
                "amount": inr(t["amount"]),
                "status": t["status"],
                "note": t.get("note", ""),
            }
            for t in hits
        ],
    }


def email_statement(account_no: str, from_date: str, to_date: str) -> dict:
    """Email a password-protected e-statement for a date range (YYYY-MM-DD) to the registered email."""
    a = BANK.account(account_no)
    if not a:
        return not_found("account", account_no)
    ref = BANK.ref("STMT")
    BANK.log("statement_emailed", f"{mask_account(a['account_no'])} {from_date}..{to_date}", ref=ref)
    return {
        "ok": True,
        "reference": ref,
        "sent_to": BANK.customer["email"],
        "period": f"{from_date} to {to_date}",
        "delivery": "Within 10 minutes",
        "password_hint": "PAN in lowercase + date of birth as DDMM",
        "charge": "Free",
    }


def get_interest_certificate(account_no: str, financial_year: str) -> dict:
    """Generate an interest / TDS certificate for a financial year (e.g. '2025-26')."""
    a = BANK.account(account_no)
    if not a:
        return not_found("account", account_no)
    ref = BANK.ref("CERT")
    BANK.log("interest_certificate", f"{financial_year} for {mask_account(a['account_no'])}", ref=ref)
    return {
        "ok": True,
        "reference": ref,
        "financial_year": financial_year,
        "sent_to": BANK.customer["email"],
        "delivery": "Instant",
        "charge": "Free",
    }


def update_contact_details(field: str, value: str) -> dict:
    """Update the registered mobile, email or address. field must be 'mobile', 'email' or 'address'. Requires verified OTP."""
    field = field.strip().lower()
    if field not in ("mobile", "email", "address"):
        return {"ok": False, "error": "bad_field", "message": "field must be mobile, email or address"}
    if not BANK.otp_ok():
        return need_otp(f"update {field}")

    old = BANK.customer[field]
    BANK.customer[field] = value
    BANK.consume_otp()
    ref = BANK.ref("SR")
    BANK.log("contact_updated", f"{field}: {old} -> {value}", ref=ref)
    return {
        "ok": True,
        "reference": ref,
        "field": field,
        "old_value": old,
        "new_value": value,
        "effective": "Immediately",
        "note": "A confirmation SMS has been sent to the old and new contact.",
    }


def update_nominee(account_no: str, nominee_name: str, relationship: str) -> dict:
    """Register or change the nominee on an account. Requires verified OTP."""
    a = BANK.account(account_no)
    if not a:
        return not_found("account", account_no)
    if not BANK.otp_ok():
        return need_otp("nominee update")

    old = a["nominee"]
    a["nominee"] = f"{nominee_name} ({relationship})"
    BANK.consume_otp()
    ref = BANK.ref("SR")
    BANK.log("nominee_updated", f"{mask_account(a['account_no'])}: {old} -> {a['nominee']}", ref=ref)
    return {
        "ok": True,
        "reference": ref,
        "account_no": mask_account(a["account_no"]),
        "previous_nominee": old,
        "new_nominee": a["nominee"],
        "effective": "Immediately",
        "charge": "Free",
    }


def request_cheque_book(account_no: str, leaves: int) -> dict:
    """Order a cheque book. leaves must be 25, 50 or 100; pass 0 for the default 25."""
    a = BANK.account(account_no)
    if not a:
        return not_found("account", account_no)
    n = leaves if leaves in (25, 50, 100) else 25
    free = BANK.customer["segment"] in ("Prime", "Burgundy") and a["cheque_books_issued_this_year"] == 0
    a["cheque_books_issued_this_year"] += 1
    ref = BANK.ref("CHQ")
    BANK.log("cheque_book_ordered", f"{n} leaves on {mask_account(a['account_no'])}", ref=ref)
    return {
        "ok": True,
        "reference": ref,
        "account_no": mask_account(a["account_no"]),
        "leaves": n,
        "charge": "Free (first book this financial year)" if free else inr(100 * (n / 25)) + " + GST",
        "delivery_address": BANK.customer["address"],
        "expected_delivery": working_days_from_now(7),
        "tat": "5-7 working days",
    }


def stop_cheque_payment(account_no: str, cheque_number: str, reason: str) -> dict:
    """Place a stop-payment instruction on a cheque that has not yet been presented."""
    a = BANK.account(account_no)
    if not a:
        return not_found("account", account_no)
    ref = BANK.ref("STOP")
    BANK.log("stop_payment", f"cheque {cheque_number} on {mask_account(a['account_no'])}: {reason}", ref=ref)
    return {
        "ok": True,
        "reference": ref,
        "cheque_number": cheque_number,
        "account_no": mask_account(a["account_no"]),
        "reason": reason,
        "status": "Stop payment active",
        "charge": "Free via this channel",
        "valid_until": days_from_now(180),
        "warning": "This cannot stop a cheque that has already been presented and cleared.",
    }


def get_branch_and_ifsc(account_no: str) -> dict:
    """Get the home branch, IFSC and MICR details for an account (for sharing with a remitter)."""
    a = BANK.account(account_no)
    if not a:
        return not_found("account", account_no)
    return {
        "ok": True,
        "account_no": a["account_no"],
        "account_holder": BANK.customer["name"],
        "type": a["type"],
        "branch": a["branch"],
        "ifsc": a["ifsc"],
        "bank": "Axis Bank",
    }
