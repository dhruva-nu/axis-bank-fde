"""Transfers, beneficiaries, bills, UPI, FASTag and standing instructions."""

from __future__ import annotations

from ..bank import BANK, days_from_now, inr, mask_account, need_otp, not_found, today


def list_beneficiaries() -> dict:
    """List the customer's registered payees with their per-transaction limits."""
    return {
        "ok": True,
        "beneficiaries": [
            {
                "id": b["id"],
                "name": b["name"],
                "nickname": b["nickname"],
                "bank": b["bank"],
                "account_no": mask_account(b["account_no"]),
                "ifsc": b["ifsc"],
                "limit": inr(b["limit"]),
                "status": b["status"],
            }
            for b in BANK.customer["beneficiaries"]
        ],
    }


def add_beneficiary(name: str, account_no: str, ifsc: str, nickname: str) -> dict:
    """Register a new payee. Requires verified OTP. A 30-minute cooling period applies before large transfers."""
    if not BANK.otp_ok():
        return need_otp("add beneficiary")

    b = {
        "id": BANK.ref("BEN"),
        "name": name,
        "account_no": account_no,
        "ifsc": ifsc,
        "bank": "Axis Bank" if ifsc.upper().startswith("UTIB") else "Other bank",
        "nickname": nickname or name,
        "limit": 500000,
        "added": today().isoformat(),
        "status": "Cooling period - full limit in 30 minutes",
    }
    BANK.customer["beneficiaries"].append(b)
    BANK.consume_otp()
    BANK.log("beneficiary_added", f"{name} {mask_account(account_no)} @ {ifsc}", ref=b["id"])
    return {
        "ok": True,
        "reference": b["id"],
        "name": name,
        "account_no": mask_account(account_no),
        "ifsc": ifsc,
        "cooling_period": "30 minutes — up to ₹5,000 can be sent immediately",
        "default_limit": inr(500000),
    }


def transfer_funds(beneficiary: str, amount: float, mode: str, from_account: str, remarks: str) -> dict:
    """Send money to a registered payee. mode: 'IMPS', 'NEFT' or 'RTGS' (pass empty to auto-pick). Requires verified OTP."""
    ben = None
    key = (beneficiary or "").strip().lower()
    for b in BANK.customer["beneficiaries"]:
        if key in (b["name"].lower(), b["nickname"].lower(), b["id"].lower()) or key in b["name"].lower():
            ben = b
            break
    if not ben:
        return {
            "ok": False,
            "error": "beneficiary_not_found",
            "message": f"'{beneficiary}' is not a registered payee. Add them first with add_beneficiary.",
            "registered": [b["nickname"] for b in BANK.customer["beneficiaries"]],
        }

    a = BANK.account(from_account)
    if not a:
        return not_found("account", from_account)
    if not BANK.otp_ok():
        return need_otp("fund transfer")

    channel = (mode or "").strip().upper()
    if channel not in ("IMPS", "NEFT", "RTGS"):
        channel = "RTGS" if amount >= 200000 else "IMPS"
    if amount > a["available_balance"]:
        return {
            "ok": False,
            "error": "insufficient_funds",
            "message": f"Available balance {inr(a['available_balance'])} is less than {inr(amount)}.",
        }
    if amount > ben["limit"]:
        return {
            "ok": False,
            "error": "limit_exceeded",
            "message": f"Per-transaction limit for {ben['nickname']} is {inr(ben['limit'])}.",
        }
    if "Cooling" in ben["status"] and amount > 5000:
        return {
            "ok": False,
            "error": "cooling_period",
            "message": f"{ben['nickname']} was added recently — only ₹5,000 can be sent until the cooling period ends.",
        }

    charge = 5.9 if channel == "IMPS" and amount > 1000 else 0.0
    a["balance"] -= amount + charge
    a["available_balance"] = a["balance"]
    ref = BANK.ref("TXN")
    BANK.customer["transactions"].insert(
        0,
        {
            "id": ref,
            "date": today().isoformat(),
            "account": a["account_no"],
            "description": f"{channel}/DR/{ben['name'].upper()}",
            "channel": channel,
            "type": "debit",
            "amount": amount,
            "balance_after": a["balance"],
            "status": "Success",
            "merchant": ben["name"],
        },
    )
    BANK.consume_otp()
    BANK.log("funds_transferred", f"{inr(amount)} to {ben['nickname']} via {channel}", ref=ref)
    return {
        "ok": True,
        "reference": ref,
        "utr": ref.replace("TXN", "AXISN"),
        "amount": inr(amount),
        "to": f"{ben['name']} ({mask_account(ben['account_no'])}, {ben['bank']})",
        "mode": channel,
        "charge": inr(charge) + " + GST" if charge else "Free",
        "remarks": remarks,
        "status": "Success",
        "credited": "Instantly" if channel in ("IMPS", "RTGS") else "Within 30 minutes (next NEFT batch)",
        "new_balance": inr(a["balance"]),
    }


