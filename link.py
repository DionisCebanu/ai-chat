#!/usr/bin/env python3
"""
link.py — tiny helper to return web links from a natural prompt like:
  "give me the link for Python programming"

Tries the third-party package `googlesearch-python` if available,
and falls back to returning a Google search URL if it’s not installed.

Install (optional, for direct results):
  pip install googlesearch-python
"""

from __future__ import annotations
from urllib.parse import quote_plus
import re

_LINK_PATTERNS = [
    re.compile(r"\bgive me (?:the )?links?\s+for\s+(.+?)\s*$", re.IGNORECASE),
    re.compile(r"\bsearch\s+for\s+(.+?)\s*$", re.IGNORECASE),
    re.compile(r"\bfind\s+links?\s+about\s+(.+?)\s*$", re.IGNORECASE),
]

def parse_query_from_message(message: str) -> str | None:
    """
    Extract the search query from the user message using predefined patterns.
    """
    for pattern in _LINK_PATTERNS:
        match = pattern.search(message)
        if match:
            subject = match.group(1).strip().strip('"\'')

            # very small guard against empty subjects like: "give me the link for"
            if subject:
                return subject
    return None

# ---- Fetch links -----------------------------------------------------------

def get_links(subject: str, num_results: int = 3) -> list[str]:
    """
    Return up to `num_results` URLs for the subject.
    - If `googlesearch-python` is installed, use it.
    - Otherwise, return a single Google search results URL (fallback).
    """
    # Preferred path: direct results via googlesearch (if installed).
    try:
        from googlesearch import search  # pip install googlesearch-python
        urls: list[str] = []
        for url in search(subject, num_results=num_results):
            urls.append(url)
            if len(urls) >= num_results:
                break
        if urls:
            return urls
    except Exception:
        # Library missing or blocked — fall back to a generic search page.
        pass
    # Fallback: return a Google search URL (always works; not scraping).
    return [f"https://www.google.com/search?q={quote_plus(subject)}"]

# ---- Top-level helper used by app.py --------------------------------------
def handle(message: str, max_results: int = 3) -> tuple[bool, str]:
    """
    If the message is a link request, return (True, reply_text).
    Otherwise return (False, "") so the caller can continue normal handling.
    """
    subject = parse_query_from_message(message)
    if not subject:
        return False, ""

    urls = get_links(subject, num_results=max_results)
    # Plain-text formatting (no Markdown) to suit your current UI.
    lines = [f"Here are some links for: {subject}"]
    for u in urls:
        lines.append(f"- {u}")
    reply = "\n".join(lines)
    return True, reply