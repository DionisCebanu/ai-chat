#!/usr/bin/env python3
"""
quickfacts.py — small intent detector + extractor for:
  phone / address / hours / email / website

Stdlib-only:
- Parse "quick fact" intents.
- DuckDuckGo HTML for organic links (no API keys).
- PHONE: Google SERP KP → Google Maps → organic pages → DDG SERP.
- Extractors prefer DOM/JSON-LD; avoid bare 10-digit noise.
"""

from __future__ import annotations
import os
import re
import json
from urllib.parse import quote_plus, unquote, urlparse, parse_qs
from urllib.request import Request, urlopen

# -------------------------- Intent parsing ---------------------------------

_FACT_PATTERNS = [
    ("phone", [
        re.compile(r"^(?:the\s*)?(?:phone|telephone)\s+(?:number\s+)?(?:of|for)\s+(?P<subject>.+)$", re.I),
        re.compile(r"^(?:phone\s*number|phone|telephone)\s*:\s*(?P<subject>.+)$", re.I),
        re.compile(r"^(?:call)\s+(?P<subject>.+)$", re.I),
    ]),
    ("address", [
        re.compile(r"^(?:the\s*)?address\s+(?:of|for)\s+(?P<subject>.+)$", re.I),
        re.compile(r"^address\s*:\s*(?P<subject>.+)$", re.I),
        re.compile(r"^(?:location|where\s+is)\s+(?P<subject>.+)$", re.I),
    ]),
    ("hours", [
        re.compile(r"^(?:the\s*)?(?:hours?|opening\s*hours|business\s*hours)\s+(?:of|for)\s+(?P<subject>.+)$", re.I),
        re.compile(r"^hours?\s*:\s*(?P<subject>.+)$", re.I),
        re.compile(r"^(?:what\s+time\s+.*open).*(?P<subject>.+)$", re.I),
    ]),
    ("email", [
        re.compile(r"^(?:the\s*)?email\s+(?:of|for)\s+(?P<subject>.+)$", re.I),
        re.compile(r"^email\s*:\s*(?P<subject>.+)$", re.I),
    ]),
    ("website", [
        re.compile(r"^(?:the\s*)?(?:website|site|homepage)\s+(?:of|for)\s+(?P<subject>.+)$", re.I),
        re.compile(r"^(?:website|site)\s*:\s*(?P<subject>.+)$", re.I),
    ]),
]

def parse_fact_query(message: str) -> tuple[str, str] | None:
    text = (message or "").strip()
    for kind, pats in _FACT_PATTERNS:
        for pat in pats:
            m = pat.match(text)
            if not m:
                continue
            subject = (m.group("subject") or "").strip().strip('"\'')

            # normalize: "the Costco Brossard" → "Costco Brossard"
            subject = re.sub(r"^\bthe\s+", "", subject, flags=re.I).strip()
            if subject:
                return kind, subject
    return None

# --------------------------- Extraction helpers ----------------------------

MOBILE_UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 14_0 like Mac OS X) "
             "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/14.0 "
             "Mobile/15A372 Safari/604.1")
DEBUG_QF = os.getenv("QUICKFACTS_DEBUG", "0") == "1"

def _fetch_html_direct(url: str, timeout: float = 10.0, ua: str = MOBILE_UA) -> str | None:
    """Fetch HTML with Accept-Encoding: identity (no br/gzip) for reliable regex on Google pages."""
    try:
        req = Request(url, headers={
            "User-Agent": ua,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "identity",
            "Connection": "close",
        })
        with urlopen(req, timeout=timeout) as r:
            ctype = r.headers.get("content-type", "")
            if "html" not in (ctype or ""):
                return None
            raw = r.read()
        try:
            return raw.decode("utf-8", errors="replace")
        except Exception:
            return raw.decode("latin-1", errors="replace")
    except Exception as e:
        if DEBUG_QF:
            print(f"[quickfacts] _fetch_html_direct fail {url}: {e}")
        return None