def check_transaction_status(reference: str) -> dict:
    """Look up a transaction by its ID, UTR, RRN or a keyword, and report whether it succeeded, failed or is pending."""
    t = BANK.transaction(reference)
    if not t:
        return not_found("transaction", reference)
    out = {
        "ok": True,
        "id": t["id"],
        "date": t["date"],
        "description": t["description"],
        "channel": t["channel"],
        "amount": inr(t["amount"]),
        "direction": t["type"],
        "status": t["status"],
        "rrn": t.get("rrn"),
        "note": t.get("note", ""),
    }
    if "fail" in t["status"].lower():
        out["reversal"] = {
            "auto_reversal": True,
            "expected_by": days_from_now(1) if t["channel"] in ("UPI", "IMPS") else days_from_now(5),
            "rbi_rule": (
                "UPI/IMPS auto-reverse by T+1; ATM and card failures by T+5. "
                "Beyond that the bank pays ₹100 per day of delay, credited automatically."
            ),
            "action": "Use report_failed_transaction to log it and get a tracking number.",
        }
    return out


def pay_bill(biller: str, amount: float, from_account: str) -> dict:
    """Pay a utility bill, mobile/DTH recharge or insurance premium from an account. Requires verified OTP."""
    a = BANK.account(from_account)
    if not a:
        return not_found("account", from_account)
    if not BANK.otp_ok():
        return need_otp("bill payment")
    if amount > a["available_balance"]:
        return {"ok": False, "error": "insufficient_funds", "message": f"Available balance {inr(a['available_balance'])}."}

    a["balance"] -= amount
    a["available_balance"] = a["balance"]
    ref = BANK.ref("BIL")
    BANK.customer["transactions"].insert(
        0,
        {
            "id": ref,
            "date": today().isoformat(),
            "account": a["account_no"],
            "description": f"BILLPAY/{biller.upper()}",
            "channel": "BBPS",
            "type": "debit",
            "amount": amount,
            "balance_after": a["balance"],
            "status": "Success",
            "merchant": biller,
        },
    )
    BANK.consume_otp()
    BANK.log("bill_paid", f"{biller} {inr(amount)}", ref=ref)
    return {
        "ok": True,
        "reference": ref,
        "biller": biller,
        "amount": inr(amount),
        "from_account": mask_account(a["account_no"]),
        "status": "Success",
        "posting": "Biller confirmation within 2 hours (BBPS)",
        "new_balance": inr(a["balance"]),
    }


