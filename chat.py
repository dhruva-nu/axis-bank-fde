#!/usr/bin/env python
"""Terminal chat with the agent — no browser, no UI.

    .venv/bin/python chat.py                          interactive
    .venv/bin/python chat.py "block my debit card"    one-shot
    .venv/bin/python chat.py --scenario fraud         run a canned scenario

Handy for checking a prompt tweak or a new tool quickly, and for testing that
your API key works before the meeting starts.

Runs whichever engine CHAT_ENGINE selects (jev by default, or adk) — same as the server.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import uuid
import warnings

from dotenv import load_dotenv

load_dotenv()
warnings.filterwarnings("ignore", message=r".*\[EXPERIMENTAL\].*")

from google.adk.runners import Runner  # noqa: E402
from google.adk.sessions import InMemorySessionService  # noqa: E402
from google.genai import types  # noqa: E402

from assistant.agent import build_agent  # noqa: E402
from assistant.bank import BANK  # noqa: E402
from assistant.jevchat import JevChat  # noqa: E402

DIM, BOLD, MAGENTA, CYAN, GREY, RESET = "\033[2m", "\033[1m", "\033[35m", "\033[36m", "\033[90m", "\033[0m"

SCENARIOS = {
    "fraud": [
        "I see a charge on my Magnus card from GlobalPay London for 47,320 that I did not make",
        "Yes please block it and raise the dispute",
        "When will I get the money back?",
    ],
    "failed_upi": [
        "I paid 12,499 to Amazon Pay yesterday, money left my account but the order didn't go through",
        "Can you just refund it now?",
    ],
    "card_limit": [
        "I'm travelling to Singapore next week, can you enable international usage on my debit card?",
        "123456",
    ],
    "overdue": [
        "Why was I charged 590 rupees on my personal loan?",
        "Please pay the overdue EMI from my Burgundy savings account",
        "123456",
        "Can you waive the bounce charge? I've never missed a payment before",
    ],
    "fd": [
        "What's the FD rate for 2 years?",
        "Book one for 1 lakh from my main account",
        "123456",
    ],
    "angry": [
        "This is the third time I'm complaining about the AMB charge on my account. Fix it or I'm closing the account.",
    ],
}


async def run_jev(turns: list[str] | None) -> None:
    bot = JevChat()
    print(f"{DIM}jev engine · {len(bot.routable)} routable tools · customer: {BANK.customer['name']}{RESET}")
    print(f"{DIM}Ctrl-C to quit. OTP in this demo is always 123456.{RESET}\n")

    async def turn(text: str) -> None:
        print(f"{BOLD}You:{RESET} {text}")
        reply = ""
        async for event, data in bot.turn(text):
            if event == "jev":
                print(f"  {DIM}{data['name']}: {json.dumps(data['result'], default=str)[:220]}{RESET}")
            elif event == "tool_call":
                args = ", ".join(f"{k}={v!r}" for k, v in data["args"].items())
                print(f"  {CYAN}→ {data['name']}({args}){RESET}")
            elif event == "tool_result":
                print(f"  {GREY}  {str(data['result'])[:150]}{RESET}")
            elif event == "error":
                print(f"  {MAGENTA}!! {data['message']}{RESET}")
            elif event == "delta":
                reply += data["text"]
        print(f"\n{MAGENTA}Assistant:{RESET} {reply.strip()}\n")

    await _drive(turn, turns)


async def _drive(turn, turns: list[str] | None) -> None:
    if turns:
        for t in turns:
            await turn(t)
        return
    while True:
        try:
            text = input(f"{BOLD}You:{RESET} ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if text.lower() in ("exit", "quit", ":q"):
            return
        if text:
            await turn(text)


async def run(turns: list[str] | None) -> None:
    if os.getenv("CHAT_ENGINE", "jev").strip().lower() == "jev":
        await run_jev(turns)
        return
    agent = build_agent()
    runner = Runner(
        app_name="cli",
        agent=agent,
        session_service=InMemorySessionService(),
        auto_create_session=True,
    )
    session_id = uuid.uuid4().hex

    print(f"{DIM}{len(agent.tools)} tools · {agent.model} · customer: {BANK.customer['name']}{RESET}")
    print(f"{DIM}Ctrl-C to quit. OTP in this demo is always 123456.{RESET}\n")

    async def turn(text: str) -> None:
        print(f"{BOLD}You:{RESET} {text}")
        content = types.Content(role="user", parts=[types.Part(text=text)])
        async for event in runner.run_async(user_id=BANK.active_id, session_id=session_id, new_message=content):
            for call in event.get_function_calls() or []:
                args = ", ".join(f"{k}={v!r}" for k, v in (call.args or {}).items())
                print(f"  {CYAN}→ {call.name}({args}){RESET}")
            for resp in event.get_function_responses() or []:
                preview = str(resp.response)[:150].replace("\n", " ")
                print(f"  {GREY}  {preview}{RESET}")
            if event.is_final_response() and event.content:
                parts = event.content.parts or []
                text_out = "".join(p.text for p in parts if getattr(p, "text", None))
                if text_out:
                    print(f"\n{MAGENTA}Assistant:{RESET} {text_out.strip()}\n")

    await _drive(turn, turns)


def main() -> None:
    args = sys.argv[1:]
    if args and args[0] == "--scenario":
        name = args[1] if len(args) > 1 else "fraud"
        if name not in SCENARIOS:
            print(f"Scenarios: {', '.join(SCENARIOS)}")
            sys.exit(1)
        asyncio.run(run(SCENARIOS[name]))
    elif args:
        asyncio.run(run([" ".join(args)]))
    else:
        asyncio.run(run(None))


if __name__ == "__main__":
    main()
