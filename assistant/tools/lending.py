"""Loans, EMIs, fixed and recurring deposits."""

from __future__ import annotations

from ..bank import BANK, days_from_now, inr, mask_account, need_otp, not_found, today, working_days_from_now

_FD_RATES = [
    (0.5, 4.25), (3, 5.75), (6, 6.25), (9, 6.75), (12, 7.10),
    (15, 7.25), (24, 7.25), (36, 7.10), (60, 7.00), (120, 7.00),
]


def _rate_for(tenure_months: int) -> float:
    rate = _FD_RATES[0][1]
    for months, r in _FD_RATES:
        if tenure_months >= months:
            rate = r
    return rate


# --------------------------------------------------------------------------- #
# Loans
# --------------------------------------------------------------------------- #


def list_loans() -> dict:
    """List the customer's loans with outstanding balance, EMI and repayment status."""
    return {
        "ok": True,
        "loans": [
            {
                "loan_id": l["loan_id"],
                "type": l["type"],
                "outstanding": inr(l["outstanding"]),
                "emi": inr(l["emi"]),
                "rate": f"{l['rate']}% ({l['rate_type']})",
                "next_due": l["next_due"],
                "status": l["status"],
                "overdue": inr(l["overdue_amount"]) if l["overdue_amount"] else None,
            }
            for l in BANK.customer["loans"]
        ],
    }


def get_loan_details(loan_ref: str) -> dict:
    """Get full details of one loan: sanctioned amount, outstanding, rate, EMIs paid and remaining, and any overdue."""
    l = BANK.loan(loan_ref)
    if not l:
        return not_found("loan", loan_ref)
    out = {
        "ok": True,
        "loan_id": l["loan_id"],
        "type": l["type"],
        "sanctioned": inr(l["sanctioned"]),
        "outstanding": inr(l["outstanding"]),
        "emi": inr(l["emi"]),
        "rate": f"{l['rate']}% p.a.",
        "rate_type": l["rate_type"],
        "tenure_months": l["tenure_months"],
        "emis_paid": l["emis_paid"],
        "emis_remaining": l["emis_remaining"],
        "next_due_date": l["next_due"],
        "emi_debit_account": mask_account(l["emi_account"]),
        "status": l["status"],
    }
    if l["overdue_amount"]:
        out["overdue"] = {
            "amount": inr(l["overdue_amount"]),
            "bounce_reason": l.get("bounce_reason"),
            "bounce_charge": inr(l.get("bounce_charge", 0)),
            "penal_interest": "2% per month over the contracted rate on the overdue amount",
            "cibil_impact": "Reported to CIBIL if unpaid past 30 days from the due date.",
            "fix": "Use pay_overdue_emi to clear it now and stop further penal interest.",
        }
    return out


def get_amortization_summary(loan_ref: str) -> dict:
    """Show how much of the loan is principal vs interest, and what is left to pay."""
    l = BANK.loan(loan_ref)
    if not l:
        return not_found("loan", loan_ref)
    total_payable = l["emi"] * l["tenure_months"]
    paid = l["emi"] * l["emis_paid"]
    return {
        "ok": True,
        "loan_id": l["loan_id"],
        "type": l["type"],
        "principal_sanctioned": inr(l["sanctioned"]),
        "principal_outstanding": inr(l["outstanding"]),
        "principal_repaid": inr(l["sanctioned"] - l["outstanding"]),
        "total_payable_over_tenure": inr(total_payable),
        "total_interest_over_tenure": inr(total_payable - l["sanctioned"]),
        "paid_so_far": inr(paid),
        "remaining_to_pay": inr(total_payable - paid),
        "emis_paid": l["emis_paid"],
        "emis_remaining": l["emis_remaining"],
        "note": "Early in a loan most of each EMI is interest; that flips as the tenure progresses.",
    }


