"""Live agent assist — the machinery behind Lever 1.

Two humans are on a call. This module reads the transcript and writes to the
*human agent's* screen: the answer to what the customer just asked, the next
action to take, and any offer or risk worth flagging. It never speaks to the
customer.

Four pieces:

* `classify_speaker()` — one cheap model call deciding whether a line came from
  the customer or the agent. The browser hands us a single undiarised stream, so
  this is a judgement, not a measurement. The UI lets you correct it, and a
  correction is fed back as context for the next line.
* `build_assist_agent()` — an ADK agent holding the **read-only** half of the
  tools. Anything that changes state is proposed as a `[DO]` card for the human
  to click. That rule is enforced by the tool list, not by the prompt.
* `execute_action()` — runs one of those held-back write tools, once a human has
  actually clicked it.
* `wrap_up()` — summary, disposition and CRM note, generated from the transcript
  instead of hand-typed after the call.
"""

from __future__ import annotations

import inspect
import json
import os
import re
from typing import Callable

from google.adk.agents import Agent
from google.genai import types

from . import config
from .agent import DEFAULT_MODEL, customer_context
from .tools import load_tools

# A tool that only reads is safe for the model to call unprompted; a tool that
# writes needs a human. The whole read/write split rides on this prefix rule,
# which is why new tools should keep to the house naming convention.
READ_PREFIXES = ("get_", "list_", "check_", "search_", "find_", "track_")

CLASSIFIER_MODEL = os.getenv("ASSIST_CLASSIFIER_MODEL", "gemini-2.5-flash-lite")


def classifier_model() -> str:
    """Overridable from branding.json → assist.classifier_model."""
    return config.branding().get("assist", {}).get("classifier_model") or CLASSIFIER_MODEL

DISPOSITIONS = [
    "Card — lost/stolen, blocked",
    "Card — replacement requested",
    "Card — limit or feature change",
    "Transaction — disputed",
    "Transaction — failed, refund tracked",
    "Account — balance or statement enquiry",
    "Loan — EMI or foreclosure enquiry",
    "Deposit — booking or closure",
    "Payments — UPI / transfer / bill",
    "Complaint — logged",
    "Service request — raised",
    "General enquiry — resolved",
    "Escalated to supervisor",
]


# --------------------------------------------------------------------------- #
# Tool split
# --------------------------------------------------------------------------- #


def split_tools() -> tuple[list[Callable], list[Callable]]:
    """(read-only tools the assist may call, write tools a human must confirm)."""
    read: list[Callable] = []
    write: list[Callable] = []
    for fn in load_tools():
        (read if fn.__name__.startswith(READ_PREFIXES) else write).append(fn)
    return read, write


def _params(fn: Callable) -> str:
    out = []
    for p in inspect.signature(fn).parameters.values():
        ann = getattr(p.annotation, "__name__", None) or "str"
        out.append(f"{p.name}: {ann}" + ("" if p.default is inspect.Parameter.empty else " (optional)"))
    return ", ".join(out)


_write_meta: dict[str, set[str]] | None = None


def write_meta() -> dict[str, set[str]]:
    """{tool name -> its required arguments}. Cached, because `load_tools()`
    re-imports every module and this is consulted once per card."""
    global _write_meta
    if _write_meta is None:
        _write_meta = {
            fn.__name__: {
                p.name
                for p in inspect.signature(fn).parameters.values()
                if p.default is inspect.Parameter.empty
            }
            for fn in split_tools()[1]
        }
    return _write_meta


def write_catalogue() -> str:
    """The write tools, described so the model can propose one as a `[DO]` card."""
    _, write = split_tools()
    return "\n".join(
        f"- {fn.__name__}({_params(fn)}) — {(fn.__doc__ or '').strip().splitlines()[0]}"
        for fn in write
    )


def execute_action(name: str, args: dict) -> dict:
    """Run a write tool. Called only from /api/assist/action — i.e. after a click."""
    _, write = split_tools()
    fn = next((f for f in write if f.__name__ == name), None)
    if fn is None:
        raise KeyError(f"{name} is not a confirmable action")
    accepted = set(inspect.signature(fn).parameters)
    return fn(**{k: v for k, v in (args or {}).items() if k in accepted})


# --------------------------------------------------------------------------- #
# The assist agent
# --------------------------------------------------------------------------- #

