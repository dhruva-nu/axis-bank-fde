"""How each tool parameter gets its value without an LLM writing it.

Every parameter becomes a choice. The options come from code — the customer's own
records, a fixed list, or the words they typed cut into pieces (extract.py) — and Jev
picks between them. A value the customer typed is never read out by a pattern: Jev is
always asked which piece it is, with "none of these" as an option. The kinds:

    Entity   one of the customer's records (account, card, loan, transaction, payee …)
    Options  a fixed list (limit types, card features, complaint categories, KB articles …)
    YesNo    a boolean
    Number   Jev picks which number they typed it is, and the unit (₹ / lakh, months / years)
    Span     Jev picks which word or run of words they typed it is (IFSC, a name, an OTP …)
    Text     the customer's own words, passed through verbatim
    Fixed    a constant (optional fields the customer never needs to give)

Slots are matched on parameter name (BY_NAME), with OVERRIDES where the same name means
something different in one tool. A tool whose parameters are not all covered is left out
of routing, so auto-discovery stays safe: a new tool with familiar parameter names just
works, and one with unfamiliar names is skipped rather than called with garbage.
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass, field
from typing import Any, Callable

from jev import noul
from jev import choice as jev_choice

from .. import config
from ..bank import BANK, inr, today
from . import extract

UNSET: Any = object()    # no value could be settled
NOT_GIVEN: Any = object()  # Jev says the customer has not said it

# Jev's pick is treated as unsure — and the customer asked — when its probability is below
# MIN_P or its confidence below MIN_CONFIDENCE. Probability alone is not enough: between two
# loans, 0.51 / 0.49 clears 0.5 while being a coin flip (confidence 0.02).
MIN_P = 0.5
MIN_CONFIDENCE = 0.3

# Tools Jev never routes to: OTP is driven by the dialogue itself, and the date tools are
# out of scope for now.
EXCLUDED = {"send_otp", "verify_otp", "email_statement", "request_callback", "book_appointment"}


@dataclass
class Ctx:
    message: str             # the customer's latest message
    earlier: list[str]       # their previous messages, oldest first
    conversation: list[str]  # "customer: …" / "assistant: …", oldest first
    action: str              # the tool being filled, as "name — docstring"
    answering: bool = False  # the message replies to our question about this slot


@dataclass
class Plan:
    value: Any = UNSET
    question: Any = None                                 # a Jev question class
    values: dict[str, Any] = field(default_factory=dict)  # label -> value
    descriptions: dict[str, str] = field(default_factory=dict)
    fallback: Any = UNSET                                # used when Jev is unsure
    is_noul: bool = False
    scale: Any = None                                    # a second question: the unit a number was given in
    scales: dict[str, float] = field(default_factory=dict)  # its label -> multiplier
    convert: Callable[[Any, float], Any] | None = None   # chosen piece (+ multiplier) -> the tool's value

    @property
    def questions(self) -> list:
        return [q for q in (self.question, self.scale) if q is not None]


class Slot:
    ask = ""

    def plan(self, name: str, ctx: Ctx) -> Plan:
        raise NotImplementedError


def _label(hint: Any, taken: set[str]) -> str:
    kept = "".join(ch if ch.isascii() and ch.isalnum() else " " for ch in str(hint).lower())
    base = "_".join(kept.split())[:40] or "option"
    if base[0].isdigit():
        base = f"o_{base}"
    label, n = base, 2
    while label in taken:
        label, n = f"{base}_{n}", n + 1
    taken.add(label)
    return label


def _choice_plan(name: str, instructions: str, items: list[tuple[Any, str, str]], fallback: Any = UNSET) -> Plan:
    """items: (value, label hint, description). One item needs no question."""
    if len(items) == 1 and items[0][0] is not NOT_GIVEN:
        return Plan(value=items[0][0])
    plan = Plan(fallback=fallback)
    taken: set[str] = set()
    for value, hint, desc in items:
        label = _label(hint, taken)
        plan.values[label] = value
        plan.descriptions[label] = desc
    plan.question = jev_choice(f"p_{name}", instructions, dict(plan.descriptions))
    return plan


# --------------------------------------------------------------------------- #
# Kinds
# --------------------------------------------------------------------------- #


def _entities(kind: str) -> list[tuple[Any, str, str]]:
    c = BANK.customer
    if kind == "account":
        return [
            (a["account_no"], f"acct_{a['account_no'][-4:]}",
             f"{a['type']} account ending {a['account_no'][-4:]}, balance {inr(a['balance'])}"
             + (" (primary account)" if i == 0 else ""))
            for i, a in enumerate(c["accounts"])
        ]
    if kind in ("card", "credit_card"):
        cards = [x for x in c["cards"] if kind == "card" or x["kind"] == "Credit"]
        return [
            (x["card_id"], x["card_id"],
             f"{x['product']} — {x['kind'].lower()} card, {x['network']}, ending {x['last4']}, {x['status']}")
            for x in cards
        ]
    if kind == "loan":
        return [
            (l["loan_id"], l["loan_id"],
             f"{l['type']} {l['loan_id']}, EMI {inr(l['emi'])}, outstanding {inr(l['outstanding'])}, {l['status']}"
             + (f", overdue {inr(l['overdue_amount'])}" if l["overdue_amount"] else ""))
            for l in c["loans"]
        ]
    if kind == "deposit":
        return [
            (d["deposit_id"], d["deposit_id"],
             f"{d['type']} {d['deposit_id']}, matures {d['maturity']} at {inr(d['maturity_amount'])}, {d['status']}")
            for d in c["deposits"]
        ]
    if kind == "txn":
        return [
            (t["id"], t["id"],
             f"{t['date']} · {t['description']}" + (f" ({t['merchant']})" if t.get("merchant") else "")
             + f" · {inr(t['amount'])} {t['type']} via {t['channel']} · {t['status']}"
             + (f" · {t['note']}" if t.get("note") else ""))
            for t in c["transactions"]
        ]
    if kind == "beneficiary":
        return [
            (b["id"], b["nickname"] or b["name"],
             f"{b['name']} (nickname '{b['nickname']}'), {b['bank']} account ending {b['account_no'][-4:]}")
            for b in c["beneficiaries"]
        ]
    if kind == "si":
        return [
            (s["si_id"], s["si_id"], f"{s['description']} — {inr(s['amount'])}, {s['frequency']}, {s['status']}")
            for s in c["standing_instructions"]
        ]
    if kind == "dispute":
        return [
            (d["dispute_id"], d["dispute_id"], f"{d['dispute_id']}: {d['description']} {inr(d['amount'])} — {d['status']}")
            for d in c["disputes"]
        ]
    if kind == "sr":
        return [
            (s["sr_no"], s["sr_no"], f"{s['sr_no']}: {s['category']} — {s['description']} ({s['status']})")
            for s in c["service_requests"]
        ]
    raise ValueError(kind)


_ENTITY_WORDS = {
    "account": "account", "card": "card", "credit_card": "credit card", "loan": "loan",
    "deposit": "deposit", "txn": "transaction", "beneficiary": "payee",
    "si": "standing instruction", "dispute": "dispute", "sr": "service request",
}


class Entity(Slot):
    """One of the customer's own records. `optional` = the tool accepts '' (primary / all)."""

    def __init__(self, kind: str, optional: bool = False) -> None:
        self.kind, self.optional = kind, optional
        self.ask = f"which {_ENTITY_WORDS[kind]}"

    def plan(self, name: str, ctx: Ctx) -> Plan:
        items = _entities(self.kind)
        if not items:
            return Plan(value="")  # nothing to choose; the tool reports not-found itself
        if self.optional:
            items.append(("", "unspecified", f"the customer did not single out one {_ENTITY_WORDS[self.kind]} "
                          "(means the primary/default one, or all of them)"))
        elif len(items) == 1:
            return Plan(value=items[0][0])
        return _choice_plan(
            name,
            f"Which of the customer's {_ENTITY_WORDS[self.kind]}s does their message refer to, "
            f"for this action: {ctx.action}?",
            items,
            fallback="" if self.optional else UNSET,
        )


