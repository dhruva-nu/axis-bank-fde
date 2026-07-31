"""The ADK agent.

Two things are deliberately dynamic:

* `instruction` is a callable, so `config/persona.md` is re-read on **every turn** —
  edit the prompt mid-conversation and the next reply obeys it.
* `build_agent()` re-scans `assistant/tools/`, so adding a tool needs only a Reload.
"""

from __future__ import annotations

import os

from google.adk.agents import Agent

from . import config
from .bank import BANK, inr, mask_account, mask_card, today
from .tools import load_tools

DEFAULT_MODEL = os.getenv("MODEL", "gemini-2.5-flash")

# Set per-request by server.py when the browser is going to read the reply aloud.
# A mutable dict rather than a bare bool so the flag can be flipped from outside
# the module without a rebind.
VOICE_MODE = {"on": False}

VOICE_HINT = (
    "## Voice call in progress\n"
    "The customer is **speaking** to you and will **hear** this reply read aloud. "
    "Every word costs about half a second of their time. So, for this turn only:\n"
    "- **Two or three sentences, 35 words maximum.** Answer the question, confirm what "
    "you did, stop. Do not add caveats, next steps or extra detail they did not ask for.\n"
    "- **Make the first sentence very short — under eight words.** It is spoken before "
    "the rest is even written, so a short opener is what stops the customer sitting in "
    "silence. 'Done — your card is blocked.' then the detail.\n"
    "- No markdown, no bullet lists, no tables, no headings — plain spoken sentences.\n"
    "- **Do not read out reference numbers, SR numbers, IDs or full account numbers.** "
    "Spoken aloud, 'BLK202607300001' takes four seconds and helps nobody. Say the "
    "reference is on their screen. Only read one out if they explicitly ask for it.\n"
    "- Refer to any card or account by its last four digits only, and never spell out an OTP.\n"
    "- Ask one question at a time and wait for the answer.\n"
    "- Expect the transcript to be imperfect. If a name or amount looks garbled, "
    "confirm it back instead of guessing."
)


def voice_hint() -> str:
    """The voice-mode instruction — overridable via branding.json → voice.agent_hint."""
    return config.branding().get("voice", {}).get("agent_hint") or VOICE_HINT


def customer_context() -> str:
    """A compact index of what this customer has, injected fresh on every turn.

    Not a data dump — just enough for the agent to know what exists and pick the
    right tool. Details come from the tools.
    """
    c = BANK.customer
    lines = [
        "## Customer context (live — regenerated every turn)",
        f"Today is {today():%d %b %Y}.",
        "",
        f"**{c['name']}** · {c['segment']} · CIF {c['cif']} · customer since {c['relationship_since']}",
        f"Mobile {c['mobile']} · {c['email']}",
        f"Home branch {c['home_branch']} · KYC: {c['kyc_status']} (valid to {c['kyc_expiry']})",
        "",
        "**Accounts**",
    ]
    for a in c["accounts"]:
        lines.append(f"- {a['type']} {mask_account(a['account_no'])} — {inr(a['balance'])} · {a['status']}")

    lines.append("")
    lines.append("**Cards**")
    for card in c["cards"] or []:
        extra = f" · outstanding {inr(card['total_outstanding'])} due {card['due_date']}" if card["kind"] == "Credit" else ""
        lines.append(f"- {card['product']} {mask_card(card['last4'])} ({card['kind']}) — {card['status']}{extra}")
    if not c["cards"]:
        lines.append("- none")

    if c["loans"]:
        lines.append("")
        lines.append("**Loans**")
        for l in c["loans"]:
            od = f" · OVERDUE {inr(l['overdue_amount'])}" if l["overdue_amount"] else ""
            lines.append(
                f"- {l['type']} {l['loan_id']} — outstanding {inr(l['outstanding'])}, "
                f"EMI {inr(l['emi'])} due {l['next_due']} · {l['status']}{od}"
            )

    if c["deposits"]:
        lines.append("")
        lines.append("**Deposits**")
        for d in c["deposits"]:
            lines.append(f"- {d['type']} {d['deposit_id']} — matures {d['maturity']} at {inr(d['maturity_amount'])}")

    open_srs = [s for s in c["service_requests"] if s["status"] != "Closed"]
    open_disputes = [d for d in c["disputes"] if "Withdrawn" not in d["status"]]
    if open_srs or open_disputes:
        lines.append("")
        lines.append("**Open cases**")
        for s in open_srs:
            lines.append(f"- SR {s['sr_no']} · {s['category']} · {s['status']} · due {s['tat_date']}")
        for d in open_disputes:
            lines.append(f"- Dispute {d['dispute_id']} · {d['description']} · {d['status']}")

    if c.get("offers"):
        lines.append("")
        lines.append("**Pre-approved offers available** — mention only if relevant to what the customer asked.")

    lines.append("")
    if BANK.otp and BANK.otp.get("verified"):
        lines.append(f"**OTP STATUS: verified** for '{BANK.otp['purpose']}'. You may proceed with that action now.")
    elif BANK.otp:
        lines.append(f"**OTP STATUS: sent** for '{BANK.otp['purpose']}', awaiting the code from the customer.")
    else:
        lines.append("**OTP STATUS: none.** Any sensitive action needs send_otp then verify_otp first.")

    return "\n".join(lines)


def _instruction(_ctx) -> str:
    parts = [config.persona(), customer_context()]
    if VOICE_MODE["on"]:
        parts.append(voice_hint())
    return "\n\n---\n\n".join(parts)


def build_agent() -> Agent:
    tools = load_tools()
    return Agent(
        name="bank_support_assistant",
        model=DEFAULT_MODEL,
        description="Axis Bank customer support assistant",
        instruction=_instruction,
        tools=tools,
    )


# `adk web` / `adk run` look for a module-level `root_agent`.
root_agent = build_agent()
