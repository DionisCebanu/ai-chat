#!/usr/bin/env python3
"""
image_search.py — fetch image URLs for natural prompts like:
  "give me an image of Montreal"
  "3 photos of sunset in Kyoto"
  "picture of the Olympic Stadium Montreal"

Order of strategies:
1) duckduckgo_search (if installed) → direct image URLs (best quality, no API key)
2) Wikimedia Commons API (stdlib urllib)
3) Unsplash 'source' fallback URL (always returns a usable image redirect)

Output format to the app:
- handle(message) -> (handled: bool, reply_text: str)
- reply_text contains lines prefixed with 'IMG: ' for each image URL, plus a header line.
"""

from __future__ import annotations
from urllib.parse import quote_plus
from urllib import request as urlrequest
import json, re

# ---------------------- Optional dependency (best path) --------------------
_HAS_DDG = False
try:
    from duckduckgo_search import DDGS  # pip install duckduckgo_search
    _HAS_DDG = True
except Exception:
    _HAS_DDG = False

# --------------------------- Prompt parsing --------------------------------

# Examples captured:
#  - "give me an image/photo/picture of X"
#  - "show 3 images of X", "3 photos of X", "image of X"
#  - "picture for X", "images about X"
_IMG_PATTERNS = [
    re.compile(
        r"""^(?:(?:give\s+me|show|find|fetch|get|search\s*for)\s+)?   # optional verb
             (?:(?P<count>\d{1,2})\s*)?                               # optional number
             (?:an?\s*)?                                              # 'a'/'an' optional
             (?:image|images|photo|photos|picture|pictures|pic|pics)\s*
             (?:of|for|about)?\s*
             (?P<subject>.+?)\s*$                                     # SUBJECT
        """,
        re.I | re.X
    ),
    re.compile(
        r"""^(?:image|images|photo|photos|picture|pictures|pic|pics)\s*:\s*(?P<subject>.+)$""",
        re.I
    ),
]

def parse_image_query(message: str) -> tuple[int, str] | None:
    """
    Return (count, subject) or None if not an image request.
    Default count = 3 (bounded 1..6). Robust to stray legacy patterns.
    """
    text = (message or "").strip()
    for pat in _IMG_PATTERNS:
        m = pat.match(text)
        if not m:
            continue

        # Be defensive in case a legacy pattern without 'subject' slipped in
        groupindex = m.re.groupindex
        if "subject" in groupindex:
            subject = (m.group("subject") or "").strip().strip('"\'')

        else:
            # Fallback: try last capturing group (best effort)
            try:
                subject = (m.group(m.lastindex or 0) or "").strip().strip('"\'')

            except Exception:
                subject = ""

        # Clean up e.g., "the Montreal" -> "Montreal"
        subject = re.sub(r"^\bthe\s+", "", subject, flags=re.I).strip()

        if not subject:
            continue  # keep trying other patterns

        count = 3
        if "count" in groupindex:
            count_str = (m.group("count") or "").strip()
            if count_str.isdigit():
                count = int(count_str)

        count = max(1, min(6, count))
        return count, subject

    return None

# --------------------------- Image fetchers --------------------------------

def _ddg_images(subject: str, count: int) -> list[str]:
    """DuckDuckGo image search (if library installed)."""
    if not _HAS_DDG:
        return []
    urls: list[str] = []
    try:
        with DDGS() as ddgs:
            for r in ddgs.images(
                keywords=subject,
                max_results=count,
                safesearch="moderate",
                size=None,      # ("Small","Medium","Large","Wallpaper")
                type_image=None # ("photo","clipart","gif","transparent","line")
            ):
                # different versions expose keys slightly differently:
                u = r.get("image") or r.get("thumbnail") or r.get("url")
                if u and u not in urls:
                    urls.append(u)
                if len(urls) >= count:
                    break
    except Exception:
        return []
    return urls

def _wikimedia_images(subject: str, count: int) -> list[str]:
    """
    Wikimedia Commons API: search for files related to the subject.
    Returns thumbnail or full URLs.
    """
    base = "https://commons.wikimedia.org/w/api.php"
    q = (
        f"{base}?action=query&format=json"
        f"&generator=search&gsrsearch={quote_plus(subject)}"
        f"&gsrlimit={count}&gsrnamespace=6"
        f"&prop=imageinfo&iiprop=url|mime|size&iiurlwidth=1600"
    )
    try:
        req = urlrequest.Request(q, headers={"User-Agent": "MiniAI-Image/0.1"})
        with urlrequest.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
        pages = (data.get("query") or {}).get("pages") or {}
        urls: list[str] = []
        for _, page in pages.items():
            iinfo = page.get("imageinfo")
            if not iinfo:
                continue
            info = iinfo[0]
            u = info.get("thumburl") or info.get("url")
            if u and u not in urls:
                urls.append(u)
        return urls[:count]
    except Exception:
        return []

def _unsplash_fallback(subject: str, count: int) -> list[str]:
    """
    Unsplash 'source' gives a random image redirect for a query.
    No scraping needed. One URL is enough; repeat if more requested.
    """
    u = f"https://source.unsplash.com/1600x900/?{quote_plus(subject)}"
    return [u] * count

def get_images(subject: str, count: int = 3) -> list[str]:
    """Try DDG → Wikimedia → Unsplash. Return a list of image URLs."""
    return (
        _ddg_images(subject, count)
        or _wikimedia_images(subject, count)
        or _unsplash_fallback(subject, count)
    )

# ------------------------------ Top-level ----------------------------------

def handle(message: str) -> tuple[bool, str]:
    """
    If the message asks for images, return (True, reply_text) where reply_text
    contains 'IMG: <url>' lines that the front-end will render as <img>.
    Otherwise, return (False, "") so the app continues normal handling.
    """
    parsed = parse_image_query(message)
    if not parsed:
        return False, ""
    n, subject = parsed
    urls = get_images(subject, count=n)

    # First line is a human header; following lines are 'IMG:' markers.
    lines = [f"Images for: {subject}"]
    for u in urls:
        lines.append(f"IMG: {u}")
    return True, "\n".join(lines)
