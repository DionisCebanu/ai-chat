#!/usr/bin/env python3
"""
scrape.py — stdlib-only page fetch + readable-text extraction + simple selectors.

Features
- Fetch with urllib (custom User-Agent, redirects, gzip/deflate).
- Charset detection via HTTP headers and <meta charset>.
- HTML parsing via html.parser; removes <script>/<style>.
- Heuristic content blocks; picks the "main" block by text density.
- Optional simple "selector" language:
    - id:   "#main"
    - class: ".article-body"
    - tag:  "article"
    - descendant combos: "#main .post-content" or "article .entry"
  (Supports tag, .class, #id, and whitespace descendant only.)

Limitations
- No JavaScript execution; purely static HTML.
- Some sites block scraping or serve paywalls. Respect robots.txt if you wish.
"""

from __future__ import annotations
from html.parser import HTMLParser
from urllib import request as urlrequest
from urllib import error as urlerror
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser
import gzip, zlib, io, re, time

# ----------------------------- HTTP fetching -------------------------------

def fetch_url(url: str, timeout: float = 10.0, user_agent: str | None = None, respect_robots: bool = False) -> tuple[int, dict, bytes]:
    """
    Return (status_code, headers_lower_dict, raw_body_bytes) or raise URLError/HTTPError.
    """
    if respect_robots and not _allowed_by_robots(url, user_agent or _default_ua()):
        raise urlerror.URLError("Blocked by robots.txt")

    req = urlrequest.Request(url, headers={
        "User-Agent": user_agent or _default_ua(),
        "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
        "Accept-Encoding": "gzip, deflate",
        "Accept-Language": "en-US,en;q=0.8",
        "Connection": "close",
    })
    with urlrequest.urlopen(req, timeout=timeout) as resp:
        status = getattr(resp, "status", 200)
        headers = {k.lower(): v for k, v in resp.getheaders()}
        raw = resp.read()

    # decompress if needed
    ce = headers.get("content-encoding", "").lower()
    if ce == "gzip":
        raw = gzip.decompress(raw)
    elif ce == "deflate":
        try:
            raw = zlib.decompress(raw)
        except zlib.error:
            raw = zlib.decompress(raw, -zlib.MAX_WBITS)

    return status, headers, raw

def _default_ua() -> str:
    return "Mozilla/5.0 (compatible; MiniAI-Scraper/0.1; +https://example.local)"

def _allowed_by_robots(url: str, ua: str) -> bool:
    parsed = urlparse(url)
    robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
    rp = RobotFileParser()
    try:
        rp.set_url(robots_url)
        rp.read()
        return rp.can_fetch(ua, url)
    except Exception:
        # If robots fails to load, default to allow (or set False to be stricter).
        return True

# ------------------------- Charset & title helpers -------------------------

_META_CHARSET_RE = re.compile(
    r'<meta[^>]+charset=["\']?([A-Za-z0-9_\-]+)["\']?', re.I)

_META_CTYPE_RE = re.compile(
    r'<meta[^>]+http-equiv=["\']?content-type["\']?[^>]*content=["\'][^>]*charset=([A-Za-z0-9_\-]+)["\']', re.I)

_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)

def sniff_charset(raw: bytes, headers: dict) -> str:
    # 1) HTTP header
    ctype = headers.get("content-type", "")
    m = re.search(r"charset=([A-Za-z0-9_\-]+)", ctype, re.I)
    if m:
        return m.group(1)
    # 2) meta tags in first 4KB
    head = raw[:4096].decode("latin-1", errors="ignore")
    m = _META_CHARSET_RE.search(head) or _META_CTYPE_RE.search(head)
    if m:
        return m.group(1)
    # 3) default fallback
    return "utf-8"

def extract_title(html_text: str) -> str:
    m = _TITLE_RE.search(html_text)
    if not m:
        return ""
    title = re.sub(r"\s+", " ", m.group(1)).strip()
    return title

# ---------------------- HTML parsing & block scoring -----------------------

_TEXT_TAGS = {"p", "li", "blockquote", "pre"}
_BLOCK_TAGS = {"article", "main", "section", "div", "td"} | _TEXT_TAGS
_SKIP_TAGS = {"script", "style", "noscript"}
_BAD_HINTS = {"nav", "menu", "header", "footer", "sidebar", "promo", "ads", "social", "share", "related", "cookie"}
_GOOD_HINTS = {"content", "article", "post", "entry", "main", "body", "text", "read"}
_MAX_NODES = 120_000
_TEXTY = {"p", "li", "blockquote", "pre", "h1", "h2", "h3"}

