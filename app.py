#!/usr/bin/env python3
# Flask chat with topic-based modular KB (std lib + Flask only)

from flask import Flask, request, jsonify, render_template, session
import json, uuid, re, time, threading, os, string
from pathlib import Path
from autolearn import AutoLearner
import link  # our link.py helper
import scrape
import image_search  # image returner
import quickfacts  # quick facts extractor (email, phone, address...)
import compare


app = Flask(__name__, template_folder="templates", static_folder="static")
app.config["SECRET_KEY"] = "dev-change-me"  # session cookie

# ---------- In-memory sessions ----------
SESSIONS = {}  # sid -> {"history":[(role,text)], "created_at":ts, "vars":{}}
LOCK = threading.RLock()  # re-entrant: avoids deadlocks

def new_session():
    sid = str(uuid.uuid4())
    with LOCK:
        SESSIONS[sid] = {"history": [], "created_at": time.time(), "vars": {}}
    return sid

def get_or_make_sid():
    sid = session.get("sid")
    with LOCK:
        exists = sid in SESSIONS if sid else False
    if not exists:
        sid = new_session()
        session["sid"] = sid
    with LOCK:
        return sid, SESSIONS[sid]

# ---------- Paths ----------
KB_DIR = Path(__file__).with_name("kb")
ROUTER_FILE = KB_DIR / "router.json"
LEARNER = AutoLearner(KB_DIR, ROUTER_FILE)

# ---------- Router (topic keywords) ----------
ROUTER = {}         # topic -> set(keywords)
ROUTER_MTIME = 0.0

def load_router(force=False):
    global ROUTER, ROUTER_MTIME
    try:
        m = ROUTER_FILE.stat().st_mtime
    except FileNotFoundError:
        if force:
            ROUTER, ROUTER_MTIME = {}, 0.0
        return
    if force or m > ROUTER_MTIME:
        try:
            data = json.loads(ROUTER_FILE.read_text(encoding="utf-8"))
            ROUTER = {topic: set(k.lower() for k in kws) for topic, kws in data.items() if isinstance(kws, list)}
            ROUTER_MTIME = m
            print(f"[Router] Loaded {len(ROUTER)} topics from {ROUTER_FILE.name}")
        except Exception as e:
            print("[Router] Failed to load:", e)

# ---------- Tokenization ----------
STOPWORDS = {
    "the","a","an","and","or","vs","versus","to","of","for","is","are","be",
    "whats","what","which","who","do","does","did","i","you","we","they",
    "it","in","on","at","by","with","from","than"
}

PUNCT = str.maketrans("", "", string.punctuation)

def tokenize(s: str) -> set:
    s = s.lower().translate(PUNCT)
    return {t for t in s.split() if t and t not in STOPWORDS}

# ---------- Topic routing ----------
def route_topics(user_text: str, top_k: int = 2):
    """Score topics by keyword overlap; return best topics."""
    load_router()
    if not ROUTER:
        return ["general"]
    utoks = tokenize(user_text)
    scores = []
    for topic, kws in ROUTER.items():
        overlap = len(utoks & kws)
        if overlap == 0:
            continue
        # mild length norm (Jaccard-ish)
        jacc = overlap / max(1, len(utoks | kws))
        score = overlap + 0.5 * jacc
        scores.append((score, topic))
    if not scores:
        return ["general"]
    scores.sort(reverse=True)
    topics = [t for _, t in scores[:top_k]]
    # Always consider 'general' as fallback
    if "general" not in topics:
        topics.append("general")
    return topics

# ---------- Topic KB cache ----------
TOPIC_CACHE = {}  # topic -> {"mtime": float, "entries": list}

def normalize_kb(data):
    entries = []
    if isinstance(data, dict):
        for pat, rep in data.items():
            entries.append({"patterns":[pat], "reply":rep})
    elif isinstance(data, list):
        for item in data:
            if not isinstance(item, dict): 
                continue
            pats = item.get("patterns") or []
            pats = [p for p in pats if isinstance(p, str) and p.strip()]
            if not pats:
                continue
            rep = item.get("reply", "")
            entries.append({"patterns": pats, "reply": rep})
    return entries

def load_topic(topic: str, force=False):
    """Load kb/<topic>.json into cache if changed."""
    f = KB_DIR / f"{topic}.json"
    try:
        m = f.stat().st_mtime
    except FileNotFoundError:
        TOPIC_CACHE.pop(topic, None)
        return []

    cached = TOPIC_CACHE.get(topic)
    if force or not cached or m > cached["mtime"]:
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            entries = normalize_kb(data)
            # Precompute tokens per pattern for scoring
            for e in entries:
                e["ptokens"] = [tokenize(p) for p in e["patterns"]]
            TOPIC_CACHE[topic] = {"mtime": m, "entries": entries}
            print(f"[KB] Loaded {len(entries)} entries: {f.name}")
        except Exception as e:
            print(f"[KB] Failed to load {f.name}:", e)
            TOPIC_CACHE.pop(topic, None)
            return []
    return TOPIC_CACHE[topic]["entries"]

