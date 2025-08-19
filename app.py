#!/usr/bin/env python3
# Flask chat with modular pipeline: tools → Gemini → (optional) KB/learn

from flask import Flask, request, jsonify, render_template, session
import json, uuid, re, time, threading, string
from pathlib import Path

# Specialized tools
import utils.weather as weather       # Open-Meteo based weather helper (handle(message) -> (handled, reply))
import utils.translate as translate   # Translation helper (handle(message) -> (handled, reply))
import link                           # buy/search/link-to helper
import scrape                         # "read <subject> [selector]" command
import image_search                   # image returner ("Image: <url>")
import quickfacts                     # phone / address / hours / email / website
import compare                        # "X or Y" / "X vs Y" compare helper
import genai_router                   # Gemini keyword/prefix router (handle(sess, msg, session_id) -> (handled, reply))
from autolearn import AutoLearner

# -----------------------------------------------------------------------------
# Feature switches (tweak without changing pipeline)
# -----------------------------------------------------------------------------
GENAI_PRIMARY = True           # Gemini answers most things
ENABLE_SMALLTALK_LOCAL = True  # Keep tiny local responses (hi/thanks/who/time/help)
ENABLE_QUICKFACTS = True       # Deterministic quick facts
ENABLE_COMPARE = True          # Compare "X or Y"
ENABLE_LINKS = True            # Link lookup
ENABLE_KB = False              # Static KB fallback (off by default)
ENABLE_AUTOTRAIN = False       # Start learning only when explicitly asked

# -----------------------------------------------------------------------------
# Flask app & session store
# -----------------------------------------------------------------------------
app = Flask(__name__, template_folder="templates", static_folder="static")
app.config["SECRET_KEY"] = "dev-change-me"  # session cookie

SESSIONS = {}                               # sid -> {"history":[(role,text)], "created_at":ts, "vars":{}}
LOCK = threading.RLock()

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

# -----------------------------------------------------------------------------
# KB routing (only used if ENABLE_KB = True)
# -----------------------------------------------------------------------------
KB_DIR = Path(__file__).with_name("kb")
ROUTER_FILE = KB_DIR / "router.json"
LEARNER = AutoLearner(KB_DIR, ROUTER_FILE)

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

STOPWORDS = {
    "the","a","an","and","or","vs","versus","to","of","for","is","are","be",
    "whats","what","which","who","do","does","did","i","you","we","they",
    "it","in","on","at","by","with","from","than"
}
PUNCT = str.maketrans("", "", string.punctuation)

def tokenize(s: str) -> set:
    s = s.lower().translate(PUNCT)
    return {t for t in s.split() if t and t not in STOPWORDS}

def route_topics(user_text: str, top_k: int = 2):
    load_router()
    if not ROUTER:
        return ["general"]
    utoks = tokenize(user_text)
    scores = []
    for topic, kws in ROUTER.items():
        overlap = len(utoks & kws)
        if overlap == 0:
            continue
        jacc = overlap / max(1, len(utoks | kws))
        score = overlap + 0.5 * jacc
        scores.append((score, topic))
    if not scores:
        return ["general"]
    scores.sort(reverse=True)
    topics = [t for _, t in scores[:top_k]]
    if "general" not in topics:
        topics.append("general")
    return topics

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
            for e in entries:
                e["ptokens"] = [tokenize(p) for p in e["patterns"]]
            TOPIC_CACHE[topic] = {"mtime": m, "entries": entries}
            print(f"[KB] Loaded {len(entries)} entries: {f.name}")
        except Exception as e:
            print(f"[KB] Failed to load {f.name}:", e)
            TOPIC_CACHE.pop(topic, None)
            return []
    return TOPIC_CACHE[topic]["entries"]

def _tok(s: str) -> list[str]:
    return [w for w in re.findall(r"[a-z0-9]+", (s or "").lower()) if w not in STOPWORDS]

def _f1(overlap: int, q: int, p: int) -> float:
    if q == 0 or p == 0 or overlap == 0:
        return 0.0
    prec = overlap / p
    rec  = overlap / q
    return (2 * prec * rec) / (prec + rec)

def kb_lookup_in_entries(sess, user_message: str, entries: list[dict]) -> str | None:
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
            score = _f1(len(overlap_set), len(q_tokens), len(p_tokens))
            if pat.lower() in (user_message or "").lower():
                score += 0.25
            if score > best_score:
                best_score = score
                best = reply
    return best if best_score >= 0.50 else None

# -----------------------------------------------------------------------------
# Smalltalk / fallback
# -----------------------------------------------------------------------------
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

def simple_ai_reply(sess, user_msg):
    text = user_msg.strip()
    lower = text.lower()

    m = re.search(r"\b(my name is|call me)\s+([A-Za-z][A-Za-z'\-]+)\b", text, re.IGNORECASE)
    if m:
        token = m.group(2)
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

_SMALLTALK_PATTERNS = [
    r"^\s*(hi|hello|hey|yo|howdy)\b",
    r"\bwho\s+are\s+you\??",
    r"\b(help|what\s+can\s+you\s+do)\b",
    r"\b(what\s+time|time\??)\b",
    r"\b(what\s+day|date)\b",
    r"^(ok|okay|thanks|thank you)\b",
    r"\b(goodbye|bye|see\s+you)\b",
]

def should_answer_locally(msg: str) -> bool:
    if not ENABLE_SMALLTALK_LOCAL:
        return False
    m = (msg or "").lower()
    return any(re.search(p, m, re.I) for p in _SMALLTALK_PATTERNS)

