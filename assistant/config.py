"""Config loading with hot reload.

Everything the demo can change lives in `config/`. Nothing here caches across a
reload, so editing a JSON or the persona and calling `reload_all()` (the Reload
button in the UI) is enough — no process restart.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"
DATA_DIR = CONFIG_DIR / "data"

PERSONA_FILE = CONFIG_DIR / "persona.md"
BRANDING_FILE = CONFIG_DIR / "branding.json"
CUSTOMERS_FILE = DATA_DIR / "customers.json"
KNOWLEDGE_FILE = DATA_DIR / "knowledge_base.json"

_cache: dict[str, Any] = {}


def _read_json(path: Path) -> dict:
    with path.open(encoding="utf-8") as fh:
        return json.load(fh)


def branding() -> dict:
    if "branding" not in _cache:
        _cache["branding"] = _read_json(BRANDING_FILE)
    return _cache["branding"]


def knowledge() -> dict:
    if "knowledge" not in _cache:
        _cache["knowledge"] = _read_json(KNOWLEDGE_FILE)
    return _cache["knowledge"]


def seed_customers() -> list[dict]:
    """Pristine copy from disk. `bank.py` owns the mutable working copy."""
    return _read_json(CUSTOMERS_FILE)["customers"]


def persona() -> str:
    """Re-read from disk every call so prompt edits land on the very next turn."""
    text = PERSONA_FILE.read_text(encoding="utf-8")
    b = branding()
    # str.replace, not str.format — so an editor can type a stray `{` mid-meeting
    # without taking the demo down.
    text = text.replace("{product_name}", b.get("product_name", "Bank Assistant"))
    text = text.replace("{bank_name}", b.get("bank_name", "the bank"))
    return text


def reload_all() -> None:
    _cache.clear()