# Phone regexes
PHONE_RE = re.compile(
    r'(?:\+?1[\s\u00A0\u202F\-.]?)?(?:\(?\d{3}\)?[\s\u00A0\u202F\-.]?)\d{3}[\s\u00A0\u202F\-.]?\d{4}'
    r'(?:\s*(?:x|ext\.?|#)\s*\d{1,6})?',
    re.I
)
# Prefer "separated" formats over bare 10-digit runs
SEPARATED_PHONE_RE = re.compile(
    r'(?:\+?1[\s\u00A0\u202F\-.]?)?\(?\d{3}\)?[\s\u00A0\u202F\-.]\d{3}[\s\u00A0\u202F\-.]\d{4}',
    re.I
)

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", re.I)
POSTAL_RE = re.compile(r"\b[A-Za-z]\d[A-Za-z]\s?\d[A-Za-z]\d\b")
STREET_HINT_RE = re.compile(
    r"\b(st|street|ave|avenue|blvd|boulevard|rd|road|rte|route|chemin|ch|rue|dr|drive|hwy|highway|place|pl|court|ct|way|lane|ln|terrace|ter)\b",
    re.I,
)
DAY_HINT = re.compile(r"\b(mon|tue|wed|thu|fri|sat|sun)\b", re.I)

# DOM-aware phone markers
TEL_HREF_RE = re.compile(r'href=["\']tel:([\+\d][\d\-\s\u00A0\u202F\.\(\)]{7,})', re.I)
ITEMPROP_TEL_RE = re.compile(r'itemprop=["\']telephone["\'][^>]*>([^<]{7,60})<', re.I)
LABEL_NEAR_PHONE_RE = re.compile(
    r'(?is)(?:\bphone\b|\btelephone\b|\btel\b|téléphone|tél)[^0-9]{0,120}(\+?\(?\d[\d\)\s\u00A0\u202F\.\-]{6,})'
)