class Options(Slot):
    """A fixed list: {value: description}, or a callable returning one."""

    def __init__(self, options: dict | Callable[[], dict], question: str, default: Any = UNSET, ask: str = "") -> None:
        self.options, self.question, self.default = options, question, default
        self.ask = ask or question

    def plan(self, name: str, ctx: Ctx) -> Plan:
        opts = self.options() if callable(self.options) else self.options
        items = [(v, str(v), d) for v, d in opts.items()]
        if self.default is not UNSET and self.default not in opts:
            items.append((self.default, "not_specified", "the customer did not say"))
        return _choice_plan(name, f"{self.question} (for: {ctx.action})", items, fallback=self.default)


class YesNo(Slot):
    def __init__(self, question: str, true_means: str, false_means: str) -> None:
        self.question, self.true_means, self.false_means = question, true_means, false_means

    def plan(self, name: str, ctx: Ctx) -> Plan:
        return Plan(question=noul(f"p_{name}", self.question, self.true_means, self.false_means), is_noul=True)


def _said(ctx: Ctx, cut: Callable[[str], list[extract.Piece]], earlier: bool = False) -> list[tuple[Any, str, str]]:
    """The pieces of what the customer typed, as Choice items — the latest message first,
    then (if `earlier`) their two messages before it."""
    sources = [("latest message", ctx.message)]
    if earlier:
        sources += [("an earlier message", t) for t in reversed(ctx.earlier[-2:])]
    items, seen = [], set()
    for where, text in sources:
        for piece in cut(text):
            if piece.text.lower() not in seen:
                seen.add(piece.text.lower())
                items.append((piece, piece.text, f"'{piece.text}' — {where}: {piece.context}"))
    return items