def pay_overdue_emi(loan_ref: str, from_account: str) -> dict:
    """Clear an overdue EMI plus bounce charges from an account. Requires verified OTP."""
    l = BANK.loan(loan_ref)
    if not l:
        return not_found("loan", loan_ref)
    if not l["overdue_amount"]:
        return {"ok": True, "nothing_due": True, "loan_id": l["loan_id"], "status": l["status"]}
    a = BANK.account(from_account)
    if not a:
        return not_found("account", from_account)
    if not BANK.otp_ok():
        return need_otp("overdue EMI payment")

    total = l["overdue_amount"] + l.get("bounce_charge", 0)
    if total > a["available_balance"]:
        return {
            "ok": False,
            "error": "insufficient_funds",
            "message": f"{inr(total)} is due but the account has {inr(a['available_balance'])}.",
        }

    a["balance"] -= total
    a["available_balance"] = a["balance"]
    l["outstanding"] -= l["overdue_amount"] * 0.4  # rough principal component
    l["overdue_amount"] = 0.0
    l["emis_paid"] += 1
    l["emis_remaining"] -= 1
    l["status"] = "Regular"
    l.pop("bounce_reason", None)
    BANK.consume_otp()
    ref = BANK.ref("EMI")
    BANK.log("overdue_emi_paid", f"{l['loan_id']} {inr(total)}", ref=ref)
    return {
        "ok": True,
        "reference": ref,
        "loan_id": l["loan_id"],
        "paid": inr(total),
        "breakup": {"emi": inr(total - l.get("bounce_charge", 0)), "bounce_charge": inr(l.get("bounce_charge", 0))},
        "loan_status": "Regular",
        "next_due": l["next_due"],
        "new_account_balance": inr(a["balance"]),
        "cibil": "No adverse reporting — cleared within 30 days of the due date.",
        "note": "The bounce charge can be waived once per loan as a goodwill gesture — use waive_charge if the customer asks.",
    }


def get_foreclosure_quote(loan_ref: str) -> dict:
    """Calculate what it costs to close a loan today: outstanding principal, foreclosure charges and total payable."""
    l = BANK.loan(loan_ref)
    if not l:
        return not_found("loan", loan_ref)
    pct = l.get("foreclosure_charges_pct", 0.0)
    charge = round(l["outstanding"] * pct / 100, 2)
    return {
        "ok": True,
        "loan_id": l["loan_id"],
        "type": l["type"],
        "outstanding_principal": inr(l["outstanding"]),
        "foreclosure_charge": inr(charge) + f" ({pct}%)" if charge else "Nil",
        "charge_note": (
            "Floating-rate home loans to individuals carry nil foreclosure charges under RBI rules."
            if pct == 0
            else f"{pct}% of outstanding, as per the sanction letter."
        ),
        "total_payable_today": inr(l["outstanding"] + charge),
        "quote_valid_till": days_from_now(7),
        "interest_saved": inr(max(0, l["emi"] * l["emis_remaining"] - l["outstanding"])),
        "documents_released": "Original property papers / NOC within 15 working days of payment.",
        "next_step": "Confirm and the payment link plus a formal foreclosure letter go to the registered email.",
    }


def request_loan_document(loan_ref: str, document_type: str) -> dict:
    """Request a loan document. document_type: 'interest certificate', 'provisional certificate', 'statement of account', 'noc', 'sanction letter', 'amortisation schedule'."""
    l = BANK.loan(loan_ref)
    if not l:
        return not_found("loan", loan_ref)
    instant = any(w in (document_type or "").lower() for w in ("interest", "provisional", "statement", "amort"))
    ref = BANK.ref("DOC")
    BANK.log("loan_document", f"{l['loan_id']} — {document_type}", ref=ref)
    return {
        "ok": True,
        "reference": ref,
        "loan_id": l["loan_id"],
        "document": document_type,
        "delivery": "Emailed instantly" if instant else "Emailed within 7 working days",
        "sent_to": BANK.customer["email"],
        "charge": "Free",
        "expected_by": today().isoformat() if instant else working_days_from_now(7),
    }


def change_emi_date(loan_ref: str, new_day_of_month: int) -> dict:
    """Change the day of the month an EMI is debited (e.g. to align with salary credit)."""
    l = BANK.loan(loan_ref)
    if not l:
        return not_found("loan", loan_ref)
    if not 1 <= new_day_of_month <= 28:
        return {"ok": False, "error": "bad_date", "message": "EMI date must be between the 1st and 28th."}
    ref = BANK.ref("SR")
    BANK.log("emi_date_change", f"{l['loan_id']} -> day {new_day_of_month}", ref=ref)
    return {
        "ok": True,
        "reference": ref,
        "loan_id": l["loan_id"],
        "new_emi_day": new_day_of_month,
        "effective_from": "The EMI cycle after next (a fresh NACH mandate has to register)",
        "tat": "15 working days for the mandate to activate",
        "charge": "Free, once per financial year",
        "warning": "Keep the existing mandate funded until the new one activates, or the EMI will bounce.",
    }


