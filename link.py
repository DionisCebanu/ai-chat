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
import os
import re

_LINK_PATTERNS = [
    re.compile(r"\bgive me (?:the )?links?\s+for\s+(.+?)\s*$", re.IGNORECASE),
    re.compile(r"\bsearch\s+for\s+(.+?)\s*$", re.IGNORECASE),
    re.compile(r"\bfind\s+links?\s+about\s+(.+?)\s*$", re.IGNORECASE),
]

# Country for retailer links (defaults to Canada for your setup)
SHOP_COUNTRY = (os.getenv("SHOP_COUNTRY") or "CA").upper()

# Buy-intent patterns (all capture a named 'subject')
_BUY_PATTERNS = [
    re.compile(r"""^(?:link\s+to\s+buy|where\s+to\s+buy|buy|purchase)\s+(?P<subject>.+?)\s*$""", re.I),
    re.compile(r"""^(?:price\s+for|price\s+of)\s+(?P<subject>.+?)\s*$""", re.I),
    re.compile(r"""^(?P<subject>.+?)\s+(?:price|deal|deals)\s*$""", re.I),
]

def parse_buy_query(message: str) -> str | None:
    text = (message or "").strip()
    for pat in _BUY_PATTERNS:
        m = pat.match(text)
        if not m:
            continue
        subj = (m.group("subject") or "").strip().strip('"\'')

        # keep exact tokens (e.g., "Galaxy S23"), but small cleanup
        subj = re.sub(r"\s+", " ", subj)
        if subj:
            return subj
    return None

def shopping_links(subject: str, country: str = SHOP_COUNTRY) -> list[str]:
    """
    Deterministic retailer/search links so we don't get 'S24' when you asked 'S23'.
    """
    q = quote_plus(subject)
    links = []

    # Google Shopping
    links.append(f"https://www.google.com/search?tbm=shop&q={q}")

    # Country-aware retailers
    if country == "CA":
        links += [
            f"https://www.amazon.ca/s?k={q}",
            f"https://www.bestbuy.ca/en-ca/search?search={q}",
            f"https://www.walmart.ca/search?q={q}",
            f"https://www.samsung.com/ca/search/?searchvalue={q}",
            f"https://www.ebay.ca/sch/i.html?_nkw={q}",
        ]
    else:  # default to US
        links += [
            f"https://www.amazon.com/s?k={q}",
            f"https://www.bestbuy.com/site/searchpage.jsp?st={q}",
            f"https://www.walmart.com/search?q={q}",
            f"https://www.samsung.com/us/search/?query={q}",
            f"https://www.ebay.com/sch/i.html?_nkw={q}",
        ]

    return links


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
    If the message is a link or shopping request, return (True, reply_text).
    Otherwise (False, "").
    """
    # 1) BUY / shopping intent first
    buy_subject = parse_buy_query(message)
    if buy_subject:
        urls = shopping_links(buy_subject)
        lines = [f"Shopping links for: {buy_subject}"]
        for u in urls:
            lines.append(f"- {u}")
        return True, "\n".join(lines)

    # 2) Generic “give me the link for …”
    subject = parse_query_from_message(message)
    if subject:
        urls = get_links(subject, num_results=max_results)
        lines = [f"Here are some links for: {subject}"]
        for u in urls:
            lines.append(f"- {u}")
        return True, "\n".join(lines)

    # Not a link request
    return False, ""