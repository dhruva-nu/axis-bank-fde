"""Policy, charges, rates and branch lookup — the agent's factual grounding.

Everything here comes from config/data/knowledge_base.json. Add an article there
and the agent can answer that question; nothing in this file needs to change.
"""

from __future__ import annotations

from .. import config
from ..bank import BANK

_STOPWORDS = {
    "what", "when", "where", "which", "how", "why", "the", "is", "are", "was", "for", "and",
    "can", "you", "my", "me", "i", "a", "an", "of", "to", "in", "on", "do", "does", "with",
    "please", "tell", "about", "it", "this", "that", "get", "any",
}


def _score(article: dict, terms: set[str]) -> int:
    tags = " ".join(article["tags"]).lower()
    title = article["title"].lower()
    body = article["body"].lower()
    score = 0
    for t in terms:
        if t in tags:
            score += 5
        if t in title:
            score += 3
        if t in body:
            score += 1
    return score


def search_knowledge_base(query: str) -> dict:
    """Look up bank policy, charges, interest rates, turnaround times or required documents. ALWAYS use this before quoting a fee, rate, TAT or rule — never answer those from memory."""
    terms = {w for w in (query or "").lower().replace("?", " ").replace(",", " ").split() if w not in _STOPWORDS}
    articles = config.knowledge()["articles"]
    ranked = sorted(((_score(a, terms), a) for a in articles), key=lambda x: -x[0])
    hits = [a for s, a in ranked if s > 0][:3]

    if not hits:
        return {
            "ok": True,
            "found": False,
            "query": query,
            "message": (
                "Nothing in the knowledge base covers this. Do not guess — tell the customer you'll "
                "confirm, and raise a service request so a specialist responds."
            ),
            "available_topics": [a["title"] for a in articles],
        }
    return {
        "ok": True,
        "found": True,
        "query": query,
        "articles": [{"id": a["id"], "title": a["title"], "content": a["body"]} for a in hits],
    }


# Customers say Bangalore, the data says Bengaluru. Add a line here for any city
# where the colloquial and official names differ.
_CITY_ALIASES = {
    "bangalore": "bengaluru",
    "blr": "bengaluru",
    "madras": "chennai",
    "bombay": "mumbai",
    "delhi": "new delhi",
    "ncr": "new delhi",
    "gurgaon": "new delhi",
    "calcutta": "kolkata",
}


def find_branch(city: str, requirement: str) -> dict:
    """Find Axis Bank branches and ATMs. requirement can be 'locker', 'cash deposit machine', 'atm' or empty for any."""
    branches = config.knowledge()["branches"]
    city_q = (city or "").strip().lower()
    city_q = _CITY_ALIASES.get(city_q, city_q)
    req = (requirement or "").strip().lower()

    hits = [b for b in branches if not city_q or city_q in b["city"].lower() or city_q in b["name"].lower()]
    if "locker" in req:
        hits = [b for b in hits if b["locker"]]
    elif "cash" in req or "cdm" in req or "deposit" in req:
        hits = [b for b in hits if b["cash_deposit_machine"]]
    elif "atm" in req:
        hits = [b for b in hits if b["atm"]]

    if not hits:
        return {
            "ok": True,
            "found": False,
            "message": f"No branch found for '{city}'{' with ' + requirement if req else ''}.",
            "cities_available": sorted({b["city"] for b in branches}),
        }
    return {
        "ok": True,
        "found": True,
        "home_branch": BANK.customer["home_branch"],
        "branches": [
            {
                "name": f"{b['name']}, {b['city']}",
                "address": b["address"],
                "ifsc": b["ifsc"],
                "timings": b["timings"],
                "phone": b["phone"],
                "facilities": [
                    n for n, v in (("ATM", b["atm"]), ("Cash deposit machine", b["cash_deposit_machine"]), ("Locker", b["locker"])) if v
                ],
            }
            for b in hits[:4]
        ],
    }
