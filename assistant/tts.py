"""Server-side text-to-speech with Gemini.

The browser's own `speechSynthesis` is free and instant, but what it sounds like
is entirely down to the machine — excellent on macOS, robotic on Linux, absent
on a Linux box with no speech engine installed. This module makes the voice the
same everywhere and makes it sound like a person.

The trade is latency. Gemini's TTS is a **batch** API — measured at roughly 3-5s
for one sentence, and `generate_content_stream` returns a single chunk at the
end, so there is no time-to-first-byte to exploit. The way to hide that is to
synthesise **per sentence, concurrently**, starting the moment the agent finishes
a sentence rather than when the reply is done: the client fires a request per
sentence as the reply streams, several are in flight at once, and they are played
back in order. TTS latency then overlaps text generation instead of following it.

Identical text is cached, so re-running a scenario — and the greeting, every
time — is instant and free.
"""

from __future__ import annotations

import base64
import io
import json
import os
import re
import time
import urllib.error
import urllib.request
import wave
from array import array
from collections import OrderedDict
from threading import Lock

from google import genai
from google.genai import types

from . import config

DEFAULT_MODEL = "gemini-2.5-flash-preview-tts"
DEFAULT_VOICE = "Kore"
DEFAULT_STYLE = "Say in a warm, calm, reassuring tone, like an experienced Indian bank agent: "

# Cheap insurance against a runaway bill or a pathological reply: the model is
# billed per output token, i.e. per second of audio produced.
MAX_CHARS = 600

# Measured: this model fails roughly 1 request in 9 — sometimes a 500 INTERNAL,
# sometimes a candidate with no audio in it — and the failures are independent of
# each other and of load (concurrent and sequential fail at the same rate). So
# retrying is the entire fix, and it needs enough attempts to matter: at ~11% per
# try, 2 attempts still leaves ~1 reply in 30 dropping to the fallback voice,
# which is often enough to happen in front of a client. Four attempts makes it
# ~1 in 60,000, and costs an expected ~0.4s because most calls never retry.
ATTEMPTS = 4
BACKOFF = 0.3        # seconds, doubled each retry

_client: genai.Client | None = None
_cache: OrderedDict[tuple, bytes] = OrderedDict()
_cache_lock = Lock()
CACHE_MAX = 256


class TTSUnavailable(RuntimeError):
    """No key, no model, or the API refused — the browser falls back to its own voice."""


def _retryable(exc: Exception) -> bool:
    """Retry server-side flakiness and rate limits; give up immediately on a bad
    request (unknown voice name, revoked key) which will fail identically forever."""
    code = getattr(exc, "code", None)
    if isinstance(code, int):
        return code >= 500 or code == 429
    return True   # timeout, connection reset, anything unlabelled — worth another go


def _get_client() -> genai.Client:
    global _client
    if _client is None:
        key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
        if not key:
            raise TTSUnavailable("no GEMINI_API_KEY / GOOGLE_API_KEY set")
        _client = genai.Client(api_key=key)
    return _client


def settings() -> dict:
    """The `voice` block from branding.json, re-read so edits apply live."""
    v = config.branding().get("voice", {}) or {}
    g = v.get("gemini", {}) or {}
    c = v.get("cloud", {}) or {}
    return {
        "provider": (v.get("provider") or "gemini").lower(),
        "model": g.get("model") or DEFAULT_MODEL,
        "voice_name": g.get("voice_name") or DEFAULT_VOICE,
        "style": g.get("style", DEFAULT_STYLE),
        "cloud_voice": c.get("voice_name") or "en-IN-Chirp3-HD-Achernar",
        "cloud_lang": c.get("language_code") or "en-IN",
        "cloud_rate": c.get("speaking_rate", 1.0),
        "cloud_key_env": c.get("api_key_env") or "GOOGLE_TTS_API_KEY",
    }


def _wav(pcm: bytes, rate: int) -> bytes:
    """Wrap raw PCM in a WAV header — browsers won't play bare L16."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)   # L16 = signed 16-bit
        w.setframerate(rate)
        w.writeframes(pcm)
    return buf.getvalue()


def _rate_from_mime(mime: str | None) -> int:
    """'audio/L16;codec=pcm;rate=24000' -> 24000. Don't assume the default holds."""
    m = re.search(r"rate=(\d+)", mime or "")
    return int(m.group(1)) if m else 24000


SILENCE = 400        # ~1% of full scale; below this is room tone, not speech
KEEP_MS = 40         # leave a little air so clips don't sound clipped together


