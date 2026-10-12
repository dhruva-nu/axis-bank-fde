"""Axis Bank support assistant — Google ADK agent over a mocked core banking system."""

import os

# VERTEX_AI_KEY routes every Gemini call (ADK, Jev engine, assist, TTS) through
# Vertex AI express mode instead of AI Studio. google-genai picks the backend
# from GOOGLE_GENAI_USE_VERTEXAI and the key from GOOGLE_API_KEY, so map onto
# those before any client is built. GEMINI_API_KEY is dropped so tts.py, which
# prefers it, can't reach for a stale AI Studio key.
if os.getenv("VERTEX_AI_KEY"):
    os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = "true"
    os.environ["GOOGLE_API_KEY"] = os.environ["VERTEX_AI_KEY"]
    os.environ.pop("GEMINI_API_KEY", None)

from .agent import root_agent  # noqa: F401,E402  (ADK discovers `root_agent` here)