class _Node:
    __slots__ = ("tag","id","classes","text","links_text_len","children","parent")
    def __init__(self, tag="", id_="", classes=None, parent=None):
        self.tag = tag
        self.id = id_
        self.classes = set(classes or [])
        self.text = []
        self.links_text_len = 0
        self.children: list[_Node] = []
        self.parent = parent

    def add_text(self, s: str):
        self.text.append(s)

    def get_text(self) -> str:
        return "".join(self.text)

class _Extractor(HTMLParser):
    def __init__(self, wanted_selector=None):
        super().__init__(convert_charrefs=True)
        self.root = _Node(tag="root")
        self.stack = [self.root]
        self.in_skip = 0
        self.wanted = _parse_selector(wanted_selector) if wanted_selector else None
        self.matched_nodes: list[_Node] = []

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        attrs = dict(attrs)
        if tag in _SKIP_TAGS:
            self.in_skip += 1
            return

        id_ = (attrs.get("id") or "").strip()
        classes = (attrs.get("class") or "").strip().split()
        node = _Node(tag=tag, id_=id_, classes=classes, parent=self.stack[-1])
        self.stack[-1].children.append(node)
        self.stack.append(node)

        if tag == "a":
            # mark link context via a sentinel child
            node.add_text("\x00LINK\x00")

        # Selector capture: naive descendant matching
        if self.wanted and tag in _BLOCK_TAGS:
            if _matches_selector_chain(node, self.wanted):
                self.matched_nodes.append(node)

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in _SKIP_TAGS:
            if self.in_skip > 0:
                self.in_skip -= 1
            return
        if self.stack and self.stack[-1].tag == tag:
            finished = self.stack.pop()
            # propagate child link text lengths up
            links_len = finished.links_text_len
            for ch in finished.children:
                links_len += ch.links_text_len
            # count own links marker -> estimate link density
            own_text = finished.get_text()
            finished.links_text_len = links_len + own_text.count("\x00LINK\x00")
            # remove markers from text
            if "\x00LINK\x00" in own_text:
                cleaned = own_text.replace("\x00LINK\x00", "")
                finished.text = [cleaned]

    def handle_data(self, data):
        if self.in_skip:
            return
        if not data.strip():
            # keep some whitespace for natural breaks
            self.stack[-1].add_text(" ")
            return
        self.stack[-1].add_text(data)

def _parse_selector(selector: str):
    """
    Parse basic selectors like:
      "#main", ".article", "article", "#main .post", "article .entry"
    Returns list of simple parts for descendant matching.
    """
    parts = []
    for raw in selector.split():
        raw = raw.strip()
        if not raw: continue
        if raw.startswith("#"):
            parts.append(("id", raw[1:]))
        elif raw.startswith("."):
            parts.append(("class", raw[1:]))
        else:
            parts.append(("tag", raw.lower()))
    return parts

def _matches_selector_chain(node: _Node, chain) -> bool:
    """
    Naive right-to-left descendant matching for our simple chain.
    """
    i = len(chain) - 1
    cur: _Node | None = node
    while i >= 0 and cur:
        kind, val = chain[i]
        if kind == "id" and cur.id != val:
            cur = cur.parent; continue
        if kind == "class" and (val not in cur.classes):
            cur = cur.parent; continue
        if kind == "tag" and cur.tag != val:
            cur = cur.parent; continue
        i -= 1
        cur = cur.parent
    return i < 0

def _score_node(node: _Node) -> float:
    """
    Score a node for "main content" likelihood.
    Factors:
      + words count
      + punctuation weight
      - link density penalty
      + hint bonus on id/class
      + tag bonus for article/main/section
    """
    text = node.get_text()
    words = len(text.split())
    if words == 0:
        return 0.0
    punct = text.count(".") + text.count(",") + text.count("!") + text.count("?")
    link_penalty = min(1.0, node.links_text_len / max(1, len(text))) * 2.0
    hint = 0.0
    idc = f"{node.id} {' '.join(node.classes)}".lower()
    if any(h in idc for h in _GOOD_HINTS): hint += 2.0
    if any(h in idc for h in _BAD_HINTS): hint -= 2.0
    tag_bonus = 1.5 if node.tag in {"article","main"} else (0.5 if node.tag in {"section","div"} else 0.0)
    return (words * 0.8) + (punct * 0.8) - link_penalty + hint + tag_bonus