def _trim_silence(pcm: bytes, rate: int) -> bytes:
    """Gemini pads every clip with ~0.4s of lead and ~0.3s of tail silence. One
    clip per sentence means that padding is paid once per sentence — about three
    seconds of dead air across a reply, and it reads as the voice hesitating."""
    samples = array("h")
    samples.frombytes(pcm[: len(pcm) - (len(pcm) % 2)])
    n = len(samples)
    if not n:
        return pcm

    first, last = 0, n - 1
    while first < n and abs(samples[first]) <= SILENCE:
        first += 1
    if first >= n:
        return pcm      # all silence — leave it alone rather than return nothing
    while last > first and abs(samples[last]) <= SILENCE:
        last -= 1

    keep = int(rate * KEEP_MS / 1000)
    first = max(0, first - keep)
    last = min(n - 1, last + keep)
    return samples[first : last + 1].tobytes()


def _synth_gemini(text: str, voice_name: str, s: dict) -> tuple[bytes, str]:
    client = _get_client()   # its TTSUnavailable is already the right shape
    cfg = types.GenerateContentConfig(
        response_modalities=["AUDIO"],
        speech_config=types.SpeechConfig(
            voice_config=types.VoiceConfig(
                prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=voice_name)
            )
        ),
    )

    last_err = "the model returned no audio"
    for attempt in range(ATTEMPTS):
        if attempt:
            time.sleep(BACKOFF * (2 ** (attempt - 1)))
        try:
            resp = client.models.generate_content(
                model=s["model"], contents=f"{s['style']}{text}", config=cfg
            )
        except Exception as exc:
            last_err = f"{type(exc).__name__}: {exc}"
            if not _retryable(exc):
                raise TTSUnavailable(last_err) from exc
            continue

        # A candidate can come back with content=None (safety stop, empty
        # generation), so every hop here is optional.
        cand = resp.candidates[0] if resp.candidates else None
        parts = (cand.content.parts or []) if cand and cand.content else []
        blob = next((p.inline_data for p in parts if getattr(p, "inline_data", None)), None)
        if blob and blob.data:
            rate = _rate_from_mime(blob.mime_type)
            return _wav(_trim_silence(blob.data, rate), rate), "audio/wav"
        last_err = "the model returned no audio"

    raise TTSUnavailable(f"{last_err} (after {ATTEMPTS} attempts)")


def _synth_cloud(text: str, s: dict) -> tuple[bytes, str]:
    """Google Cloud Text-to-Speech — the low-latency path.

    Gemini's TTS is a batch model: measured 4-11s for a single sentence, which is
    far too slow to hold a conversation. Cloud TTS answers in a few hundred
    milliseconds with comparable Chirp3-HD / Neural2 voices. It needs the
    Cloud Text-to-Speech API enabled on a GCP project and its own key — an
    AI Studio key is refused with PERMISSION_DENIED.
    """
    key = os.getenv(s["cloud_key_env"])
    if not key:
        raise TTSUnavailable(f"{s['cloud_key_env']} is not set")

    payload = json.dumps({
        "input": {"text": text},
        "voice": {"languageCode": s["cloud_lang"], "name": s["cloud_voice"]},
        "audioConfig": {"audioEncoding": "MP3", "speakingRate": s["cloud_rate"]},
    }).encode()

    req = urllib.request.Request(
        f"https://texttospeech.googleapis.com/v1/text:synthesize?key={key}",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            body = json.loads(r.read())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:200]
        raise TTSUnavailable(f"HTTP {exc.code}: {detail}") from exc
    except Exception as exc:
        raise TTSUnavailable(f"{type(exc).__name__}: {exc}") from exc

    audio = base64.b64decode(body.get("audioContent") or "")
    if not audio:
        raise TTSUnavailable("Cloud TTS returned no audio")
    return audio, "audio/mpeg"


def synthesize(text: str, voice_name: str | None = None) -> tuple[bytes, str]:
    """Text -> (audio bytes, mime). Raises TTSUnavailable so the caller can fall back."""
    text = (text or "").strip()
    if not text:
        raise TTSUnavailable("empty text")
    if len(text) > MAX_CHARS:
        text = text[:MAX_CHARS]

    s = settings()
    voice_name = voice_name or s["voice_name"]
    cache_key = (s["provider"], s["model"], voice_name, s["style"],
                 s["cloud_voice"], s["cloud_rate"], text)

    with _cache_lock:
        hit = _cache.get(cache_key)
        if hit is not None:
            _cache.move_to_end(cache_key)
            return hit

    if s["provider"] == "cloud":
        result = _synth_cloud(text, s)
    else:
        result = _synth_gemini(text, voice_name, s)

    with _cache_lock:
        _cache[cache_key] = result
        while len(_cache) > CACHE_MAX:
            _cache.popitem(last=False)

    return result


def cache_stats() -> dict:
    with _cache_lock:
        return {"entries": len(_cache), "bytes": sum(len(a) for a, _ in _cache.values())}
