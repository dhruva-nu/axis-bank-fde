"""LLM vs Jev: the same queries through both chat engines, side by side.

    llm   the ADK agent (master's engine): Gemini picks the tools and writes the reply
    jev   JevChat: Jev picks the tool and every argument; Gemini only writes the reply

Every query is a fresh single-turn conversation against a freshly reset bank, run once per
engine (llm then jev, query by query, so drift in API latency hits both alike). Recorded per
run: the tool calls, the reply, time to first reply token, total time, tokens and cost.

    .venv/bin/python bench/llm_vs_jev.py                 # all 15 queries
    .venv/bin/python bench/llm_vs_jev.py --only 1,5      # a subset (1-based)
    .venv/bin/python bench/llm_vs_jev.py --jev-price 0.5 # Jev $ per 1M input tokens

Writes bench/results/<timestamp>.json and prints a summary table.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
import time
import uuid
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")
# Jev's per-query log goes to a scratch file the benchmark reads back, not the demo's log.
os.environ["JEV_LOG_FILE"] = str(Path(tempfile.mkdtemp()) / "jev_turns.jsonl")

from google.adk.agents.run_config import RunConfig, StreamingMode  # noqa: E402
from google.adk.runners import Runner  # noqa: E402
from google.adk.sessions import InMemorySessionService  # noqa: E402
from google.genai import types  # noqa: E402

from assistant import agent as agent_module  # noqa: E402
from assistant.bank import BANK  # noqa: E402
from assistant.jevchat import JevChat  # noqa: E402
from assistant.jevchat.log import LOG_FILE  # noqa: E402

QUERIES = [
    "What's my account balance?",
    "Show me the last few transactions on my Burgundy savings account",
    "I lost my Magnus credit card, please block it",
    "My Flipkart credit card still hasn't arrived, where is it?",
    "How much do I owe on my Magnus card this month and when is it due?",
    "There's a 47,320 charge from GlobalPay London on my card that I did not make",
    "I paid 12,499 to Amazon Pay yesterday, money got debited but the order failed",
    "Why was I charged extra on my personal loan?",
    "What would it cost to foreclose my home loan today?",
    "What's the FD interest rate for 2 years?",
    "Enable international usage on my debit card, I'm travelling to Singapore",
    "Set the daily ATM withdrawal limit on my debit card to 50,000",
    "What are the charges if I don't maintain minimum balance?",
    "What's the IFSC code of my home branch?",
    "Thanks, that's all for today!",
]

# Gemini 2.5 Flash, paid tier, USD per 1M tokens. Thinking tokens bill as output.
GEMINI_PRICE = {"input": 0.30, "cached": 0.03, "output": 2.50}


def gemini_cost(u: dict) -> float:
    prompt = u.get("prompt_token_count") or 0
    cached = u.get("cached_content_token_count") or 0
    out = (u.get("candidates_token_count") or 0) + (u.get("thoughts_token_count") or 0)
    return ((prompt - cached) * GEMINI_PRICE["input"] + cached * GEMINI_PRICE["cached"] + out * GEMINI_PRICE["output"]) / 1e6


def add_usage(total: dict, u: dict) -> None:
    for k in ("prompt_token_count", "cached_content_token_count", "candidates_token_count", "thoughts_token_count"):
        total[k] = total.get(k, 0) + (u.get(k) or 0)


def fresh_bank() -> None:
    BANK.reset()
    agent_module.VOICE_MODE["on"] = False


# --------------------------------------------------------------------------- #
# LLM — the ADK agent, driven the way server.py drives it (SSE streaming)
# --------------------------------------------------------------------------- #


async def run_llm(runner: Runner, query: str) -> dict:
    fresh_bank()
    content = types.Content(role="user", parts=[types.Part(text=query)])
    calls, fired, usage, llm_calls, unfinished = [], set(), {}, 0, None
    streamed, final_text, error = "", "", None
    t0, first = time.perf_counter(), None
    try:
        async for event in runner.run_async(
            user_id=BANK.active_id,
            session_id=uuid.uuid4().hex,
            new_message=content,
            run_config=RunConfig(streaming_mode=StreamingMode.SSE),
        ):
            for call in event.get_function_calls() or []:
                key = call.id or f"{call.name}:{json.dumps(dict(call.args or {}), sort_keys=True)}"
                if key not in fired:  # ADK repeats a call on the partial and the final event
                    fired.add(key)
                    calls.append({"tool": call.name, "args": dict(call.args or {})})
            for resp in event.get_function_responses() or []:
                for c in reversed(calls):
                    if c["tool"] == resp.name and "result" not in c:
                        c["result"] = resp.response
                        break
            # One usage record per model call: the final (non-partial) event of each. A call that
            # returns no content ends on a partial with no final, but its prompt is still billed.
            if event.usage_metadata and event.author != "user":
                u = event.usage_metadata.model_dump(exclude_none=True, mode="json")
                if event.partial:
                    unfinished = u
                else:
                    add_usage(usage, u)
                    llm_calls, unfinished = llm_calls + 1, None
            parts = (event.content.parts or []) if event.content else []
            text = "".join(p.text for p in parts if getattr(p, "text", None) and not getattr(p, "thought", False))
            if not text:
                continue
            first = first or round((time.perf_counter() - t0) * 1000)
            if event.partial:
                streamed += text
            elif event.is_final_response():
                final_text = text
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
    if unfinished:
        add_usage(usage, unfinished)
        llm_calls += 1
    reply = (final_text or streamed).strip()
    if not reply and not error:
        error = "empty reply: the model returned no content"
    return {
        "reply": reply,
        "tools": calls,
        "first_token_ms": first,
        "total_ms": round((time.perf_counter() - t0) * 1000),
        "llm_calls": llm_calls,
        "gemini_usage": usage,
        "gemini_cost": gemini_cost(usage),
        "jev_calls": 0,
        "jev_input_tokens": 0,
        "error": error,
    }


# --------------------------------------------------------------------------- #
# Jev — JevChat, exactly as the chat page runs it
# --------------------------------------------------------------------------- #


def last_log() -> dict:
    lines = LOG_FILE.read_text(encoding="utf-8").splitlines()
    return json.loads(lines[-1])


async def run_jev(bot: JevChat, query: str) -> dict:
    fresh_bank()
    bot.reset()
    calls, reply, errors = [], "", []
    t0, first = time.perf_counter(), None
    async for event, data in bot.turn(query):
        if event == "tool_call":
            calls.append({"tool": data["name"], "args": data["args"]})
        elif event == "tool_result":
            calls[-1]["result"] = data["result"]
        elif event == "delta":
            first = first or round((time.perf_counter() - t0) * 1000)
            reply += data["text"]
        elif event == "error":
            errors.append(data["message"])
    total = round((time.perf_counter() - t0) * 1000)

    log = last_log()
    jev = [s for s in log["steps"] if s["kind"] == "jev"]
    llm = [s for s in log["steps"] if s["kind"] == "llm"]
    usage: dict = {}
    for s in llm:
        add_usage(usage, s.get("usage") or {})
    route = next((s for s in jev if s["title"] == "route"), None)
    return {
        "reply": reply.strip(),
        "tools": calls,
        "first_token_ms": first,
        "total_ms": total,
        "llm_calls": len(llm),
        "gemini_usage": usage,
        "gemini_cost": gemini_cost(usage),
        "jev_calls": len(jev),
        "jev_input_tokens": sum(s.get("input_tokens") or 0 for s in jev),
        "jev_ms": sum(s.get("ms") or 0 for s in jev),
        "llm_ms": sum(s.get("ms") or 0 for s in llm),
        "jev_route": (route or {}).get("answers", {}).get("tool"),
        "jev_steps": [
            {"title": s["title"], "ms": s.get("ms"), "input_tokens": s.get("input_tokens"),
             "resolved": s.get("resolved"), "error": s.get("error")}
            for s in jev
        ],
        "error": "; ".join(errors) or log.get("error"),
    }


# --------------------------------------------------------------------------- #


def fmt_tools(tools: list[dict]) -> str:
    return " → ".join(t["tool"] for t in tools) or "—"


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", help="comma-separated 1-based query numbers")
    ap.add_argument("--jev-price", type=float, default=0.30,
                    help="Jev USD per 1M input tokens (no published price; default = Gemini Flash input)")
    args = ap.parse_args()
    picked = [int(i) for i in args.only.split(",")] if args.only else range(1, len(QUERIES) + 1)

    runner = Runner(app_name="bench", agent=agent_module.build_agent(),
                    session_service=InMemorySessionService(), auto_create_session=True)
    bot = JevChat()
    print(f"model {agent_module.DEFAULT_MODEL} · jev {os.getenv('JEV_MODEL', 'jev-latest')} · "
          f"customer {BANK.customer['name']} · Jev @ ${args.jev_price}/1M input\n")

    rows = []
    for i in picked:
        q = QUERIES[i - 1]
        print(f"[{i:2}] {q}")
        llm = await run_llm(runner, q)
        print(f"     llm  {llm['total_ms']:>6} ms  ${llm['gemini_cost']:.5f}  {fmt_tools(llm['tools'])}"
              + (f"  !! {llm['error']}" if llm["error"] else ""))
        jev = await run_jev(bot, q)
        jev["jev_cost"] = jev["jev_input_tokens"] * args.jev_price / 1e6
        print(f"     jev  {jev['total_ms']:>6} ms  ${jev['gemini_cost'] + jev['jev_cost']:.5f}  {fmt_tools(jev['tools'])}"
              + (f"  !! {jev['error']}" if jev["error"] else ""))
        rows.append({"n": i, "query": q, "llm": llm, "jev": jev})

    out = ROOT / "bench" / "results" / f"{time.strftime('%Y%m%d-%H%M%S')}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    meta = {"model": agent_module.DEFAULT_MODEL, "jev_model": os.getenv("JEV_MODEL", "jev-latest"),
            "jev_price_per_m_input": args.jev_price, "gemini_price_per_m": GEMINI_PRICE,
            "customer": BANK.customer["name"], "time": time.strftime("%Y-%m-%d %H:%M:%S")}
    out.write_text(json.dumps({"meta": meta, "rows": rows}, indent=1, default=str, ensure_ascii=False))

    def tot(side: str, key: str) -> float:
        return sum(r[side].get(key) or 0 for r in rows)

    n = len(rows)
    llm_cost, jev_cost = tot("llm", "gemini_cost"), tot("jev", "gemini_cost") + tot("jev", "jev_cost")
    print(f"\n{'':14}{'LLM':>14}{'Jev':>14}")
    print(f"{'avg total ms':14}{tot('llm', 'total_ms') / n:>14.0f}{tot('jev', 'total_ms') / n:>14.0f}")
    print(f"{'avg 1st tok ms':14}{tot('llm', 'first_token_ms') / n:>14.0f}{tot('jev', 'first_token_ms') / n:>14.0f}")
    print(f"{'total cost $':14}{llm_cost:>14.5f}{jev_cost:>14.5f}")
    print(f"{'cost / query $':14}{llm_cost / n:>14.5f}{jev_cost / n:>14.5f}")
    print(f"\nresults → {out.relative_to(ROOT)}")


if __name__ == "__main__":
    asyncio.run(main())
