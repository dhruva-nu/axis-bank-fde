"""Tool auto-discovery.

Every public top-level function in every module in this package becomes a tool the
agent can call. To add a new capability mid-meeting:

    1. Write a function in one of these modules (or a brand-new .py file here).
    2. Give it type hints and a one-line docstring — that docstring IS the description
       the model reads, so write it for the model.
    3. Return a dict.
    4. Hit Reload in the UI.

That's it. No registry to update, no imports to add, no restart.

Helpers you don't want exposed as tools: prefix the name with `_`.
Set TOOL_MODULES=accounts,cards in .env to load only some modules (useful if you
want to narrow the agent's surface for a specific demo).
"""

from __future__ import annotations

import importlib
import inspect
import os
import pkgutil
from pathlib import Path
from typing import Callable

_PKG_DIR = Path(__file__).parent


def load_tools() -> list[Callable]:
    only = {m.strip() for m in os.getenv("TOOL_MODULES", "").split(",") if m.strip()}
    tools: list[Callable] = []

    for mod_info in sorted(pkgutil.iter_modules([str(_PKG_DIR)]), key=lambda m: m.name):
        if only and mod_info.name not in only:
            continue
        module = importlib.import_module(f"{__name__}.{mod_info.name}")
        module = importlib.reload(module)  # pick up edits without restarting
        for name, obj in vars(module).items():
            if name.startswith("_"):
                continue
            if inspect.isfunction(obj) and obj.__module__ == module.__name__:
                tools.append(obj)

    return tools


def tool_manifest() -> list[dict]:
    """Names + descriptions, for the dev panel and the /api/tools endpoint."""
    out = []
    for fn in load_tools():
        doc = (fn.__doc__ or "").strip().split("\n")[0]
        out.append({"name": fn.__name__, "module": fn.__module__.rsplit(".", 1)[-1], "doc": doc})
    return out
