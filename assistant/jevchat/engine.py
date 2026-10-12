"""The Jev-routed chatbot: Jev decides, the tools act, an LLM only writes the reply.

One turn:

    1. Pending OTP?   Jev: which number in the message is the code -> verify_otp -> run the held action
    2. Pending slot?  Jev: "is this the answer to what we asked?" + the missing slot(s)
    3. Route          Jev picks one tool (or none) from every tool's docstring
    4. Fill           every parameter becomes a Jev question, all in one request (slots.py)
    5. Execute        the tool runs; an otp_required refusal sends an OTP and holds the call
    6. Reply          Gemini gets the message + what was done + the tool output, and writes
                      the customer-facing text. It holds no tools, so it cannot act.

Decisions are Jev's; the LLM never picks a tool or writes an argument. Every query is
recorded — each Jev call's state, questions and answers, each tool call, the reply — by
log.TurnLog, for the viewer on :8001.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass, field
from typing import Any, AsyncIterator, Callable

from google import genai
from google.genai import types
from jev import AsyncJev, noul
from jev import choice as jev_choice

from .. import agent as agent_module
from .. import config
from ..bank import BANK
from ..tools import load_tools
from .log import TurnLog
from .slots import EXCLUDED, Ctx, Resolved, Slot, Span, settle, slots_for

ROUTE_MIN = 0.3  # below this the router is too unsure to act; the customer is asked to clarify
MAX_SLOT_ATTEMPTS = 2

Event = tuple[str, dict]

OTP = Span("otp", ask="the 6-digit OTP the customer was just sent by SMS")

FORMAT_RULES = """## How this turn works — this overrides anything above about tools

The bank's decision engine has ALREADY chosen and run the system action for this turn,
or decided none was needed. You hold no tools and cannot call any — ignore every
instruction above about calling tools, send_otp or verify_otp; that is handled for you.

Your one job is to write the reply to the customer from the system report you are given.
- State only facts that appear in the tool results or the customer context. Never invent
  an amount, date, rate, reference number or policy.