def get_fastag_status() -> dict:
    """Get FASTag balance, linked vehicle and blacklist risk."""
    f = BANK.customer.get("fastag")
    if not f:
        return {"ok": True, "has_fastag": False, "message": "No FASTag is linked to this customer."}
    return {
        "ok": True,
        "has_fastag": True,
        "tag_id": f["tag_id"],
        "vehicle": f["vehicle"],
        "balance": inr(f["balance"]),
        "status": f["status"],
        "low_balance": f["balance"] < f["low_balance_threshold"],
        "warning": (
            "Balance is low. A tag with insufficient balance is blacklisted at the plaza "
            "and the toll is charged at double, in cash."
            if f["balance"] < f["low_balance_threshold"]
            else ""
        ),
    }


def recharge_fastag(amount: float, from_account: str) -> dict:
    """Top up the customer's FASTag from an account. Requires verified OTP."""
    f = BANK.customer.get("fastag")
    if not f:
        return {"ok": False, "error": "no_fastag", "message": "No FASTag linked to this customer."}
    a = BANK.account(from_account)
    if not a:
        return not_found("account", from_account)
    if not BANK.otp_ok():
        return need_otp("FASTag recharge")

    a["balance"] -= amount
    a["available_balance"] = a["balance"]
    f["balance"] += amount
    BANK.consume_otp()
    ref = BANK.ref("FTG")
    BANK.log("fastag_recharged", f"{inr(amount)} on {f['tag_id']}", ref=ref)
    return {
        "ok": True,
        "reference": ref,
        "amount": inr(amount),
        "new_tag_balance": inr(f["balance"]),
        "vehicle": f["vehicle"],
        "credited": "Instantly",
        "tip": "Auto-recharge can be set to trigger below a threshold — ask if the customer wants it.",
    }


def list_standing_instructions() -> dict:
    """List active standing instructions, e-mandates and auto-debits, with amounts and dates."""
    return {"ok": True, "standing_instructions": BANK.customer["standing_instructions"]}


def cancel_standing_instruction(si_id: str) -> dict:
    """Cancel a standing instruction or e-mandate. Requires verified OTP."""
    si = next((s for s in BANK.customer["standing_instructions"] if s["si_id"].lower() == (si_id or "").lower()), None)
    if not si:
        return not_found("standing instruction", si_id)
    if not BANK.otp_ok():
        return need_otp("cancel standing instruction")

    si["status"] = "Cancelled"
    BANK.consume_otp()
    ref = BANK.ref("SI")
    BANK.log("si_cancelled", f"{si['si_id']} {si['description']}", ref=ref)
    return {
        "ok": True,
        "reference": ref,
        "si_id": si["si_id"],
        "description": si["description"],
        "status": "Cancelled",
        "effective": "From the next scheduled debit",
        "warning": "If this mandate covers a loan EMI, cancelling it will cause a bounce charge.",
    }


def get_upi_status() -> dict:
    """Get the customer's UPI handles, daily limit and usage so far today."""
    u = BANK.customer.get("upi")
    if not u:
        return {"ok": True, "registered": False}
    return {
        "ok": True,
        "registered": True,
        "handles": u["handles"],
        "status": u["status"],
        "daily_limit": inr(u["daily_limit"]),
        "used_today": inr(u["used_today"]),
        "remaining_today": inr(u["daily_limit"] - u["used_today"]),
        "linked_accounts": [mask_account(x) for x in u["linked_accounts"]],
    }


def deregister_upi() -> dict:
    """Deregister UPI on this customer's number, e.g. after losing a phone. Requires verified OTP."""
    u = BANK.customer.get("upi")
    if not u:
        return {"ok": False, "error": "not_registered", "message": "UPI is not registered."}
    if not BANK.otp_ok():
        return need_otp("UPI deregistration")

    u["status"] = "Deregistered"
    BANK.consume_otp()
    ref = BANK.ref("UPI")
    BANK.log("upi_deregistered", ", ".join(u["handles"]), ref=ref)
    return {
        "ok": True,
        "reference": ref,
        "status": "Deregistered",
        "effective": "Immediately",
        "impact": "All UPI autopay mandates on these handles are cancelled and must be re-created after re-registering.",
    }