# The unit a number was given in is its own question, asked in the same Jev request.
_SCALES: dict[str, tuple[str, dict[str, tuple[str, float]]]] = {
    "money": ("What unit did the customer give this amount in?", {
        "rupees": ("plain rupees, as written (500, ₹5,000, Rs 2500, 5000/-)", 1),
        "thousand": ("thousands (5k, 5 thousand)", 1e3),
        "lakh": ("lakhs (1.5 lakh, 2 lacs, 2L)", 1e5),
        "crore": ("crores (1 crore, 2 cr)", 1e7),
    }),
    "months": ("Is this period given in months or in years?", {
        "months": ("months", 1),
        "years": ("years", 12),
    }),
}


class Number(Slot):
    """A number the customer typed. Jev picks which number-bearing word it is (or none of
    them), and — for money and tenures — the unit, in the same request."""

    def __init__(self, unit: str, ask: str = "", default: Any = UNSET, lo: float = 0, hi: float = float("inf")) -> None:
        self.unit, self.ask, self.default, self.lo, self.hi = unit, ask, default, lo, hi

    def plan(self, name: str, ctx: Ctx) -> Plan:
        # Earlier messages count too ("FD for 2 years" … "make it 1 lakh"); Jev sees which is which.
        items = _said(ctx, lambda t: extract.words(t, with_digits=True), earlier=True)
        if not items:
            return Plan(value=self.default)
        items.append((NOT_GIVEN, "not_given", f"none of these is {self.ask}"))
        plan = _choice_plan(
            name,
            f"The bank needs {self.ask} for this action: {ctx.action}. "
            "Which of these numbers the customer wrote is it? Pick 'not_given' if they have not said it.",
            items,
            fallback=self.default,
        )
        if self.unit in _SCALES:
            question, scales = _SCALES[self.unit]
            plan.scale = jev_choice(f"u_{name}", f"{question} (It is {self.ask}.)", {k: d for k, (d, _) in scales.items()})
            plan.scales = {k: m for k, (_, m) in scales.items()}
        plan.convert = self._convert
        return plan

    def _convert(self, piece: extract.Piece, multiplier: float) -> Any:
        n = extract.number(piece.text)
        if n is None:
            return UNSET
        value = n * multiplier
        value = int(value) if float(value).is_integer() else round(value, 2)
        return value if self.lo <= value <= self.hi else UNSET


def _otp_pieces(text: str) -> list[extract.Piece]:
    pieces = extract.words(text, with_digits=True)
    together = extract.digits(text)
    if len(pieces) > 1 and len(together) >= 4:  # read out with spaces: '1 2 3 4 5 6'
        pieces.append(extract.Piece(together, "all the digits in the message, read together"))
    return pieces


