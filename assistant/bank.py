"""Mock core banking system.

A single in-memory store standing in for CBS + card management + LOS + CRM.
Every tool in `assistant/tools/` reads and writes through here, so the demo has
real state: block a card and it stays blocked, transfer money and the balance moves.

Reset (or a config edit + Reload) restores everything from
`config/data/customers.json`.
"""

from __future__ import annotations

import copy
import itertools
import random
from datetime import date, datetime, timedelta
from typing import Any

from . import config

# --------------------------------------------------------------------------- #
# Store
# --------------------------------------------------------------------------- #


class Bank:
    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.customers: list[dict] = copy.deepcopy(config.seed_customers())
        self.active_id: str = self.customers[0]["id"]
        self.audit: list[dict] = []
        self.otp: dict[str, Any] | None = None
        self._seq = itertools.count(1)

    # -- customer -------------------------------------------------------- #

    @property
    def customer(self) -> dict:
        for c in self.customers:
            if c["id"] == self.active_id:
                return c
        return self.customers[0]

    def switch_customer(self, customer_id: str) -> dict:
        for c in self.customers:
            if c["id"] == customer_id:
                self.active_id = customer_id
                self.otp = None
                return c
        raise ValueError(f"No such customer: {customer_id}")

    def roster(self) -> list[dict]:
        return [
            {"id": c["id"], "name": c["name"], "segment": c["segment"], "branch": c["home_branch"]}
            for c in self.customers
        ]

    # -- lookups ---------------------------------------------------------- #

    def account(self, account_no: str) -> dict | None:
        """Match on full number or last-4/partial, so the model can be sloppy."""
        accounts = self.customer["accounts"]
        if not account_no:
            return accounts[0] if accounts else None
        key = account_no.replace(" ", "").replace("X", "").replace("x", "")
        for a in accounts:
            if a["account_no"] == account_no or (key and a["account_no"].endswith(key)):
                return a
        return None

    def card(self, card_ref: str) -> dict | None:
        """Match on card_id, last 4 digits, or a product/kind word ('credit')."""
        cards = self.customer["cards"]
        if not card_ref:
            return cards[0] if cards else None
        ref = card_ref.strip().lower().replace(" ", "").replace("x", "")
        for c in cards:
            if c["card_id"].lower() == card_ref.strip().lower() or c["last4"] == ref[-4:]:
                return c
        for c in cards:
            if ref in c["product"].lower() or ref in c["kind"].lower():
                return c
        return None

    def loan(self, loan_ref: str) -> dict | None:
        loans = self.customer["loans"]
        if not loan_ref:
            return loans[0] if loans else None
        ref = loan_ref.strip().lower()
        for l in loans:
            if l["loan_id"].lower() == ref or ref in l["type"].lower():
                return l
        return None

    def deposit(self, deposit_ref: str) -> dict | None:
        deposits = self.customer["deposits"]
        if not deposit_ref:
            return deposits[0] if deposits else None
        ref = deposit_ref.strip().lower()
        for d in deposits:
            if d["deposit_id"].lower() == ref or ref in d["type"].lower():
                return d
        return None

    def transaction(self, txn_ref: str) -> dict | None:
        ref = (txn_ref or "").strip().lower()
        for t in self.customer["transactions"]:
            if t["id"].lower() == ref:
                return t
        # fall back to a description / merchant / RRN match
        for t in self.customer["transactions"]:
            hay = f"{t['description']} {t.get('merchant', '')} {t.get('rrn', '')}".lower()
            if ref and ref in hay:
                return t
        return None

    # -- reference numbers ------------------------------------------------ #

    def ref(self, prefix: str) -> str:
        return f"{prefix}{datetime.now():%Y%m%d}{next(self._seq):04d}"

    # -- audit ------------------------------------------------------------ #

    def log(self, action: str, detail: str, **extra: Any) -> None:
        self.audit.append(
            {
                "at": datetime.now().strftime("%H:%M:%S"),
                "action": action,
                "detail": detail,
                "customer": self.customer["name"],
                **extra,
            }
        )

    # -- OTP -------------------------------------------------------------- #

    DEMO_OTP = "123456"

    def issue_otp(self, purpose: str) -> str:
        self.otp = {
            "code": self.DEMO_OTP,
            "purpose": purpose,
            "expires": datetime.now() + timedelta(minutes=5),
            "verified": False,
            "attempts": 0,
        }
        return self.DEMO_OTP

    def check_otp(self, code: str) -> tuple[bool, str]:
        if not self.otp:
            return False, "No OTP was requested. Send one first."
        if datetime.now() > self.otp["expires"]:
            return False, "OTP expired. Please request a new one."
        self.otp["attempts"] += 1
        if self.otp["attempts"] > 3:
            self.otp = None
            return False, "Too many incorrect attempts. OTP cancelled for security."
        if code.strip() != self.otp["code"]:
            return False, f"Incorrect OTP. {4 - self.otp['attempts']} attempt(s) left."
        self.otp["verified"] = True
        return True, f"OTP verified for {self.otp['purpose']}."

    def otp_ok(self, purpose_hint: str = "") -> bool:
        return bool(self.otp and self.otp.get("verified"))

    def consume_otp(self) -> None:
        self.otp = None


BANK = Bank()


# --------------------------------------------------------------------------- #
# Shared helpers used by tools
# --------------------------------------------------------------------------- #


def inr(amount: float) -> str:
    """Indian comma grouping: 1234567.5 -> '₹12,34,567.50'."""
    neg = amount < 0
    whole, frac = divmod(round(abs(amount) * 100), 100)
    s = str(whole)
    if len(s) > 3:
        head, tail = s[:-3], s[-3:]
        parts = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        s = ",".join(parts + [tail])
    return f"{'-' if neg else ''}₹{s}.{frac:02d}"


def mask_account(account_no: str) -> str:
    return "X" * max(0, len(account_no) - 4) + account_no[-4:]


def mask_card(last4: str) -> str:
    return f"XXXX XXXX XXXX {last4}"


def today() -> date:
    return date.today()


def working_days_from_now(n: int) -> str:
    d = today()
    added = 0
    while added < n:
        d += timedelta(days=1)
        if d.weekday() < 5:
            added += 1
    return d.isoformat()


def days_from_now(n: int) -> str:
    return (today() + timedelta(days=n)).isoformat()


def need_otp(purpose: str) -> dict:
    """Uniform refusal every sensitive tool returns when OTP is missing."""
    return {
        "ok": False,
        "error": "otp_required",
        "message": (
            f"This action ({purpose}) needs OTP verification. "
            "Call send_otp, ask the customer for the 6-digit code, then call verify_otp."
        ),
    }


def not_found(what: str, ref: str) -> dict:
    return {"ok": False, "error": "not_found", "message": f"No {what} found matching '{ref}'."}
