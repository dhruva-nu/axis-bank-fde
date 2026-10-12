#!/usr/bin/env python
"""Jev log viewer — every customer query as its own group of Jev calls, on :8001.

    GET  /             the viewer
    GET  /api/turns    ?after=N -> records from line N of logs/jev_turns.jsonl, and the next N
    POST /api/clear    empty the log

server.py starts this alongside the chat when CHAT_ENGINE=jev. It only reads the log file,
so it also runs on its own:

    .venv/bin/python jev_log.py
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse

load_dotenv()

from assistant.jevchat import log as jev_log  # noqa: E402

ROOT = Path(__file__).resolve().parent
PORT = int(os.getenv("JEV_LOG_PORT", "8001"))

app = FastAPI(title="Jev log")


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(ROOT / "web" / "jevlog.html", headers={"Cache-Control": "no-store"})


@app.get("/api/turns")
async def turns(after: int = 0) -> JSONResponse:
    records, total = jev_log.read_turns(after)
    if total < after:  # the file was cleared or rotated; start over
        records, total = jev_log.read_turns(0)
        return JSONResponse({"turns": records, "next": total, "reset": True})
    return JSONResponse({"turns": records, "next": total, "file": str(jev_log.LOG_FILE)})


@app.post("/api/clear")
async def clear() -> JSONResponse:
    jev_log.clear()
    return JSONResponse({"ok": True})


if __name__ == "__main__":
    import uvicorn

    print(f"\n  Jev log  →  http://localhost:{PORT}   (reading {jev_log.LOG_FILE})\n")
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="warning")