ASSIST_INSTRUCTION = """\
# Live agent assist

You are **not** talking to the customer. A **human** Axis Bank agent is on a live
phone call with them. You are the heads-up display on that agent's screen — they
glance at you between sentences, while the customer is still talking.

You will be given what was just said on the call. React to the **customer's** last
request.

## Output format — tagged lines, nothing else

Emit only these. No greetings, no explanation, no markdown, no text outside a tag.

    [ANSWER] Short title :: the fact that answers what they just asked
    [ACTION] Short title :: what the agent should say or do next
    [DO] tool_name {"arg": "value"} :: the button label the agent will see
    [OFFER] Short title :: a pre-approved offer that genuinely fits this moment
    [RISK] Short title :: fraud, compliance or escalation worth flagging

## How to be useful

- **Look it up — never guess.** You have read-only tools against the live core
  banking system. A balance, a due date, a card status, a TAT, a dispute stage:
  call the tool and quote the real value. A wrong number on this screen gets read
  out to a customer.
- **Be glanceable.** Titles under five words. Bodies under fifteen. The agent is
  mid-sentence, not reading an email.
- **Two cards is normal, four is the maximum.** Rank by what unblocks the call.
- **Say the number.** "₹47,320.50, due 12 Aug" beats "their balance is available".
  Reference numbers belong here — this screen is the reason the deck says the
  agent shouldn't read them aloud.
- **If the customer said nothing actionable, emit nothing at all.** Silence is a
  valid turn. Do not narrate the call back to the agent.

## Actions the human confirms

You cannot change anything yourself. To block a card, raise a dispute, send an
OTP or book anything, emit a `[DO]` card and the agent clicks it:

    [DO] block_card {"last4": "4291"} :: Block debit card ••4291

The token straight after `[DO]` must be a **tool name copied exactly** from the
list at the bottom of this prompt — lowercase, with underscores. Not a verb, not
a description. Then valid JSON arguments matching that tool's signature, then
`::`, then the label the human reads. Get the name wrong and the button is
dropped, so copy it rather than remembering it.

Propose the action as soon as the customer's intent is clear — the click is the
confirmation, so you are not being presumptuous by putting the button there.

## Offers — the revenue lever, under the compliance rule

`[OFFER]` only when **all** of these hold:
- the customer's problem is resolved or clearly under control,
- the call is not a complaint, dispute, fraud report or any distressed moment,
- `get_preapproved_offers` actually returned something they are eligible for.

When those hold, **do check**. A call winding down calmly — "that's all I needed",
"everything else is fine", a plain enquiry answered with nothing wrong — is exactly
the moment this lever exists for, and staying silent there is a miss. Call
`get_preapproved_offers` and surface **one** offer: the product, and the single
number that makes it worth hearing.

Otherwise stay silent. A mistimed offer on a fraud call is a mis-selling incident,
not a missed sale, and the decision is the human's either way.
"""


def assist_hint() -> str:
    """Overridable from branding.json → assist.instruction."""
    return config.branding().get("assist", {}).get("instruction") or ASSIST_INSTRUCTION


def _assist_instruction(_ctx) -> str:
    return "\n\n---\n\n".join(
        [
            assist_hint(),
            customer_context(),
            "## Actions you can propose with [DO]\n\n" + write_catalogue(),
        ]
    )


def build_assist_agent() -> Agent:
    global _write_meta
    _write_meta = None  # a Reload may have added, renamed or re-signed a tool
    read, _ = split_tools()
    return Agent(
        name="agent_assist",
        model=DEFAULT_MODEL,
        description="Real-time assist for a human bank agent on a live call",
        instruction=_assist_instruction,
        tools=read,
    )


# --------------------------------------------------------------------------- #
# Card parsing
# --------------------------------------------------------------------------- #

_CARD = re.compile(r"^\[(ANSWER|ACTION|DO|OFFER|RISK)\]\s*(.*)$", re.I)


def parse_card(line: str) -> dict | None:
    """One tagged line -> a card dict, or None if the line isn't one."""
    m = _CARD.match(line.strip())
    if not m:
        return None
    kind, rest = m.group(1).lower(), m.group(2).strip()
    title, _, body = rest.partition("::")
    card = {"kind": kind, "title": title.strip(), "body": body.strip()}

    if kind == "do":
        # "block_card {"last4": "4291"}" — tool name, then a JSON blob.
        head = title.strip()
        name, _, raw = head.partition(" ")
        name = name.strip()
        label = card["body"] or name

        if name not in write_meta():
            # The model wrote prose where the tool name goes. Keep the advice —
            # it is usually still the right next step — but never render a button
            # that cannot fire.
            return {"kind": "action", "title": label or head, "body": ""}

        try:
            args = json.loads(raw.strip()) if raw.strip() else {}
        except json.JSONDecodeError:
            args = {}
        if not isinstance(args, dict):
            args = {}

        # A button proposed before the customer has given you the reference would
        # run and fail. Say what is still needed instead — that is the next thing
        # for the agent to ask.
        missing = sorted(a for a in write_meta()[name] if not str(args.get(a, "")).strip())
        if missing:
            return {
                "kind": "action",
                "title": label or head,
                "body": f"needs {', '.join(missing)} — ask the customer first",
            }

        card.update(tool=name, args=args, title=label, body="")
    return card