# Google SERP knowledge panel & Maps variants
GOOGLE_KP_PHONE_LINK_RE = re.compile(
    r'<a[^>]+data-dtype=["\']d3ph["\'][^>]*>.*?<span[^>]*>([^<]{7,60})</span>',
    re.I | re.S
)
GOOGLE_KP_ARIA_LABEL_RE = re.compile(
    r'aria-label="[^"]*?\(?\+?1?[\s\u00A0\u202F\-.]?\(?\d{3}\)?[\s\u00A0\u202F\-.]?\d{3}[\s\u00A0\u202F\-.]?\d{4}[^"]*?"',
    re.I
)
GOOGLE_PHONE_DIV_RE = re.compile(  # Io6YTe phone container
    r'<div[^>]+class="[^"]*\bIo6YTe\b[^"]*"[^>]*>.*?(\(?\+?1?[\s\u00A0\u202F\-.]?\(?\d{3}\)?[\s\u00A0\u202F\-.]?\d{3}[\s\u00A0\u202F\-.]?\d{4}.*?)</div>',
    re.I | re.S
)
JSON_PHONE_RE = re.compile(r'"phone[^"]*"\s*:\s*"(\+?[\d\-\s\u00A0\u202F\(\)]{7,})"', re.I)
JSONLD_RE = re.compile(r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', re.I | re.S)

def _format_na_phone(raw: str) -> str:
    """Format 10/11-digit NANP numbers; otherwise return raw."""
    digits = re.sub(r"\D", "", raw or "")
    if len(digits) == 11 and digits.startswith("1"):
        return f"+1 ({digits[1:4]}) {digits[4:7]}-{digits[7:]}"
    if len(digits) == 10:
        return f"({digits[0:3]}) {digits[3:6]}-{digits[6:]}"
    return (raw or "").strip()

def _pick_best_phone(cands: list[str]) -> str | None:
    """Score candidates; prefer separated formats and plausible NANP lengths."""
    scored = []
    seen = set()
    for c in cands or []:
        if not c:
            continue
        if c in seen:
            continue
        seen.add(c)
        digits = re.sub(r"\D", "", c)
        if not (len(digits) == 10 or (len(digits) == 11 and digits.startswith("1"))):
            continue
        score = 0
        if SEPARATED_PHONE_RE.search(c): score += 3
        if "(" in c or ")" in c: score += 1
        if "-" in c or " " in c or "\u00A0" in c or "\u202F" in c: score += 1
        scored.append((score, c))
    if not scored:
        return None
    scored.sort(key=lambda t: t[0], reverse=True)
    return scored[0][1]

def extract_phones(text: str) -> list[str]:
    return list(dict.fromkeys(m.group(0).strip() for m in PHONE_RE.finditer(text or "")))

def extract_emails(text: str) -> list[str]:
    return list(dict.fromkeys(m.group(0) for m in EMAIL_RE.finditer(text or "")))

def extract_addresses(text: str) -> list[str]:
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    candidates: list[str] = []
    for i, ln in enumerate(lines):
        score = 0
        if POSTAL_RE.search(ln): score += 2
        if STREET_HINT_RE.search(ln): score += 1
        if "address" in ln.lower(): score += 1
        if score >= 2:
            candidates.append(ln)
            if i + 1 < len(lines):
                combo = f"{ln} {lines[i+1]}"
                if POSTAL_RE.search(combo) or STREET_HINT_RE.search(combo):
                    candidates.append(combo)
    uniq = sorted(set(candidates), key=len, reverse=True)
    return uniq[:3]

def extract_hours(text: str) -> list[str]:
    lines = [ln.strip() for ln in (text or "").splitlines()]
    out, window, capturing = [], [], False
    for ln in lines:
        if DAY_HINT.search(ln):
            capturing = True
            window.append(ln); continue
        if capturing and ln:
            window.append(ln); continue
        if capturing and not ln:
            break
    for ln in window:
        if DAY_HINT.search(ln) or ":" in ln or "closed" in ln.lower():
            out.append(ln.strip())
    seen, res = set(), []
    for ln in out:
        if ln not in seen:
            seen.add(ln); res.append(ln)
    return res[:10]

def _jsonld_phones(html: str) -> list[str]:
    phones: list[str] = []
    for m in JSONLD_RE.finditer(html or ""):
        raw = (m.group(1) or "").strip()
        try:
            data = json.loads(raw)
        except Exception:
            continue
        objs = data if isinstance(data, list) else [data]
        for o in objs:
            if isinstance(o, dict):
                tel = o.get("telephone")
                if isinstance(tel, str):
                    hit = PHONE_RE.search(tel)
                    if hit:
                        phones.append(hit.group(0))
    return list(dict.fromkeys(phones))

def find_phone_by_dom_labels(html: str) -> str | None:
    """Try itemprop='telephone', tel: links, label-near-number, JSON-LD telephone."""
    if not html:
        return None
    m = ITEMPROP_TEL_RE.search(html)
    if m:
        cand = m.group(1).strip()
        hit = PHONE_RE.search(cand)
        if hit:
            return hit.group(0)
    m = TEL_HREF_RE.search(html)
    if m:
        return m.group(1).strip()
    m = LABEL_NEAR_PHONE_RE.search(html)
    if m:
        return m.group(1).strip()
    phones = _jsonld_phones(html)
    if phones:
        return phones[0]
    return None

# ------------------------ DuckDuckGo HTML search ---------------------------

def _ddg_first_n_links(query: str, n: int = 5) -> list[str]:
    """Query DuckDuckGo's HTML endpoint and return up to n organic result URLs."""
    import scrape as scrapemod
    url = f"https://duckduckgo.com/html/?q={quote_plus(query)}"
    try:
        status, headers, raw = scrapemod.fetch_url(url, timeout=10.0, user_agent=MOBILE_UA)
    except Exception:
        return []
    if "html" not in (headers.get("content-type","") or ""):
        return []
    html = raw.decode(scrapemod.sniff_charset(raw, headers), errors="replace")

    urls: list[str] = []

    # <a class="result__a" href="...">
    for m in re.finditer(r'<a[^>]+class="[^"]*result__a[^"]*"[^>]+href="([^"]+)"', html, re.I):
        href = m.group(1)
        if href.startswith("/l/"):  # DDG redirect
            qs = parse_qs(urlparse(href).query)
            if "uddg" in qs and qs["uddg"]:
                real = unquote(qs["uddg"][0])
                if real.startswith("http"):
                    urls.append(real)
        elif href.startswith("http"):
            urls.append(href)
        if len(urls) >= n:
            break

    # fallback: generic uddg= capture
    if not urls:
        m = re.search(r'href="/l/\?[^"]*uddg=([^"&]+)', html, re.I)
        if m:
            real = unquote(m.group(1))
            if real.startswith("http"):
                urls.append(real)

    # dedupe, preserve order
    out, seen = [], set()
    for u in urls:
        if u not in seen:
            seen.add(u); out.append(u)
    return out[:n]

# ----------------------------- Google helpers ------------------------------

def _google_serp_phone(subject: str) -> tuple[str | None, str]:
    """
    Pull phone from Google SERP knowledge panel (mobile UA, identity encoding).
    Accept only KP (data-dtype="d3ph") or aria-label; never generic SERP hits.
    """
    url = f"https://www.google.com/search?q={quote_plus(subject + ' phone number')}&hl=en"
    html = _fetch_html_direct(url)
    if not html:
        return None, ""
    m = GOOGLE_KP_PHONE_LINK_RE.search(html)
    if m:
        cand = m.group(1).strip()
        hit = PHONE_RE.search(cand)
        if hit:
            return _format_na_phone(hit.group(0)), url
    m = GOOGLE_KP_ARIA_LABEL_RE.search(html)
    if m:
        hit = PHONE_RE.search(m.group(0))
        if hit:
            return _format_na_phone(hit.group(0)), url
    return None, ""

def _maps_direct_phone(subject: str) -> tuple[str | None, str]:
    """
    Google Maps (mobile UA, identity encoding). Accept only:
    - tel: links
    - Io6YTe phone container
    - JSON "phoneNumber"
    (No generic page-wide regex to avoid tracking/IDs.)
    """
    for q in (subject, f"{subject} phone"):
        url = f"https://www.google.com/maps/search/{quote_plus(q)}?hl=en"
        html = _fetch_html_direct(url)
        if not html:
            continue

        cands: list[str] = []

        m = TEL_HREF_RE.search(html)
        if m:
            cands.append(m.group(1).strip())

        m = GOOGLE_PHONE_DIV_RE.search(html)
        if m:
            cands.append(m.group(1).strip())

        m = JSON_PHONE_RE.search(html)
        if m:
            cands.append(m.group(1).strip())

        best = _pick_best_phone(cands)
        if best:
            return _format_na_phone(best), url

    return None, ""

def _ddg_try_phone_from_serp(query: str) -> str | None:
    """Search DDG HTML and try to extract a phone from the SERP itself (best-effort)."""
    import scrape as scrapemod
    url = f"https://duckduckgo.com/html/?q={quote_plus(query)}"
    try:
        status, headers, raw = scrapemod.fetch_url(url, timeout=10.0, user_agent=MOBILE_UA)
    except Exception:
        return None
    if "html" not in (headers.get("content-type","") or ""):
        return None
    html = raw.decode(scrapemod.sniff_charset(raw, headers), errors="replace")

    # Prefer separated on SERP; avoid bare 10-digit when possible
    m = SEPARATED_PHONE_RE.search(html)
    if m:
        return _format_na_phone(m.group(0))
    m = PHONE_RE.search(html)
    return _format_na_phone(m.group(0)) if m else None

# ----------------------------- Orchestrator --------------------------------

def _maps_link(subject: str) -> str:
    return f"https://www.google.com/maps/search/{quote_plus(subject)}"

def _site_search_link(subject: str) -> str:
    return f"https://www.google.com/search?q={quote_plus(subject + ' official site')}"

def _candidate_pages(subject: str, hint: str, max_candidates: int = 5):
    """Yield (url, html, readable) for up to max_candidates organic results."""
    import scrape as scrapemod

    links = _ddg_first_n_links(f"{subject} {hint}".strip(), n=max_candidates)
    for u in links:
        try:
            status, headers, raw = scrapemod.fetch_url(u, timeout=10.0, user_agent=MOBILE_UA)
        except Exception:
            continue
        ctype = headers.get("content-type", "")
        if "html" not in (ctype or ""):
            continue
        enc = scrapemod.sniff_charset(raw, headers)
        try:
            html = raw.decode(enc, errors="replace")
        except Exception:
            html = raw.decode("utf-8", errors="replace")
        readable = scrapemod.extract_readable(html) or ""
        yield u, html, readable

def handle(message: str) -> tuple[bool, str]:
    """
    If this is a quick-facts query, return (True, reply_text).
    Else (False, "") so the app can continue other handlers.
    """
    parsed = parse_fact_query(message)
    if not parsed:
        return False, ""

    kind, subject = parsed
    hint = {
        "phone":   "phone contact",
        "address": "address location",
        "hours":   "hours opening",
        "email":   "email contact",
        "website": "official site",
    }.get(kind, "")

    # Website: just return a search link
    if kind == "website":
        links = [_site_search_link(subject)]
        return True, "Website links:\n" + "\n".join(f"- {x}" for x in links)

    # PHONE pipeline
    if kind == "phone":
        phone, source = _google_serp_phone(subject)
        if not phone:
            phone, src2 = _maps_direct_phone(subject)
            if phone:
                source = src2
        if not phone:
            # Try organic pages
            for url, html, readable in _candidate_pages(subject, "phone contact", max_candidates=5):
                cands = []
                p = find_phone_by_dom_labels(html)
                if p: cands.append(p)
                # also look for separated numbers in visible text
                cands += [m.group(0) for m in SEPARATED_PHONE_RE.finditer(html)]
                cands += [m.group(0) for m in SEPARATED_PHONE_RE.finditer(readable)]
                best = _pick_best_phone(cands)
                if best:
                    phone, source = best, url
                    break
        if not phone:
            # Last resort: DDG SERP itself (best-effort)
            p = _ddg_try_phone_from_serp(f"{subject} phone number")
            if p:
                phone, source = p, "DuckDuckGo results"
        if phone:
            return True, f"Phone for {subject}: {_format_na_phone(phone)}\nSource: {source}"

    # EMAIL / ADDRESS / HOURS via organic pages
    for url, html, readable in _candidate_pages(subject, hint, max_candidates=5):
        if kind == "email":
            emails = extract_emails(html) or extract_emails(readable)
            if emails:
                return True, f"Email for {subject}: {emails[0]}\nSource: {url}"
        elif kind == "address":
            addrs = extract_addresses(readable)
            if addrs:
                return True, f"Address for {subject}: {addrs[0]}\nSource: {url}"
        elif kind == "hours":
            hrs = extract_hours(readable)
            if hrs:
                pretty = "\n".join(hrs)
                return True, f"Hours for {subject}:\n{pretty}\nSource: {url}"

    # Graceful fallback
    lines = [f"Couldn’t extract {kind} reliably. Try these:",
             f"- {_maps_link(subject)}",
             f"- {_site_search_link(subject)}"]
    return True, "\n".join(lines)
