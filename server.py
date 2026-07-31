"""FastAPI server: serves the chat UI and streams the ADK agent over SSE.

Endpoints
    GET  /                 the chat UI
    GET  /assist           the live-call agent-assist UI  (see assistant/assist.py)
    GET  /api/config       branding, quick chips, customer roster  (UI reads this at load)
    POST /api/chat         SSE stream of the agent's reply + its tool calls
                           {"message": str, "voice": bool} — voice=true asks the
                           agent for a short, speakable reply (see agent.VOICE_MODE)
    POST /api/tts          one sentence -> WAV, spoken by Gemini (see assistant/tts.py)
    GET  /api/state        live core-banking state + audit trail (the right-hand panel)
    GET  /api/tools        every tool the agent currently has
    POST /api/customer     switch the logged-in customer
    POST /api/reload       re-read config/ + re-scan tools + rebuild the agent
    POST /api/new-chat     clear the conversation, keep the banking state

Agent assist (the second page — a human agent on a live call, not a chatbot)
    POST /api/assist/turn     one heard utterance -> SSE: who said it, then cards
    POST /api/assist/action   run a write tool the human just clicked
    POST /api/assist/wrapup   summary + disposition + CRM note from the transcript
    POST /api/assist/reset    end the call, clear the transcript
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import traceback
import uuid
import warnings
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, Request, Response
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

load_dotenv()

# Keep the console readable during a live demo. These are all cosmetic:
# ADK flags its streaming/schema features as experimental, and OpenTelemetry
# throws a teardown traceback whenever a browser disconnects mid-SSE-stream.
warnings.filterwarnings("ignore", message=r".*\[EXPERIMENTAL\].*")
logging.getLogger("opentelemetry.context").setLevel(logging.CRITICAL)

from google.adk.agents.run_config import RunConfig, StreamingMode  # noqa: E402
from google.adk.runners import Runner  # noqa: E402
from google.adk.sessions import InMemorySessionService  # noqa: E402
from google.genai import types  # noqa: E402

from assistant import agent as agent_module  # noqa: E402
from assistant import assist as assist_module  # noqa: E402
from assistant import config as cfg  # noqa: E402
from assistant import tts as tts_engine  # noqa: E402
from assistant.bank import BANK, inr, mask_account  # noqa: E402
from assistant.tools import tool_manifest  # noqa: E402

ROOT = Path(__file__).resolve().parent
APP_NAME = "axis_support"
ASSIST_APP = "axis_assist"

app = FastAPI(title="Axis Bank Assistant — POC")
app.mount("/static", StaticFiles(directory=ROOT / "web"), name="static")

session_service = InMemorySessionService()


class Live:
    """Mutable holder so /api/reload can swap the agent without a restart."""

    def __init__(self) -> None:
        self.rebuild()
        self.session_id = uuid.uuid4().hex

    def rebuild(self) -> None:
        cfg.reload_all()
        self.agent = agent_module.build_agent()
        self.runner = Runner(
            app_name=APP_NAME,
            agent=self.agent,
            session_service=session_service,
            auto_create_session=True,
        )
        # Same 71 tools, split: the assist agent holds the read-only half.
        self.assist_agent = assist_module.build_assist_agent()
        self.assist_runner = Runner(
            app_name=ASSIST_APP,
            agent=self.assist_agent,
            session_service=session_service,
            auto_create_session=True,
        )

    def new_session(self) -> None:
        self.session_id = uuid.uuid4().hex


class Call:
    """One live call on the assist page: what was heard, and what was clicked."""

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.session_id = uuid.uuid4().hex
        self.transcript: list[dict] = []
        self.actions: list[dict] = []
        self.sent_upto = 0  # how much of the transcript the assist agent has seen

    def unseen(self) -> str:
        """Everything said since the last assist run, so the agent gets both sides."""
        lines = self.transcript[self.sent_upto:]
        self.sent_upto = len(self.transcript)
        return "\n".join(f"{t['speaker'].upper()}: {t['text']}" for t in lines)

    def executed(self) -> set[str]:
        return {_action_key(a["tool"], a["args"]) for a in self.actions}


def _action_key(tool: str, args: dict) -> str:
    return f"{tool}:{json.dumps(args or {}, sort_keys=True)}"


LIVE = Live()
CALL = Call()


# --------------------------------------------------------------------------- #
# UI
# --------------------------------------------------------------------------- #


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(ROOT / "web" / "index.html")


@app.get("/assist")
async def assist_page() -> FileResponse:
    return FileResponse(ROOT / "web" / "assist.html")


@app.get("/api/config")
async def get_config() -> JSONResponse:
    b = cfg.branding()
    c = BANK.customer
    greeting = b.get("greeting", "Hello {first_name}").replace("{first_name}", c["first_name"])
    return JSONResponse(
        {
            "branding": b,
            "greeting": greeting,
            "customer": {
                "id": c["id"],
                "name": c["name"],
                "first_name": c["first_name"],
                "segment": c["segment"],
                "mobile": c["mobile"],
            },
            "roster": BANK.roster(),
            "model": agent_module.DEFAULT_MODEL,
            "tool_count": len(LIVE.agent.tools),
        }
    )


# --------------------------------------------------------------------------- #
# Chat
# --------------------------------------------------------------------------- #


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n"


async def _stream(message: str):
    yield _sse("start", {"session": LIVE.session_id})

    content = types.Content(role="user", parts=[types.Part(text=message)])
    run_config = RunConfig(streaming_mode=StreamingMode.SSE)
    streamed_any = False
    emitted = ""  # what the browser has already been sent for the current text turn
    fired: set[str] = set()

    try:
        async for event in LIVE.runner.run_async(
            user_id=BANK.active_id,
            session_id=LIVE.session_id,
            new_message=content,
            run_config=run_config,
        ):
            for call in event.get_function_calls() or []:
                # ADK reports the same call on both a partial and the final event.
                # Sent twice, the trace panel grows a phantom row that never gets
                # a result and sits on "waiting…" for the rest of the demo.
                key = call.id or f"{call.name}:{json.dumps(dict(call.args or {}), sort_keys=True)}"
                if key in fired:
                    continue
                fired.add(key)
                emitted = ""  # a tool call ends this text turn; the next one starts fresh
                yield _sse("tool_call", {"name": call.name, "args": dict(call.args or {})})

            for resp in event.get_function_responses() or []:
                yield _sse("tool_result", {"name": resp.name, "result": resp.response})

            # content, and content.parts, are both independently optional on an Event.
            parts = (event.content.parts or []) if event.content else []
            text = "".join(p.text for p in parts if getattr(p, "text", None))
            if not text:
                continue

            if getattr(event, "partial", False):
                streamed_any = True
                # ADK has shipped partials as both incremental chunks and as the
                # running accumulation. Send only the new suffix either way, so the
                # browser never double-renders a sentence on stage.
                if emitted and text.startswith(emitted):
                    chunk, emitted = text[len(emitted):], text
                else:
                    chunk, emitted = text, emitted + text
                if chunk:
                    yield _sse("delta", {"text": chunk})
            elif event.is_final_response() and not streamed_any:
                yield _sse("delta", {"text": text})

    except asyncio.CancelledError:  # browser navigated away mid-stream — not an error
        raise
    except Exception as exc:  # keep the demo alive — show the error in the chat
        traceback.print_exc()
        yield _sse("error", {"message": f"{type(exc).__name__}: {exc}"})

    yield _sse("done", {"audit": BANK.audit[-12:]})


@app.post("/api/chat")
async def chat(request: Request) -> StreamingResponse:
    body = await request.json()
    message = (body.get("message") or "").strip()
    # The browser tells us when the reply will be read aloud, so the agent can
    # answer in short spoken sentences instead of markdown bullet lists. Picked
    # up by agent._instruction(), which ADK re-evaluates on every turn.
    agent_module.VOICE_MODE["on"] = bool(body.get("voice"))
    return StreamingResponse(
        _stream(message),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# --------------------------------------------------------------------------- #
# Live state panel
# --------------------------------------------------------------------------- #


@app.get("/api/state")
async def state() -> JSONResponse:
    c = BANK.customer
    return JSONResponse(
        {
            "customer": {"name": c["name"], "segment": c["segment"], "kyc": c["kyc_status"]},
            "accounts": [
                {"label": a["type"], "ref": mask_account(a["account_no"]), "value": inr(a["balance"]), "status": a["status"]}
                for a in c["accounts"]
            ],
            "cards": [
                {
                    "label": card["product"],
                    "ref": f"••{card['last4']}",
                    "value": inr(card["total_outstanding"]) if card["kind"] == "Credit" else card["kind"],
                    "status": card["status"],
                }
                for card in c["cards"]
            ],
            "loans": [
                {"label": l["type"], "ref": l["loan_id"], "value": inr(l["outstanding"]), "status": l["status"]}
                for l in c["loans"]
            ],
            "deposits": [
                {"label": d["type"], "ref": d["deposit_id"], "value": inr(d["maturity_amount"]), "status": d["status"]}
                for d in c["deposits"]
            ],
            "service_requests": c["service_requests"],
            "disputes": c["disputes"],
            "audit": list(reversed(BANK.audit))[:40],
            "otp": (
                {"purpose": BANK.otp["purpose"], "verified": BANK.otp["verified"], "code": BANK.otp["code"]}
                if BANK.otp
                else None
            ),
        }
    )


@app.post("/api/tts")
async def tts(request: Request) -> Response:
    """One sentence of the reply -> WAV. The browser fires these concurrently as
    the reply streams and plays them in order, so synthesis overlaps generation.

    A failure here is never fatal: 503 tells the browser to fall back to its own
    `speechSynthesis` voice and carry on."""
    body = await request.json()
    text = (body.get("text") or "").strip()
    if not text:
        return JSONResponse({"error": "empty text"}, status_code=400)
    try:
        # The SDK call is blocking; keep the event loop free for the SSE stream.
        audio, mime = await asyncio.to_thread(tts_engine.synthesize, text, body.get("voice"))
    except tts_engine.TTSUnavailable as exc:
        return JSONResponse({"error": str(exc)}, status_code=503)
    return Response(
        content=audio,
        media_type=mime,
        headers={"Cache-Control": "no-store", "Content-Length": str(len(audio))},
    )


# --------------------------------------------------------------------------- #
# Agent assist — the live-call page
# --------------------------------------------------------------------------- #


def _already_done(card: dict) -> bool:
    """An action the human has already run this call. The model keeps proposing it
    until it sees the result, so drop the repeat rather than stack the button."""
    return card["kind"] == "do" and _action_key(card["tool"], card.get("args")) in CALL.executed()


async def _assist_stream(text: str, forced: str | None):
    """One heard utterance: decide who said it, then assist if it was the customer.

    The speaker label goes out before the assist runs, so the transcript paints
    immediately and the cards arrive underneath it.
    """
    try:
        speaker = forced or await asyncio.to_thread(
            assist_module.classify_speaker, text, CALL.transcript
        )
        CALL.transcript.append({"speaker": speaker, "text": text})
        yield _sse("speaker", {"speaker": speaker, "text": text, "index": len(CALL.transcript) - 1})

        if speaker != "customer":
            # The agent talking is context for the next assist run, not a trigger.
            yield _sse("done", {"assisted": False})
            return

        content = types.Content(role="user", parts=[types.Part(text=CALL.unseen())])
        seen = ""      # every character of this text turn we have already handled
        pending = ""   # the tail that is not yet a complete line
        fired: set[str] = set()

        async for event in LIVE.assist_runner.run_async(
            user_id=BANK.active_id,
            session_id=CALL.session_id,
            new_message=content,
            run_config=RunConfig(streaming_mode=StreamingMode.SSE),
        ):
            for call in event.get_function_calls() or []:
                # The same call is reported on both a partial and the final event.
                key = call.id or f"{call.name}:{json.dumps(dict(call.args or {}), sort_keys=True)}"
                if key in fired:
                    continue
                fired.add(key)
                yield _sse("tool_call", {"name": call.name, "args": dict(call.args or {})})

            for resp in event.get_function_responses() or []:
                yield _sse("tool_result", {"name": resp.name, "result": resp.response})

            parts = (event.content.parts or []) if event.content else []
            text = "".join(p.text for p in parts if getattr(p, "text", None))
            if not text:
                continue
            if not (getattr(event, "partial", False) or event.is_final_response()):
                continue

            # ADK sends partials as increments on some versions and as the running
            # accumulation on others, and the final event repeats the whole turn.
            # Track what has been seen so a card is never emitted twice.
            if text.startswith(seen):
                new, seen = text[len(seen):], text
            else:
                new, seen = text, seen + text
            if not new:
                continue
            pending += new

            while "\n" in pending:
                line, _, pending = pending.partition("\n")
                card = assist_module.parse_card(line)
                if card and not _already_done(card):
                    yield _sse("card", card)

        card = assist_module.parse_card(pending)
        if card and not _already_done(card):
            yield _sse("card", card)

    except asyncio.CancelledError:
        raise
    except Exception as exc:
        traceback.print_exc()
        yield _sse("error", {"message": f"{type(exc).__name__}: {exc}"})

    yield _sse("done", {"assisted": True})


@app.post("/api/assist/turn")
async def assist_turn(request: Request) -> StreamingResponse:
    """One utterance heard on the call. `speaker` overrides the model's guess —
    that is what the flip control in the transcript sends."""
    body = await request.json()
    text = (body.get("text") or "").strip()
    forced = body.get("speaker") if body.get("speaker") in ("customer", "agent") else None
    return StreamingResponse(
        _assist_stream(text, forced),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/api/assist/action")
async def assist_action(request: Request) -> JSONResponse:
    """Execute a write tool. Reachable only from a click — the assist agent itself
    never holds these (see assist.split_tools)."""
    body = await request.json()
    name, args = body.get("tool"), body.get("args") or {}
    try:
        result = await asyncio.to_thread(assist_module.execute_action, name, args)
    except KeyError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    except Exception as exc:
        traceback.print_exc()
        return JSONResponse({"error": f"{type(exc).__name__}: {exc}"}, status_code=500)
    CALL.actions.append({"tool": name, "args": args, "result": result})
    # Tell the assist agent it happened. Without this it has no idea the click
    # landed and goes on recommending the same action for the rest of the call.
    fields = result if isinstance(result, dict) else {}
    note = fields.get("reference") or fields.get("sr_no") or fields.get("status") or "done"
    CALL.transcript.append({"speaker": "system", "text": f"[core banking] {name} executed — {note}"})
    return JSONResponse({"ok": True, "result": result, "audit": BANK.audit[-6:]})


@app.post("/api/assist/wrapup")
async def assist_wrapup() -> JSONResponse:
    if not CALL.transcript:
        return JSONResponse({"error": "nothing was heard on this call"}, status_code=400)
    try:
        record = await asyncio.to_thread(assist_module.wrap_up, CALL.transcript, CALL.actions)
    except Exception as exc:
        traceback.print_exc()
        return JSONResponse({"error": f"{type(exc).__name__}: {exc}"}, status_code=500)
    return JSONResponse(
        {
            "ok": True,
            **record,
            "actions": [{"tool": a["tool"], "args": a["args"]} for a in CALL.actions],
            "turns": len(CALL.transcript),
        }
    )


@app.post("/api/assist/correct")
async def assist_correct(request: Request) -> JSONResponse:
    """Re-label a line. Speaker attribution is a guess off one undiarised mic, so
    the transcript stays editable — and the wrap-up reads the corrected version."""
    body = await request.json()
    index, speaker = body.get("index"), body.get("speaker")
    if not isinstance(index, int) or not 0 <= index < len(CALL.transcript):
        return JSONResponse({"error": "no such line"}, status_code=400)
    if speaker not in ("customer", "agent"):
        return JSONResponse({"error": "speaker must be customer or agent"}, status_code=400)
    CALL.transcript[index]["speaker"] = speaker
    return JSONResponse({"ok": True})


@app.post("/api/assist/reset")
async def assist_reset() -> JSONResponse:
    CALL.reset()
    return JSONResponse({"ok": True})


@app.get("/api/tools")
async def tools() -> JSONResponse:
    manifest = tool_manifest()
    read = {fn.__name__ for fn in assist_module.split_tools()[0]}
    grouped: dict[str, list] = {}
    for t in manifest:
        grouped.setdefault(t["module"], []).append(
            {"name": t["name"], "doc": t["doc"], "mode": "read" if t["name"] in read else "write"}
        )
    return JSONResponse({"count": len(manifest), "read_count": len(read), "modules": grouped})


# --------------------------------------------------------------------------- #
# Demo controls
# --------------------------------------------------------------------------- #


@app.post("/api/customer")
async def switch_customer(request: Request) -> JSONResponse:
    body = await request.json()
    BANK.switch_customer(body["id"])
    LIVE.new_session()
    c = BANK.customer
    greeting = cfg.branding().get("greeting", "Hello {first_name}").replace("{first_name}", c["first_name"])
    return JSONResponse({"ok": True, "customer": c["name"], "greeting": greeting})


@app.post("/api/reload")
async def reload_everything() -> JSONResponse:
    """Re-read config/, re-scan tools, rebuild the agent, reseed the mock bank."""
    cfg.reload_all()  # drop cached JSON first...
    BANK.reset()      # ...so the reseed picks up the edited customers.json
    LIVE.rebuild()
    LIVE.new_session()
    c = BANK.customer
    return JSONResponse(
        {
            "ok": True,
            "tool_count": len(LIVE.agent.tools),
            "model": agent_module.DEFAULT_MODEL,
            "greeting": cfg.branding().get("greeting", "").replace("{first_name}", c["first_name"]),
            "customer": {"id": c["id"], "name": c["name"], "first_name": c["first_name"], "segment": c["segment"]},
            "roster": BANK.roster(),
            "branding": cfg.branding(),
        }
    )


@app.post("/api/new-chat")
async def new_chat() -> JSONResponse:
    """Fresh conversation, same banking state — for re-running a scenario."""
    LIVE.new_session()
    BANK.otp = None
    return JSONResponse({"ok": True, "session": LIVE.session_id})


if __name__ == "__main__":
    import uvicorn

    if not (os.getenv("GOOGLE_API_KEY") or os.getenv("GOOGLE_GENAI_USE_VERTEXAI")):
        print("\n  !! GOOGLE_API_KEY is not set. Copy .env.example to .env and add your key.")
        print("     Get one at https://aistudio.google.com/apikey\n")

    port = int(os.getenv("PORT", "8000"))
    print(f"\n  Axis Bank Assistant POC  →  http://localhost:{port}\n")
    uvicorn.run("server:app", host="0.0.0.0", port=port, reload=False)