# kind -> (how the message is cut into options, how the chosen piece becomes the value)
_SPANS: dict[str, tuple[Callable[[str], list[extract.Piece]], Callable[[str], str]]] = {
    "otp": (_otp_pieces, extract.digits),
    "ifsc": (extract.words, str.upper),
    "account_number": (lambda t: extract.words(t, with_digits=True), extract.digits),
    "cheque": (lambda t: extract.words(t, with_digits=True), extract.digits),
    "name": (lambda t: extract.runs(t, 4), str.strip),
    "contact": (lambda t: extract.words(t) + extract.tails(t, 3), str.strip),
}


class Span(Slot):
    """A value the customer typed — an IFSC, a name, an OTP. The message is cut into words
    (or runs of words) and Jev picks which one it is, or none of them."""

    def __init__(self, kind: str, ask: str) -> None:
        self.kind, self.ask = kind, ask

    def plan(self, name: str, ctx: Ctx) -> Plan:
        cut, clean = _SPANS[self.kind]
        items = _said(ctx, cut)
        whole = extract.trim(ctx.message)
        if ctx.answering and whole and all(whole.lower() != str(h).lower() for _, h, _ in items):
            # They are replying to our question for this: the whole reply may be the value.
            items.append((extract.Piece(whole, "the customer's whole reply"), whole, f"their whole reply: '{whole}'"))
        if not items:
            return Plan()
        items.append((NOT_GIVEN, "not_given", f"none of these is {self.ask}"))
        plan = _choice_plan(
            name,
            f"The bank needs {self.ask} for this action: {ctx.action}. "
            "Which part of what the customer wrote is it, exactly? Pick 'not_given' if they have not said it.",
            items,
        )
        plan.convert = lambda piece, _: clean(piece.text) or UNSET
        return plan


class Text(Slot):
    def __init__(self, source: str = "message") -> None:
        self.source = source

    def plan(self, name: str, ctx: Ctx) -> Plan:
        if self.source == "conversation":
            return Plan(value="\n".join(ctx.conversation[-8:] + [f"customer: {ctx.message}"]))
        return Plan(value=ctx.message)


class Fixed(Slot):
    def __init__(self, value: Any) -> None:
        self.value = value

    def plan(self, name: str, ctx: Ctx) -> Plan:
        return Plan(value=self.value)


# --------------------------------------------------------------------------- #
# Runtime option lists
# --------------------------------------------------------------------------- #


def _kb_articles() -> dict:
    return {a["title"]: f"{a['title']} — {', '.join(a['tags'][:8])}" for a in config.knowledge()["articles"]}


def _complaint_categories() -> dict:
    return {k: k for k in config.knowledge()["complaint_categories"]}


def _cities() -> dict:
    return {city: city for city in sorted({b["city"] for b in config.knowledge()["branches"]})}


def _financial_years() -> dict:
    d = today()
    start = d.year if d.month >= 4 else d.year - 1
    out = {}
    for y in (start, start - 1, start - 2):
        fy = f"{y}-{str(y + 1)[-2:]}"
        out[fy] = f"FY {fy} (April {y} – March {y + 1})" + (" — the current year" if y == start else "")
    return out


def _txn_facets() -> dict:
    """search_transactions does a substring match, so its query is a merchant, channel or status."""
    out: dict[str, str] = {}
    for t in BANK.customer["transactions"]:
        if t.get("merchant"):
            out.setdefault(t["merchant"].lower(), f"merchant {t['merchant']}")
    for t in BANK.customer["transactions"]:
        out.setdefault(t["channel"].lower(), f"{t['channel']} transactions")
    out.setdefault("failed", "failed transactions")
    out.setdefault("pending", "pending transactions")
    return out


# --------------------------------------------------------------------------- #
# The table
# --------------------------------------------------------------------------- #