# ---------- KB lookup within topic ----------
def _tok(s: str) -> list[str]:
    return [w for w in re.findall(r"[a-z0-9]+", (s or "").lower()) if w not in STOPWORDS]

def _f1(overlap: int, q: int, p: int) -> float:
    if q == 0 or p == 0 or overlap == 0:
        return 0.0
    prec = overlap / p
    rec  = overlap / q
    return (2 * prec * rec) / (prec + rec)

def kb_lookup_in_entries(sess, user_message: str, entries: list[dict]) -> str | None:
    """
    Return the best reply from entries only if there's meaningful token overlap
    (ignoring stopwords). Prevents 'Mercedes or BMW' from matching 'i5 or i7'.
    """
    q_tokens = set(_tok(user_message))
    best = None
    best_score = 0.0

    for item in entries or []:
        patterns = item.get("patterns") or []
        reply = item.get("reply") or item.get("response") or ""
        if not reply:
            continue

        for pat in patterns:
            p_tokens = set(_tok(pat))
            if not p_tokens:
                continue

            overlap_set = q_tokens & p_tokens
            if not overlap_set:
                continue

            # Require at least one NON-stopword in common (already enforced by _tok)
            # Optional: minimum overlap size
            if len(overlap_set) < 1:
                continue

            # Score by F1 on tokens; boost if exact substring hit
            score = _f1(len(overlap_set), len(q_tokens), len(p_tokens))
            if pat.lower() in (user_message or "").lower():
                score += 0.25  # small bonus for literal phrase

            if score > best_score:
                best_score = score
                best = reply

    # Only accept if it looks genuinely relevant
    return best if best_score >= 0.50 else None

def render_template_reply(template: str, sess, user_text: str) -> str:
    name = (sess.get("vars") or {}).get("name", "")
    comma_name = f", {name}" if name else ""
    class _D(dict):
        def __missing__(self, key): return ""
    ctx = _D(name=name, comma_name=comma_name, user_text=user_text)
    try:
        return template.format_map(ctx)
    except Exception:
        return template

# ---------- Simple fallback (general chatter) ----------
def simple_ai_reply(sess, user_msg):
    text = user_msg.strip()
    lower = text.lower()

    # remember name
    m = re.search(r"\b(my name is|call me)\s+([A-Za-z][A-Za-z'\-]+)\b", text, re.IGNORECASE)
    if m:
        token = m.group(2)
        # keep original casing from the message
        orig = re.search(rf"\b({re.escape(token)})\b", text, re.IGNORECASE)
        if orig:
            sess["vars"]["name"] = orig.group(1)

    if any(w in lower for w in ["hello", "hi", "hey", "yo", "howdy"]):
        name = sess["vars"].get("name")
        return f"Hey {name}! What’s on your mind?" if name else "Hey! What’s on your mind?"

    if "time" in lower or "what time" in lower:
        return f"It’s {time.strftime('%H:%M')}."
    if any(k in lower for k in ["date", "what day"]):
        return f"Today is {time.strftime('%A %d %B %Y')}."

    if any(k in lower for k in ["who are you","tu es qui","qui es-tu"]):
        return "I’m a simple chat buddy with topic files (travel, hobbies, car, general)."

    if text.endswith("?"):
        return "Good question! Want to give a bit more detail?"

    if "help" in lower:
        return "Tell me what’s on your mind, or try topics like travel, hobbies, car, or general."
    
    return "Got it. Tell me more so I can help better."

def list_all_topics() -> list[str]:
    """
    Return all topic names that have a kb/<topic>.json file,
    excluding the router.json file.
    """
    KB_DIR.mkdir(parents=True, exist_ok=True)  # ensure folder exists
    return sorted(p.stem for p in KB_DIR.glob("*.json") if p.name != "router.json")

