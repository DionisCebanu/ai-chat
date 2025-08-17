#!/usr/bin/env python3
# compare.py — detect "X or Y" / "X vs Y" and auto-scrape a quick comparison

import re
import link         # your existing link.get_links / link.handle
import scrape       # your existing fetch_url / extract_readable / sniff_charset

# "i5 or i7", "i5 vs i7", "intel core i5 versus intel core i7" etc.
COMPARE_PAT = re.compile(
    r"^\s*(?P<left>[^?]+?)\s+(?:or|vs|versus)\s+(?P<right>[^?]+?)\s*\??$",
    re.I,
)

def parse_compare(message: str):
    m = COMPARE_PAT.match((message or "").strip())
    if not m:
        return None
    left = m.group("left").strip()
    right = m.group("right").strip()
    if len(left) < 2 or len(right) < 2:
        return None
    return left, right

def _score_text(text: str, left: str, right: str) -> int:
    """Very light filter: must mention both sides, longer is better."""
    if not text:
        return 0
    t = text.lower()
    score = 0
    if left.lower() in t:  score += 1
    if right.lower() in t: score += 1
    # small boost for length (0,1,2)
    score += min(len(text) // 400, 2)
    return score

def _html_to_text(html: str) -> str:
    # crude HTML→text if extract_readable returns too little
    s = re.sub(r"(?is)<script.*?</script>|<style.*?</style>", " ", html or "")
    s = re.sub(r"(?is)<br\s*/?>", "\n", s)
    s = re.sub(r"(?is)</p\s*>", "\n", s)
    s = re.sub(r"(?is)<[^>]+>", " ", s)
    s = re.sub(r"[ \t\f\v]+", " ", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()

def handle(message: str):
    pair = parse_compare(message)
    if not pair:
        return (False, "")
    left, right = pair

    queries = [
        f"{left} vs {right}",
        f"{left} versus {right}",
        f"{left} {right} comparison",
        f"{left} vs {right} difference",
    ]

    tried = set()

    for q in queries:
        try:
            urls = link.get_links(q, num_results=6)
        except Exception:
            urls = []
        for url in urls:
            if url in tried:
                continue
            tried.add(url)
            try:
                # short, per-request timeout; UA handled inside scrape if you added it
                status, headers, raw = scrape.fetch_url(url, timeout=8.0)
                if "html" not in (headers.get("content-type", "") or ""):
                    continue
                enc = scrape.sniff_charset(raw, headers)
                html = raw.decode(enc, errors="replace")
                readable = (scrape.extract_readable(html) or "").strip()
                text = readable if len(readable) >= 300 else _html_to_text(html)

                if _score_text(text, left, right) >= 2:
                    title = ""
                    if hasattr(scrape, "extract_title"):
                        try:
                            title = scrape.extract_title(html) or ""
                        except Exception:
                            title = ""
                    title_line = f"{title}\n" if title else ""
                    snippet = text[:1200].strip()
                    return (True, f"Source: {url}\n{title_line}{snippet}")
            except Exception:
                # keep trying the next URL
                continue

    # If we couldn't fetch a page, at least return helpful links.
    try:
        handled, links_reply = link.handle(f"compare {left} vs {right}", max_results=5)
        if handled:
            return (True, "Couldn’t fetch a comparison page right now. Here are some links:\n" + links_reply)
    except Exception:
        pass

    return (True, "Couldn’t fetch a comparison page right now. Please try again or be more specific.")