BY_NAME: dict[str, Slot] = {
    "account_no": Entity("account", optional=True),
    "from_account": Entity("account", optional=True),
    "credit_to_account": Entity("account", optional=True),
    "card_ref": Entity("card"),
    "loan_ref": Entity("loan"),
    "deposit_ref": Entity("deposit"),
    "txn_ref": Entity("txn"),
    "transaction_ref": Entity("txn"),
    "reference": Entity("txn"),
    "beneficiary": Entity("beneficiary"),
    "si_id": Entity("si"),
    "dispute_id": Entity("dispute", optional=True),
    "sr_number": Entity("sr", optional=True),
    "amount": Number("money", ask="the amount in rupees"),
    "requested_limit": Number("money", ask="the new credit limit they want"),
    "points": Number("points", ask="how many reward points to redeem"),
    "count": Number("count", ask="how many transactions to show", default=0, lo=1, hi=50),
    "new_day_of_month": Number("ordinal", ask="the day of the month (1–28) for the EMI debit", lo=1, hi=28),
    "tenure_months": Number("months", ask="the tenure", lo=1, hi=360),
    "enable": YesNo(
        "Does the customer want this card feature switched ON?",
        "they want it enabled / turned on / allowed",
        "they want it disabled / turned off / blocked",
    ),
    "leaves": Options({25: "25-leaf cheque book", 50: "50-leaf cheque book", 100: "100-leaf cheque book"},
                      "How many cheque leaves?", default=0),
    "limit_type": Options(
        {"atm_daily": "daily ATM cash withdrawal limit", "pos_daily": "daily in-store / POS / swipe limit",
         "ecom_daily": "daily online / e-commerce limit", "cash_limit": "credit card cash-advance limit"},
        "Which card limit does the customer want changed?", ask="which limit (ATM, in-store, online or cash)",
    ),
    "feature": Options(
        {"international": "international / overseas / foreign usage", "contactless": "contactless / tap-to-pay",
         "ecom": "online / e-commerce payments", "atm": "ATM withdrawals"},
        "Which card feature is this about?", ask="which feature (international, contactless, online or ATM)",
    ),
    "autopay_type": Options({"full": "pay the total amount due", "minimum": "pay only the minimum due"},
                            "Should autopay cover the full due or the minimum due?"),
    "document_type": Options(
        {"interest certificate": "interest certificate (for tax)", "provisional certificate": "provisional interest certificate",
         "statement of account": "loan statement of account", "noc": "NOC / no-objection / closure certificate",
         "sanction letter": "sanction letter", "amortisation schedule": "amortisation / repayment schedule"},
        "Which loan document does the customer want?",
    ),
    "loan_type": Options(
        {"Personal Loan": "personal loan", "Home Loan": "home loan / housing", "Car Loan": "car / auto / vehicle loan",
         "Education Loan": "education loan", "Gold Loan": "gold loan", "Loan Against Property": "loan against property"},
        "What kind of loan does the customer want?",
    ),
    "relationship": Options(
        {r: r for r in ("Spouse", "Father", "Mother", "Son", "Daughter", "Brother", "Sister", "Other")},
        "What is the nominee's relationship to the customer?",
    ),
    "field": Options({"mobile": "mobile / phone number", "email": "email address", "address": "postal address"},
                     "Which contact detail does the customer want to update?"),
    "value": Span("contact", ask="the new contact detail"),
    "financial_year": Options(_financial_years, "Which financial year is the certificate for?"),
    "category": Options(_complaint_categories, "Which complaint / service-request category fits best?"),
    "city": Options(_cities, "Which city is the customer asking about?", default=""),
    "requirement": Options(
        {"locker": "a locker", "cash deposit machine": "a cash deposit machine", "atm": "an ATM"},
        "Does the customer need a specific facility at the branch?", default="",
    ),
    "ifsc": Span("ifsc", ask="the payee's IFSC code"),
    "cheque_number": Span("cheque", ask="the 6-digit cheque number"),
    "name": Span("name", ask="the payee's full name"),
    "nominee_name": Span("name", ask="the nominee's full name"),
    "biller": Span("name", ask="which biller (electricity board, mobile operator, insurer…)"),
    "nickname": Fixed(""),
    "remarks": Fixed(""),
    "description": Text("message"),
    "reason": Text("message"),
    "conversation_summary": Text("conversation"),
}