def get_preapproved_offers() -> dict:
    """List pre-approved loan, credit limit and balance-transfer offers for this customer."""
    offers = BANK.customer.get("offers", [])
    if not offers:
        return {"ok": True, "count": 0, "message": "No pre-approved offers on this customer right now."}
    return {"ok": True, "count": len(offers), "offers": offers}


def apply_for_loan(loan_type: str, amount: float, tenure_months: int) -> dict:
    """Start a loan application. Checks pre-approved offers first — those disburse same-day with no documents."""
    for o in BANK.customer.get("offers", []):
        if loan_type.lower() in o.get("type", "").lower() and amount <= o.get("amount", 0):
            r = o["rate"] / 1200
            n = tenure_months or o["tenure_months"]
            emi = round(amount * r * (1 + r) ** n / ((1 + r) ** n - 1), 2)
            ref = BANK.ref("LAP")
            BANK.log("loan_applied_preapproved", f"{loan_type} {inr(amount)}/{n}m", ref=ref)
            return {
                "ok": True,
                "reference": ref,
                "pre_approved": True,
                "loan_type": o["type"],
                "amount": inr(amount),
                "rate": f"{o['rate']}% p.a.",
                "tenure_months": n,
                "emi": inr(emi),
                "processing_fee": inr(round(amount * 0.01, 2)),
                "documents_required": "None — pre-approved on existing relationship.",
                "disbursal": "Same day, to the primary account, once the customer accepts.",
                "offer_valid_till": o["valid_till"],
            }

    ref = BANK.ref("LAP")
    BANK.log("loan_applied", f"{loan_type} {inr(amount)}/{tenure_months}m", ref=ref)
    return {
        "ok": True,
        "reference": ref,
        "pre_approved": False,
        "loan_type": loan_type,
        "amount_requested": inr(amount),
        "tenure_months": tenure_months,
        "status": "Application logged — credit assessment pending",
        "documents_required": [
            "PAN and Aadhaar",
            "Last 3 months' salary slips or last 2 years' ITR",
            "Last 6 months' bank statement (not needed for Axis salary accounts)",
        ],
        "tat": "In-principle decision in 2 working days after documents",
        "indicative_rate": "10.49% - 21% p.a. depending on the credit profile",
    }


# --------------------------------------------------------------------------- #
# Deposits
# --------------------------------------------------------------------------- #


def list_deposits() -> dict:
    """List the customer's fixed and recurring deposits with maturity dates and amounts."""
    return {
        "ok": True,
        "deposits": [
            {
                "deposit_id": d["deposit_id"],
                "type": d["type"],
                "principal": inr(d.get("principal", d.get("monthly_installment", 0))),
                "rate": f"{d['rate']}% p.a.",
                "opened": d["opened"],
                "maturity": d["maturity"],
                "maturity_amount": inr(d["maturity_amount"]),
                "status": d["status"],
            }
            for d in BANK.customer["deposits"]
        ],
    }


def get_deposit_rates(tenure_months: int) -> dict:
    """Get the applicable FD/RD interest rate for a tenure in months, including the senior-citizen rate."""
    tenure = tenure_months if tenure_months > 0 else 12
    rate = _rate_for(tenure)
    return {
        "ok": True,
        "tenure_months": tenure,
        "general_rate": f"{rate}% p.a.",
        "senior_citizen_rate": f"{round(rate + 0.5, 2)}% p.a.",
        "tax_saver_fd": "7.10% p.a. with a 5-year lock-in, deduction under Section 80C",
        "note": "Interest is compounded quarterly. TDS applies above ₹40,000 interest a year (₹50,000 for seniors).",
    }