# ---------- Orchestrate: route → topic lookup(s) → fallback ----------
def generate_reply(user_message):
    """
    Orchestrates the reply pipeline:

    0) If mid-learning → continue learning (short-circuit)
    1) Images (image_search)
    2) Scrape commands: "read <subject> [selector]" (scrape.parse_scrape_command)
    3) Quick facts: phone/address/hours/email/website (quickfacts.handle)
    4) Compare: "X or Y" / "X vs Y" (compare.handle)
    5) Links: buy/search/link-to (link.handle)
    6) Topic KB: routed topics first, then full scan (kb_lookup_in_entries)
    7) Learning flow start (LEARNER.handle) if still unknown
    8) Friendly fallback (simple_ai_reply)

    Always appends to session history and returns (sid, reply).
    """
    sid, sess = get_or_make_sid()
    msg = (user_message or "").strip()
    reply = None

    # ---- A) If we're mid-learning, handle FIRST and short-circuit ----------
    learn_state = (sess.get("vars") or {}).get("learn", {}).get("state", "idle")
    if learn_state in ("await_topic", "await_reply", "await_new_topic_keywords"):
        try:
            handled, learn_reply = LEARNER.handle(sess, msg)
            if handled:
                reply = learn_reply
        except Exception as e:
            reply = f"Sorry, I hit an error continuing training: {e}"

    # ---- B) Normal pipeline -------------------------------------------------
    # 0) Images
    if reply is None:
        try:
            handled, img_reply = image_search.handle(msg)
            if handled:
                reply = img_reply
        except Exception as e:
            # don't fail the whole request
            reply = None

    # 1) Scrape commands
    if reply is None:
        try:
            subject, selector = scrape.parse_scrape_command(msg)
            if subject:
                url, title, text = scrape.scrape_first_result(
                    subject, selector=selector, num_results=3, max_chars=1500
                )
                title_line = f"{title}\n" if title else ""
                reply = f"Source: {url}\n{title_line}{text}".strip()
        except Exception as e:
            reply = f"Could not fetch content ({e})."

    # 2) Quick facts (phone/address/hours/email/website)
    if reply is None:
        try:
            handled, qf_reply = quickfacts.handle(msg)
            if handled:
                reply = qf_reply
        except Exception:
            reply = None

    # 3) Compare handler ("X or Y" / "X vs Y")
    if reply is None:
        try:
            handled, cmp_reply = compare.handle(msg)
            if handled:
                reply = cmp_reply
        except Exception:
            reply = None

    # 4) Links (buy/search/link-to)
    if reply is None:
        try:
            handled, link_reply = link.handle(msg, max_results=3)
            if handled:
                reply = link_reply
        except Exception:
            reply = None

    # 5) Topic KB (routed → all topics)
    if reply is None:
        try:
            topics = route_topics(msg, top_k=2)
        except Exception:
            topics = []

        # Routed topics first
        try:
            for t in topics:
                entries = load_topic(t)
                reply = kb_lookup_in_entries(sess, msg, entries)
                if reply:
                    break
            # If nothing yet, scan all topics
            if reply is None:
                for t in list_all_topics():
                    entries = load_topic(t)
                    reply = kb_lookup_in_entries(sess, msg, entries)
                    if reply:
                        break
        except Exception:
            reply = None

    # 6) Start learning flow (if still unknown)
    if reply is None:
        try:
            handled, learn_reply = LEARNER.handle(sess, msg)
            if handled:
                reply = learn_reply
        except Exception as e:
            reply = f"Sorry, I hit an error starting training: {e}"

    # 7) Friendly fallback
    if reply is None:
        try:
            reply = simple_ai_reply(sess, msg)
        except Exception as e:
            reply = f"Sorry, I couldn't process that: {e}"

    # ---- Persist history ----------------------------------------------------
    with LOCK:
        sess.setdefault("history", [])
        sess["history"].append(("user", msg))
        sess["history"] = sess["history"][-10:]
        sess["history"].append(("assistant", reply))

    return sid, reply







# ---------- Routes ----------
@app.get("/")
def index():
    load_router()
    get_or_make_sid()
    return render_template("index.html")

@app.post("/chat")
def chat():
    data = request.get_json(silent=True) or {}
    message = (data.get("message") or "").strip()
    if not message:
        return jsonify(error="Missing 'message' field"), 400
    sid, reply = generate_reply(message)
    return jsonify(session_id=sid, message=message, reply=reply)

@app.get("/health")
def health():
    return "ok", 200, {"Content-Type": "text/plain; charset=utf-8"}

# Debug helpers
@app.get("/admin/reload")
def admin_reload():
    load_router(force=True)
    # clear topic cache to force reload next request
    TOPIC_CACHE.clear()
    return jsonify(ok=True, topics=list(ROUTER.keys()))

@app.get("/admin/route")
def admin_route_preview():
    q = request.args.get("q","")
    return jsonify(query=q, topics=route_topics(q))

if __name__ == "__main__":
    load_router(force=True)
    app.run(host="127.0.0.1", port=8082, debug=True)