# --------------------------------------------------------------------------- #
# Speaker attribution
# --------------------------------------------------------------------------- #

_CLASSIFY = """\
You are labelling one line from a live phone call between an Axis Bank customer \
and a human bank agent.

The AGENT works for the bank: they look things up, confirm actions, ask for \
verification, and offer to do things. The CUSTOMER has the problem: they describe \
what happened, ask for things, and answer the agent's questions.

Recent lines, most recent last:
{history}

Label this line:
{text}

Reply with exactly one word: CUSTOMER or AGENT."""


_genai_client = None


def _client():
    """One client for the process. A fresh `genai.Client` per call looks harmless
    but shares a transport that gets closed underneath you — every call after the
    first then dies with 'the client has been closed'."""
    global _genai_client
    if _genai_client is None:
        from google import genai

        key = os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY")
        if not key:
            raise RuntimeError("GOOGLE_API_KEY / GEMINI_API_KEY is not set")
        _genai_client = genai.Client(api_key=key)
    return _genai_client


def classify_speaker(text: str, history: list[dict]) -> str:
    """'customer' or 'agent'. Falls back to alternation if the call fails.

    Only the last few lines are sent, and thinking is switched off — this sits in
    front of every assist run, so it is kept deliberately small and fast.
    """
    recent = "\n".join(f"{h['speaker']}: {h['text']}" for h in history[-4:]) or "(start of call)"
    try:
        resp = _client().models.generate_content(
            model=classifier_model(),
            contents=_CLASSIFY.format(history=recent, text=text),
            config=types.GenerateContentConfig(
                temperature=0,
                max_output_tokens=8,
                system_instruction="Answer with one word only.",
                thinking_config=types.ThinkingConfig(thinking_budget=0),
            ),
        )
        answer = (resp.text or "").strip().upper()
        if "AGENT" in answer:
            return "agent"
        if "CUSTOMER" in answer:
            return "customer"
        print(f"  [assist] classifier gave no verdict for {text!r} -> {answer!r}")
    except Exception as exc:  # never take the call down over a label...
        print(f"  [assist] classifier failed ({type(exc).__name__}: {exc}) — alternating")
    # ...but say so, or a silent fallback quietly becomes the whole feature.
    return "agent" if history and history[-1]["speaker"] == "customer" else "customer"


# --------------------------------------------------------------------------- #
# After-call work
# --------------------------------------------------------------------------- #

_WRAPUP = """\
This is the transcript of a call between an Axis Bank customer and a human agent, \
plus the actions the agent took in core banking during it.

{transcript}

{actions}

Write the after-call record the agent would otherwise type by hand. Be specific: \
name the products, amounts, reference numbers and dates that actually came up. If \
something was promised to the customer, it belongs in follow_ups with its date."""

_WRAPUP_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string", "description": "3-4 sentences: what they wanted, what was done, where it stands."},
        "disposition": {"type": "string", "enum": DISPOSITIONS},
        "crm_note": {"type": "string", "description": "One compact paragraph for the CRM activity log."},
        "follow_ups": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"what": {"type": "string"}, "due": {"type": "string"}},
                "required": ["what", "due"],
            },
        },
        "sentiment": {"type": "string", "enum": ["Satisfied", "Neutral", "Frustrated", "At risk"]},
    },
    "required": ["summary", "disposition", "crm_note", "follow_ups", "sentiment"],
}


def wrap_up(transcript: list[dict], actions: list[dict]) -> dict:
    """Summary, disposition tag and CRM note, from the transcript."""
    lines = "\n".join(f"{t['speaker'].upper()}: {t['text']}" for t in transcript)
    done = (
        "Actions executed during the call:\n"
        + "\n".join(f"- {a['tool']}({json.dumps(a.get('args', {}))})" for a in actions)
        if actions
        else "No core-banking actions were executed during the call."
    )
    resp = _client().models.generate_content(
        model=DEFAULT_MODEL,
        contents=_WRAPUP.format(transcript=lines, actions=done),
        config=types.GenerateContentConfig(
            temperature=0.2,
            response_mime_type="application/json",
            response_schema=_WRAPUP_SCHEMA,
        ),
    )
    return json.loads(resp.text)