def book_fixed_deposit(amount: float, tenure_months: int, from_account: str) -> dict:
    """Book a new fixed deposit from a savings account. Requires verified OTP."""
    a = BANK.account(from_account)
    if not a:
        return not_found("account", from_account)
    if not BANK.otp_ok():
        return need_otp("FD booking")
    if amount < 5000:
        return {"ok": False, "error": "below_minimum", "message": "Minimum fixed deposit is ₹5,000."}
    if amount > a["available_balance"]:
        return {"ok": False, "error": "insufficient_funds", "message": f"Available balance {inr(a['available_balance'])}."}

    rate = _rate_for(tenure_months)
    maturity_amount = round(amount * (1 + rate / 400) ** (tenure_months / 3), 2)
    a["balance"] -= amount
    a["available_balance"] = a["balance"]
    fd = {
        "deposit_id": BANK.ref("FD"),
        "type": "Fixed Deposit",
        "principal": amount,
        "rate": rate,
        "opened": today().isoformat(),
        "maturity": days_from_now(int(tenure_months * 30.44)),
        "maturity_amount": maturity_amount,
        "tenure": f"{tenure_months} months",
        "status": "Active",
        "payout": "On maturity",
        "auto_renew": True,
        "premature_penalty_pct": 1.0,
    }
    BANK.customer["deposits"].append(fd)
    BANK.consume_otp()
    BANK.log("fd_booked", f"{inr(amount)} for {tenure_months}m @ {rate}%", ref=fd["deposit_id"])
    return {
        "ok": True,
        "deposit_id": fd["deposit_id"],
        "principal": inr(amount),
        "rate": f"{rate}% p.a.",
        "tenure_months": tenure_months,
        "maturity_date": fd["maturity"],
        "maturity_amount": inr(maturity_amount),
        "interest_earned": inr(maturity_amount - amount),
        "debited_from": mask_account(a["account_no"]),
        "new_account_balance": inr(a["balance"]),
        "auto_renew": True,
        "advice": "The FD advice has been emailed to the registered address.",
    }


def get_premature_closure_quote(deposit_ref: str) -> dict:
    """Calculate what a deposit is worth if broken today, after the premature-withdrawal penalty."""
    d = BANK.deposit(deposit_ref)
    if not d:
        return not_found("deposit", deposit_ref)
    penalty = d.get("premature_penalty_pct", 1.0)
    effective = round(d["rate"] - penalty, 2)
    principal = d.get("principal") or d.get("monthly_installment", 0) * d.get("installments_paid", 0)
    payout = round(principal * 1.03, 2)  # indicative accrued interest at the reduced rate
    return {
        "ok": True,
        "deposit_id": d["deposit_id"],
        "type": d["type"],
        "contracted_rate": f"{d['rate']}% p.a.",
        "penalty": f"{penalty}%",
        "effective_rate_on_closure": f"{effective}% p.a.",
        "principal": inr(principal),
        "indicative_payout_today": inr(payout),
        "if_held_to_maturity": inr(d["maturity_amount"]),
        "you_would_forgo": inr(d["maturity_amount"] - payout),
        "maturity_date": d["maturity"],
        "alternative": (
            "An overdraft against the deposit gives up to 90% of the value at the FD rate + 1%, "
            "and keeps the deposit and its interest intact. Worth offering before breaking it."
        ),
    }


def close_deposit(deposit_ref: str, credit_to_account: str) -> dict:
    """Prematurely close a deposit and credit the proceeds to an account. Requires verified OTP."""
    d = BANK.deposit(deposit_ref)
    if not d:
        return not_found("deposit", deposit_ref)
    a = BANK.account(credit_to_account)
    if not a:
        return not_found("account", credit_to_account)
    if not BANK.otp_ok():
        return need_otp("premature deposit closure")

    principal = d.get("principal") or d.get("monthly_installment", 0) * d.get("installments_paid", 0)
    payout = round(principal * 1.03, 2)
    a["balance"] += payout
    a["available_balance"] = a["balance"]
    d["status"] = "Closed prematurely"
    BANK.consume_otp()
    ref = BANK.ref("CLS")
    BANK.log("deposit_closed", f"{d['deposit_id']} paid out {inr(payout)}", ref=ref)
    return {
        "ok": True,
        "reference": ref,
        "deposit_id": d["deposit_id"],
        "amount_credited": inr(payout),
        "credited_to": mask_account(a["account_no"]),
        "new_account_balance": inr(a["balance"]),
        "penalty_applied": f"{d.get('premature_penalty_pct', 1.0)}% on the applicable rate",
        "credited_when": "Instantly",
        "tds": "TDS, if any, has been deducted and will reflect in Form 26AS.",
    }