def _collect_text(node: _Node) -> str:
    """
    Collect block text with simple line breaks between paragraphs/list items.
    Iterative to avoid recursion depth issues.
    """
    lines = []
    stack = [node]
    visited = 0
    while stack:
        n = stack.pop()
        visited += 1
        if visited > _MAX_NODES:
            break
        if n.tag in _TEXTY:
            t = " ".join(n.get_text().split())
            if t:
                lines.append(t)
        # push children
        if n.children:
            stack.extend(reversed(n.children))  # keep doc order nicer
    text = "\n".join(lines).strip()
    if not text:
        text = " ".join(node.get_text().split()).strip()
    # collapse excessive newlines
    return re.sub(r"\n{3,}", "\n\n", text)


# ---------------------------- Public functions -----------------------------

def extract_readable(html_text: str, selector: str | None = None) -> str:
    """
    Return best-effort readable text from HTML.
    If selector is provided and matches, prefer that; else fall back to best-scored block.
    """
    parser = _Extractor(wanted_selector=selector)
    parser.feed(html_text)
    parser.close()

    # Selector path (prefer explicit user intent)
    if parser.matched_nodes:
        best_sel = max(parser.matched_nodes, key=_score_node)
        text = _collect_text(best_sel)
        if text:
            return text

    # Heuristic best block across all
    # Heuristic best block across all
    # Heuristic best block across all (ITERATIVE)
    candidates = []
    stack = [parser.root]
    visited = 0
    while stack:
        n = stack.pop()
        visited += 1
        if visited > _MAX_NODES:
            break
        if n.tag in _BLOCK_TAGS:
            candidates.append(n)
        # push children
        if n.children:
            stack.extend(n.children)



    if not candidates:
        return ""

    best = max(candidates, key=_score_node)
    return _collect_text(best)

def scrape_first_result(subject: str, selector: str | None = None, num_results: int = 3, max_chars: int = 1500) -> tuple[str, str, str]:
    """
    Search using link.get_links(subject), fetch first URL, extract text.
    Returns (url, title, text[:max_chars]).
    """
    # Import locally to avoid hard dependency if link.py not present in other contexts
    import link as linkmod

    urls = linkmod.get_links(subject, num_results=num_results)
    if not urls:
        raise RuntimeError("No search results.")

    url = urls[0]
    status, headers, raw = fetch_url(url)
    ctype = headers.get("content-type","")
    if "html" not in ctype:
        return url, "", f"(Non-HTML content: {ctype})"

    enc = sniff_charset(raw, headers)
    try:
        html = raw.decode(enc, errors="replace")
    except Exception:
        html = raw.decode("utf-8", errors="replace")

    title = extract_title(html)
    text = extract_readable(html, selector=selector)
    if not text:
        text = "(Sorry, couldn’t extract readable text.)"

    if max_chars and len(text) > max_chars:
        text = text[:max_chars].rstrip() + " …"

    return url, title, text

# ------------------------------ Command parser -----------------------------

# Accept messages like:
#  - "read about Kyoto"
#  - "scrape Tesla Model 3 selector: #main .post-content"
#  - "read about coffee id: content"
#  - "read about coffee class: article-body"
#  - "read about coffee tag: article"
_CMD_RE = re.compile(
    r"^(?:read(?:\s+about)?|scrape|summarize)\s+(.+?)(?:\s+(selector|id|class|tag)\s*:\s*([#.\w\- ]+))?\s*$",
    re.I
)

def parse_scrape_command(message: str) -> tuple[str | None, str | None]:
    """
    Return (subject, selector_or_hint) or (None, None) if not a scrape command.
    """
    m = _CMD_RE.match((message or "").strip())
    if not m:
        return None, None
    subject = m.group(1).strip()
    how = (m.group(2) or "").lower()
    val = (m.group(3) or "").strip()
    selector = None
    if how == "selector":
        selector = val
    elif how == "id":
        selector = f"#{val}"
    elif how == "class":
        selector = f".{val}"
    elif how == "tag":
        selector = val.lower()
    return subject, (selector or None)