OVERRIDES: dict[tuple[str, str], Slot] = {
    ("add_beneficiary", "account_no"): Span("account_number", ask="the payee's account number"),
    ("get_credit_card_bill", "card_ref"): Entity("credit_card"),
    ("pay_credit_card_bill", "card_ref"): Entity("credit_card"),
    ("setup_autopay", "card_ref"): Entity("credit_card"),
    ("request_credit_limit_increase", "card_ref"): Entity("credit_card"),
    ("withdraw_dispute", "dispute_id"): Entity("dispute"),
    ("block_card", "reason"): Options(
        {"lost": "the card is lost / missing", "stolen": "the card was stolen",
         "fraud": "fraud, misuse or transactions they did not make",
         "temporary block": "they just want it paused / temporarily blocked (card is safe)"},
        "Why does the customer want the card blocked?",
    ),
    ("reissue_card", "reason"): Options(
        {"damaged": "damaged / broken / chip not working", "worn out": "worn out / old",
         "name change": "name change", "lost": "lost card replacement", "stolen": "stolen card replacement"},
        "Why does the customer need a replacement card?",
    ),
    ("raise_dispute", "reason"): Options(
        {"not received": "goods or service not received", "duplicate charge": "charged twice",
         "wrong amount": "charged the wrong amount", "cancelled order": "order was cancelled but still charged",
         "unauthorised": "they did not make this transaction"},
        "What is the dispute about?",
    ),
    ("convert_transaction_to_emi", "tenure_months"): Options(
        {m: f"{m} months" for m in (3, 6, 9, 12, 18, 24)}, "Over how many months?", default=0,
    ),
    ("get_deposit_rates", "tenure_months"): Number("months", ask="the deposit tenure", default=0, lo=1, hi=120),
    ("apply_for_loan", "tenure_months"): Number("months", ask="the loan tenure", default=0, lo=1, hi=360),
    ("redeem_reward_points", "mode"): Options(
        {"statement_credit": "cash back on the card statement", "voucher": "vouchers / catalogue",
         "partner_transfer": "airline or hotel miles / partner transfer"},
        "How does the customer want to redeem?",
    ),
    ("transfer_funds", "mode"): Options({"IMPS": "IMPS (instant)", "NEFT": "NEFT", "RTGS": "RTGS (large amounts)"},
                                        "Which transfer channel did the customer ask for?", default=""),
    ("search_knowledge_base", "query"): Options(_kb_articles, "Which policy article answers the customer's question?"),
    ("search_transactions", "query"): Options(_txn_facets, "What is the customer searching their transactions for?"),
}


def slots_for(fn: Callable) -> dict[str, Slot] | None:
    """The slot for every parameter of `fn`, or None if any parameter is not covered."""
    out = {}
    for pname in inspect.signature(fn).parameters:
        slot = OVERRIDES.get((fn.__name__, pname)) or BY_NAME.get(pname)
        if slot is None:
            return None
        out[pname] = slot
    return out


@dataclass
class Missing:
    param: str
    ask: str
    options: list[str]  # Jev's top picks, when it was unsure between records


@dataclass
class Resolved:
    args: dict[str, Any]
    missing: list[Missing]
    trace: dict[str, Any]


def settle(plans: dict[str, Plan], slots: dict[str, Slot], answers: Any) -> Resolved:
    args, missing, trace = {}, [], {}
    for pname, plan in plans.items():
        if plan.question is None:
            value = plan.value
            if value is not UNSET:
                trace[pname] = {"value": value, "via": "direct"}
        elif plan.is_noul:
            p = answers[plan.question].probability
            value = p >= 0.5
            trace[pname] = {"value": value, "p_yes": round(p, 3)}
        else:
            ans = answers[plan.question]
            ranked = ans.ranked()
            value = plan.values[ans.choice]
            unsure = ans.probability < MIN_P or ans.confidence < MIN_CONFIDENCE
            unit = None
            if unsure or value is NOT_GIVEN:
                value = plan.fallback
            elif plan.convert:
                if plan.scale is not None:
                    unit = answers[plan.scale].choice
                value = plan.convert(value, plan.scales.get(unit, 1))
                if value is UNSET:  # not a usable value (e.g. outside the allowed range)
                    value = plan.fallback
            trace[pname] = {
                "value": None if value is UNSET else value,
                "top": {l: round(p, 3) for l, p in ranked[:3]},
                "confidence": round(ans.confidence, 3),
                **({"unit": unit} if unit else {}),
                **({"unsure": True} if unsure else {}),
            }
            if value is UNSET:
                options = [plan.descriptions[l] for l, _ in ranked[:2] if plan.values[l] is not NOT_GIVEN] if unsure else []
                missing.append(Missing(pname, slots[pname].ask, options))
                continue
        if value is UNSET:
            missing.append(Missing(pname, slots[pname].ask, []))
            continue
        args[pname] = value
    return Resolved(args, missing, trace)