def is_explicit_learn_trigger(msg: str) -> bool:
    m = (msg or "").lower().strip()
    return m.startswith(("teach:", "train:", "learn:", "add pattern:", "add topic:"))

# -----------------------------------------------------------------------------
# Main pipeline
# -----------------------------------------------------------------------------
def list_all_topics() -> list[str]:
    KB_DIR.mkdir(parents=True, exist_ok=True)
    return sorted(p.stem for p in KB_DIR.glob("*.json") if p.name != "router.json")

def _genai_direct(msg: str) -> str | None:
    """Direct Gemini call via aiapi if router doesn't trigger."""
    try:
        import aiapi
        fn = getattr(aiapi, "text_reply", None) or getattr(aiapi, "get_gemini_general_response", None)
        if callable(fn):
            out = (fn(msg) or "").strip()
            return out or None
    except Exception as e:
        app.logger.warning("GenAI direct error: %s", e)
    return None


def generate_reply(user_message):
    """
    Priority:
      0) Continue learn if already in-progress
      1) Images
      2) 'read ...' scrape
      3) Quickfacts
      3.5) Weather
      3.6) Translate
      4) Local smalltalk
      5) Gemini (primary)
      6) Optional: links / compare
      7) Optional: KB
      8) Optional: start learning only if explicitly asked
      9) Friendly fallback
    """
    sid, sess = get_or_make_sid()
    msg = (user_message or "").strip()
    reply = None

    # 0) Continue learning if already mid-flow
    learn_state = (sess.get("vars") or {}).get("learn", {}).get("state", "idle")
    if learn_state in ("await_topic", "await_reply", "await_new_topic_keywords"):
        try:
            handled, learn_reply = LEARNER.handle(sess, msg)
            if handled:
                reply = learn_reply
        except Exception as e:
            reply = f"Sorry, I hit an error continuing training: {e}"

    # 1) Images
    if reply is None:
        try:
            handled, img_reply = image_search.handle(msg)
            if handled:
                reply = img_reply
        except Exception:
            reply = None

    # 2) Scrape "read ..."
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

    # 3) Quickfacts
    if reply is None and ENABLE_QUICKFACTS:
        try:
            handled, qf_reply = quickfacts.handle(msg)
            if handled:
                reply = qf_reply
        except Exception:
            reply = None

    # 3.5) Weather
    if reply is None:
        try:
            handled, w_reply = weather.handle(msg)
            if handled:
                reply = w_reply
        except Exception:
            reply = None

    # 3.6) Translate
    if reply is None:
        try:
            handled, tr_reply = translate.handle(msg)
            if handled:
                reply = tr_reply
        except Exception:
            reply = None

    # 4) Local smalltalk
    if reply is None and should_answer_locally(msg):
        try:
            reply = simple_ai_reply(sess, msg)
        except Exception:
            reply = None

    # 5) Gemini (primary brain)
    if reply is None and GENAI_PRIMARY:
        try:
            handled, g_reply = genai_router.handle(sess, msg, session_id=sid)
            if handled and g_reply:
                reply = g_reply
        except Exception as e:
            app.logger.warning("genai_router error: %s", e)
            reply = None

        # NEW: guaranteed Gemini fallback if router didn't handle
        if reply is None:
            direct = _genai_direct(msg)
            if direct:
                reply = direct

    # 6) Optional: links / compare
    if reply is None and ENABLE_LINKS:
        try:
            handled, link_reply = link.handle(msg, max_results=3)
            if handled:
                reply = link_reply
        except Exception:
            reply = None

    if reply is None and ENABLE_COMPARE:
        try:
            handled, cmp_reply = compare.handle(msg)
            if handled:
                reply = cmp_reply
        except Exception:
            reply = None

    # 7) Optional KB
    if reply is None and ENABLE_KB:
        try:
            topics = route_topics(msg, top_k=2)
        except Exception:
            topics = []
        try:
            for t in topics:
                entries = load_topic(t)
                reply = kb_lookup_in_entries(sess, msg, entries)
                if reply:
                    break
            if reply is None:
                for t in list_all_topics():
                    entries = load_topic(t)
                    reply = kb_lookup_in_entries(sess, msg, entries)
                    if reply:
                        break
        except Exception:
            reply = None

    # 8) Start learning only if explicitly asked
    if reply is None and ENABLE_AUTOTRAIN and is_explicit_learn_trigger(msg):
        try:
            handled, learn_reply = LEARNER.handle(sess, msg)
            if handled:
                reply = learn_reply
        except Exception as e:
            reply = f"Sorry, I hit an error starting training: {e}"

    # 9) Friendly fallback
    if reply is None:
        try:
            reply = simple_ai_reply(sess, msg)
        except Exception as e:
            reply = f"Sorry, I couldn't process that: {e}"

    # Persist history
    with LOCK:
        sess.setdefault("history", [])
        sess["history"].append(("user", msg))
        sess["history"] = sess["history"][-10:]
        sess["history"].append(("assistant", reply))

    return sid, reply

# -----------------------------------------------------------------------------
# Routes
# -----------------------------------------------------------------------------
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
    TOPIC_CACHE.clear()
    return jsonify(ok=True, topics=list(ROUTER.keys()))

@app.get("/admin/route")
def admin_route_preview():
    q = request.args.get("q","")
    return jsonify(query=q, topics=route_topics(q))

if __name__ == "__main__":
    load_router(force=True)
    app.run(host="127.0.0.1", port=8082, debug=True)