- If the report says to ask the customer for something, ask for exactly that, in one question.
- If a tool returned ok=false, say plainly what went wrong and what happens next.
- Never reveal an OTP, PIN or CVV, even if one appears in the data."""


@dataclass
class Pending:
    kind: str  # "slot": waiting for a missing value | "otp": waiting for the code
    tool: str
    args: dict
    missing: list[str] = field(default_factory=list)
    asked: str = ""
    attempts: int = 0


@dataclass
class Report:
    """What the system did this turn — the only thing the reply LLM is told.
    `log` is the per-query record for the viewer; the LLM never sees it."""

    log: TurnLog
    steps: list[dict] = field(default_factory=list)
    instruction: str = "Tell the customer the outcome."
    notes: list[str] = field(default_factory=list)
    settled: bool = False  # a pending question was answered, so this turn is not routed afresh

    def decide(self, why: str, instruction: str) -> None:
        """Set what the reply must do, and record why in the log."""
        self.instruction = instruction
        self.log.step("note", why, text=instruction)

    def note(self, text: str) -> None:
        self.notes.append(text)
        self.log.step("note", "note", text=text)


def _summary(fn: Callable) -> str:
    return (fn.__doc__ or fn.__name__).strip().split("\n")[0]


def _public(value: Any) -> Any:
    """Drop `_`-prefixed keys (e.g. send_otp's _demo_note, which carries the code)."""
    if isinstance(value, dict):
        return {k: _public(v) for k, v in value.items() if not str(k).startswith("_")}
    if isinstance(value, list):
        return [_public(v) for v in value]
    return value


class JevChat:
    def __init__(self) -> None:
        self._jev: AsyncJev | None = None
        self._llm: genai.Client | None = None
        self.rebuild()
        self.reset()

    # -- lifecycle -------------------------------------------------------- #

    def rebuild(self) -> None:
        """Re-scan assistant/tools/ — same hot-reload as the ADK agent."""
        self.fns: dict[str, Callable] = {fn.__name__: fn for fn in load_tools()}
        self.slots: dict[str, dict[str, Slot]] = {}
        self.skipped: list[str] = []
        for name, fn in self.fns.items():
            if name in EXCLUDED:
                continue
            slots = slots_for(fn)
            if slots is None:
                self.skipped.append(name)
            else:
                self.slots[name] = slots

    def reset(self) -> None:
        self.history: list[tuple[str, str]] = []
        self.pending: Pending | None = None

    @property
    def routable(self) -> list[str]:
        return list(self.slots)

    @property
    def jev(self) -> AsyncJev:
        if self._jev is None:
            self._jev = AsyncJev(model=os.getenv("JEV_MODEL", "jev-latest"))
        return self._jev

    @property
    def llm(self) -> genai.Client:
        if self._llm is None:
            self._llm = genai.Client()
        return self._llm

    # -- the turn --------------------------------------------------------- #

    async def turn(self, message: str) -> AsyncIterator[Event]:
        report = Report(log=TurnLog(message, BANK.customer["name"], self._pending()))
        reply, error = "", None
        try:
            try:
                async for ev in self._decide(message, report):
                    yield ev
            except Exception as exc:  # keep the chat alive; say so in the trace and the reply
                error = f"{type(exc).__name__}: {exc}"
                yield "error", {"message": f"Jev: {error}"}
                report.decide(
                    "decision failed",
                    "The bank's decision engine failed this turn, so nothing was done. "
                    "Apologise in one short sentence and ask the customer to try again.",
                )
            async for chunk in self._reply(message, report):
                reply += chunk
                yield "delta", {"text": chunk}
            self.history += [("customer", message), ("assistant", reply.strip())]
        finally:  # also when the browser disconnects mid-reply
            report.log.finish(reply.strip(), self._pending(), error)

    def _pending(self) -> dict | None:
        return asdict(self.pending) if self.pending else None

    async def _ask(self, report: Report, title: str, state: dict, *questions: Any) -> tuple[Any, dict]:
        """One Jev request, recorded with exactly what was sent and what came back."""
        entry = report.log.step("jev", title, state=state, questions={q.name: q.to_wire() for q in questions})
        t0 = time.perf_counter()
        try:
            result = await self.jev.ask(state, *questions)
        except Exception as exc:
            entry.update(ms=round((time.perf_counter() - t0) * 1000), error=f"{type(exc).__name__}: {exc}")
            raise
        entry.update(
            ms=round((time.perf_counter() - t0) * 1000),
            model=result.model,
            request_id=result.request_id,
            input_tokens=result.usage.input_tokens,
            answers={name: a.to_dict() for name, a in result.items()},
        )
        return result, entry

    async def _decide(self, message: str, report: Report) -> AsyncIterator[Event]:
        p = self.pending

        if p and p.kind == "otp":
            code, trace = await self._otp_code(p, message, report)
            if trace:
                yield "jev", {"name": "jev · otp code", "args": {"tool": p.tool}, "result": trace}
            if code:
                for ev in self._call("verify_otp", {"code": code}, report):
                    yield ev
                if report.steps[-1]["result"].get("ok"):
                    self.pending = None
                    async for ev in self._execute(p.tool, p.args, report):
                        yield ev
                elif BANK.otp is None:
                    self.pending = None
                    report.decide(
                        "OTP cancelled",
                        "The OTP was cancelled after too many wrong attempts, so the action did not happen. "
                        "Say so and offer to start again.",
                    )
                else:
                    report.decide("OTP wrong", "The OTP was wrong. Ask them to check the SMS and read the code out again.")
                return
            self.pending = None
            report.note(f"The customer did not give the OTP, so the pending '{p.tool}' was dropped.")

        if p and p.kind == "slot":
            async for ev in self._continue_slot(p, message, report):
                yield ev
            if report.settled:
                return

        async for ev in self._route(message, report):
            yield ev

    async def _otp_code(self, p: Pending, message: str, report: Report) -> tuple[str | None, dict]:
        """Jev picks which number in the message is the OTP — or says there is none."""
        ctx = self._ctx(message, p.tool, answering=True)
        plan = OTP.plan("code", ctx)
        if not plan.questions:  # not a digit in the message: they did not give a code
            return None, {}
        state = self._state(message, f"verifying the OTP sent for {ctx.action}")
        result, entry = await self._ask(report, "otp code", state, *plan.questions)
        r = settle({"code": plan}, {"code": OTP}, result)
        entry["resolved"] = r.trace
        return r.args.get("code"), r.trace

    async def _continue_slot(self, p: Pending, message: str, report: Report) -> AsyncIterator[Event]:
        """The customer may be answering what we asked — or have moved on."""
        ctx = self._ctx(message, p.tool, answering=True)
        slots = {m: self.slots[p.tool][m] for m in p.missing}
        plans = {m: s.plan(m, ctx) for m, s in slots.items()}
        answering = noul(
            "answering",
            f'The assistant just asked the customer for {p.asked}. Is the latest customer message '
            "an answer to that question, rather than a new, different request?",
        )
        questions = [answering] + [q for pl in plans.values() for q in pl.questions]
        result, entry = await self._ask(report, "slot answer", self._state(message, ctx.action), *questions)
        is_answer = result[answering].probability
        r = settle(plans, slots, result)
        entry["resolved"] = r.trace
        yield "jev", {
            "name": "jev · slot answer",
            "args": {"tool": p.tool, "asked": p.asked},
            "result": {"p_answering": round(is_answer, 3), **r.trace},
        }
        if is_answer < 0.5:
            self.pending = None
            report.note(f"The customer moved on from the pending '{p.tool}'.")
            return

        report.settled = True
        args = {**p.args, **r.args}
        if r.missing:
            # Only a reply that settles nothing counts against the customer.
            p.attempts = 0 if r.args else p.attempts + 1
            if p.attempts >= MAX_SLOT_ATTEMPTS:
                self.pending = None
                report.decide(
                    "gave up on slot",
                    f"The system still could not work out {r.missing[0].ask}, so nothing was done. "
                    "Apologise briefly and ask them to state the whole request again in one message.",
                )
                return
            p.args, p.missing = args, [m.param for m in r.missing]
            self._ask_for(p, r, report)
            return
        self.pending = None
        async for ev in self._execute(p.tool, args, report):
            yield ev

    async def _route(self, message: str, report: Report) -> AsyncIterator[Event]:
        criteria = {name: (self.fns[name].__doc__ or name).strip() for name in self.routable}
        criteria["none"] = (
            "No bank action or lookup needed: a greeting, thanks, goodbye, small talk, "
            "or a reply that does not ask for anything."
        )
        router = jev_choice(
            "tool",
            "Which single bank-system action should run to handle the customer's latest message? "
            "Use the recent conversation for context — a short reply like 'yes, do it' continues the topic before it.",
            criteria,
        )
        result, _ = await self._ask(report, "route", self._state(message), router)
        ans = result[router]
        top = ans.ranked()[:5]
        yield "jev", {
            "name": "jev · route",
            "args": {"message": message},
            "result": {"tool": ans.choice, "p": round(ans.probability, 3), "top": {k: round(v, 3) for k, v in top}},
        }

        if ans.choice == "none":
            report.decide(
                "no tool needed",
                "No bank action was needed for this message. Reply briefly and naturally. If they seem to "
                "want something specific, do not make up any data — ask one short clarifying question.",
            )
            return
        if ans.probability < ROUTE_MIN:
            likely = "; ".join(_summary(self.fns[k]) for k, _ in top[:3] if k != "none")
            report.decide(
                f"route unsure (p={ans.probability:.2f} < {ROUTE_MIN})",
                "The system could not tell what the customer wants, so nothing was done. Ask one short "
                f"clarifying question. The most likely intents were: {likely}",
            )
            return

        tool = ans.choice
        ctx = self._ctx(message, tool)
        slots = self.slots[tool]
        plans = {pname: s.plan(pname, ctx) for pname, s in slots.items()}
        questions = [q for pl in plans.values() for q in pl.questions]
        if questions:
            result, entry = await self._ask(report, f"params · {tool}", self._state(message, ctx.action), *questions)
        else:  # nothing to ask (verbatim text, fixed values, a single record, nothing typed)
            result, entry = None, report.log.step("resolve", f"params · {tool}")
        r = settle(plans, slots, result)
        entry["resolved"] = r.trace
        yield "jev", {"name": "jev · params", "args": {"tool": tool}, "result": r.trace}

        if r.missing:
            self.pending = Pending("slot", tool, r.args, [m.param for m in r.missing])
            self._ask_for(self.pending, r, report)
            return
        async for ev in self._execute(tool, r.args, report):
            yield ev

    def _ask_for(self, p: Pending, r: Resolved, report: Report) -> None:
        m = r.missing[0]
        p.asked = m.ask + (f" (likely one of: {'; '.join(m.options)})" if m.options else "")
        instruction = (
            f"To do this — {_summary(self.fns[p.tool])} — the system still needs {m.ask}. "
            "Nothing has been done yet. Ask the customer for exactly that, in one short question."
        )
        if m.options:
            instruction += f" It is most likely one of: {'; '.join(m.options)} — offer these as the choices."
        report.decide(f"missing: {', '.join(p.missing)}", instruction)

    async def _execute(self, tool: str, args: dict, report: Report) -> AsyncIterator[Event]:
        for ev in self._call(tool, args, report):
            yield ev
        result = report.steps[-1]["result"]
        if isinstance(result, dict) and result.get("error") == "otp_required":
            purpose = _summary(self.fns[tool]).split(".")[0]
            for ev in self._call("send_otp", {"purpose": purpose}, report):
                yield ev
            self.pending = Pending("otp", tool, args)
            report.decide(
                f"holding {tool} for OTP",
                "This action needs OTP verification, so it has NOT happened yet. An OTP has just been sent to "
                "the registered mobile: say what you are about to do, and ask them to read out the 6-digit code.",
            )

    def _call(self, name: str, args: dict, report: Report):
        yield "tool_call", {"name": name, "args": args}
        t0 = time.perf_counter()
        try:
            result = self.fns[name](**args)
        except Exception as exc:  # a tool bug must not take the turn down
            result = {"ok": False, "error": "tool_failed", "message": f"{type(exc).__name__}: {exc}"}
        report.steps.append({"tool": name, "args": args, "result": result})
        report.log.step("tool", name, args=args, result=result, ms=round((time.perf_counter() - t0) * 1000))
        yield "tool_result", {"name": name, "result": result}

    # -- context ---------------------------------------------------------- #

    def _conversation(self) -> list[str]:
        return [f"{who}: {text}" for who, text in self.history]

    def _ctx(self, message: str, tool: str, answering: bool = False) -> Ctx:
        return Ctx(
            message=message,
            earlier=[t for who, t in self.history if who == "customer"],
            conversation=self._conversation(),
            action=f"{tool} — {_summary(self.fns[tool])}",
            answering=answering,
        )

    def _state(self, message: str, action: str | None = None) -> dict:
        state: dict[str, Any] = {
            "recent_conversation": self._conversation()[-6:],
            "latest_customer_message": message,
        }
        if action:
            state["action_being_prepared"] = action
        return state

    # -- the reply -------------------------------------------------------- #

    async def _reply(self, message: str, report: Report) -> AsyncIterator[str]:
        system = "\n\n---\n\n".join(
            [config.persona(), agent_module.customer_context(), FORMAT_RULES]
            + ([agent_module.voice_hint()] if agent_module.VOICE_MODE["on"] else [])
        )
        steps = "\n".join(
            f"{i}. {s['tool']}({json.dumps(s['args'], default=str)}) returned:\n"
            f"{json.dumps(_public(s['result']), indent=1, default=str)}"
            for i, s in enumerate(report.steps, 1)
        ) or "Nothing — no system action ran."
        prompt = "\n\n".join(
            [
                "Recent conversation:\n" + ("\n".join(self._conversation()[-8:]) or "(this is the first message)"),
                f"Customer's latest message:\n{message}",
                f"What the system did this turn:\n{steps}",
                *(["Notes:\n" + "\n".join(report.notes)] if report.notes else []),
                f"Your reply should: {report.instruction}",
            ]
        )
        model = agent_module.DEFAULT_MODEL
        cfg = types.GenerateContentConfig(
            system_instruction=system,
            temperature=0.3,
            # Formatting needs no reasoning; on Flash, thinking only adds latency.
            thinking_config=types.ThinkingConfig(thinking_budget=0) if "flash" in model else None,
        )
        entry = report.log.step("llm", f"reply · {model}", instruction=report.instruction, prompt=prompt, system=system)
        t0, first, text = time.perf_counter(), None, ""
        try:
            stream = await self.llm.aio.models.generate_content_stream(model=model, contents=prompt, config=cfg)
            async for chunk in stream:
                if chunk.usage_metadata:  # cumulative; the last chunk carries the totals
                    entry["usage"] = chunk.usage_metadata.model_dump(exclude_none=True, mode="json")
                if chunk.text:
                    first = first or round((time.perf_counter() - t0) * 1000)
                    text += chunk.text
                    yield chunk.text
        except Exception as exc:
            entry["error"] = f"{type(exc).__name__}: {exc}"
            yield f"\n\n_(The reply could not be written: {type(exc).__name__}: {exc})_"
        finally:
            entry.update(reply=text, first_token_ms=first, ms=round((time.perf_counter() - t0) * 1000))
