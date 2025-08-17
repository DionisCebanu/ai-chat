#!/usr/bin/env python3
# Flask-based mini AI chat (serves the HTML and the /chat API)

from flask import Flask, request, jsonify, render_template, session
import uuid, re, time, threading

app = Flask(__name__, template_folder="templates", static_folder="static")
app.config["SECRET_KEY"] = "dev-change-me"  # used for session cookies

# --- In-memory sessions ------------------------------------------------------
SESSIONS = {}  # sid -> {"history": [(role, text), ...], "created_at": ts, "vars": {}}
LOCK = threading.RLock()

def new_session():
    sid = str(uuid.uuid4())
    with LOCK:
        SESSIONS[sid] = {"history": [], "created_at": time.time(), "vars": {}}
    return sid

def get_or_make_sid():
    sid = session.get("sid")
    with LOCK:
        if not sid or sid not in SESSIONS:
            sid = new_session()
            session["sid"] = sid
    return sid, SESSIONS[sid]

# --- Simple “AI” -------------------------------------------------------------
def simple_ai_reply(sess, user_msg):
    text = user_msg.strip()
    lower = text.lower()

    # Remember name if user introduces themselves
    m = re.search(r"(je m'appelle|je suis|my name is)\s+([A-Za-zÀ-ÖØ-öø-ÿ'\-]+)", lower)
    if m:
        token = m.group(2)
        orig = re.search(rf"({token})", text, re.IGNORECASE)
        if orig:
            sess["vars"]["name"] = orig.group(1)

    # Greetings
    if any(w in lower for w in ["salut", "bonjour", "bonsoir", "hey", "hello", "hi", "coucou"]):
        name = sess["vars"].get("name")
        return f"Hi {name}! How can I help?" if name else "Hi! How can I help?"

    # Time / date
    if "time" in lower or "heure" in lower:
        return f"It’s {time.strftime('%H:%M')}."
    if any(k in lower for k in ["date", "what day", "quel jour"]):
        return f"Today is {time.strftime('%A %d %B %Y')}."

    # Identity
    if any(k in lower for k in ["who are you", "tu es qui", "qui es-tu"]):
        return "I’m a tiny Flask chatbot running pure Python logic. 😊"

    # Help
    if "help" in lower or "aide" in lower:
        return "Ask me the time, tell me your name, or any simple question. I keep short context per session."

    # Thanks / bye
    if any(k in lower for k in ["thanks", "merci", "thx"]):
        return "You’re welcome!"
    if any(k in lower for k in ["bye", "au revoir", "see you"]):
        return "See you soon!"

    # Questions
    if text.endswith("?"):
        name = sess["vars"].get("name")
        prefix = f"{name}, " if name else ""
        return f"{prefix}good question! I’m very simple for now—try rephrasing or give more detail."

    # Fallback
    return "Got it. Could you clarify what you need?"

def generate_reply(user_message):
    sid, sess = get_or_make_sid()
    sess["history"].append(("user", user_message))
    sess["history"] = sess["history"][-10:]
    reply = simple_ai_reply(sess, user_message)
    sess["history"].append(("assistant", reply))
    return sid, reply

# --- Routes ------------------------------------------------------------------
@app.get("/")
def index():
    get_or_make_sid()  # ensure sid exists
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

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8082, debug=True)
