#!/usr/bin/env python3
# mini_ai_chat.py — 100% Python standard library chat API

from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse
import json, uuid, re, time, threading

HOST = "127.0.0.1"
PORT = 8082

# --- In-memory sessions ------------------------------------------------------
SESSIONS = {}  # session_id -> {"history": [(role, text), ...], "created_at": ts, "vars": {}}
LOCK = threading.Lock()

def new_session():
    sid = str(uuid.uuid4())
    with LOCK:
        SESSIONS[sid] = {"history": [], "created_at": time.time(), "vars": {}}
    return sid

def get_session(sid=None):
    if not sid or sid not in SESSIONS:
        sid = new_session()
    return sid, SESSIONS[sid]

# --- Super simple “AI” -------------------------------------------------------
def simple_ai_reply(session, user_msg):
    """
    Minimal heuristics:
    - greetings, time/date, identity/help
    - remember name: "my name is X" / "je m'appelle X"
    - light context echo for unknown questions
    """
    text = user_msg.strip()
    lower = text.lower()

    # Remember name
    m = re.search(r"(je m'appelle|je suis|my name is)\s+([A-Za-zÀ-ÖØ-öø-ÿ'\-]+)", lower)
    if m:
        token = m.group(2)
        orig = re.search(rf"({token})", text, re.IGNORECASE)
        if orig:
            session["vars"]["name"] = orig.group(1)

    # Greetings
    if any(w in lower for w in ["salut", "bonjour", "bonsoir", "hey", "hello", "hi", "coucou"]):
        name = session["vars"].get("name")
        return f"Hi {name}! How can I help?" if name else "Hi! How can I help?"

    # Time / date
    if "time" in lower or "heure" in lower:
        return f"It’s {time.strftime('%H:%M')}."
    if any(k in lower for k in ["date", "what day", "quel jour"]):
        return f"Today is {time.strftime('%A %d %B %Y')}."

    # Identity
    if any(k in lower for k in ["who are you", "tu es qui", "qui es-tu"]):
        return "I’m a tiny Python-only chatbot, no external libraries. 😊"

    # Help
    if "help" in lower or "aide" in lower:
        return "Ask me the time, tell me your name, or any simple question. I remember a bit within this session."

    # Thanks / bye
    if any(k in lower for k in ["thanks", "merci", "thx"]):
        return "You’re welcome!"
    if any(k in lower for k in ["bye", "au revoir", "see you"]):
        return "See you soon!"

    # Question fallback
    if text.endswith("?"):
        name = session["vars"].get("name")
        prefix = f"{name}, " if name else ""
        return f"{prefix}good question! I’m very simple for now—try rephrasing or give more detail."

    # Generic fallback
    return "Got it. Could you clarify what you need?"

def generate_reply(session_id, user_message):
    sid, session = get_session(session_id)
    session["history"].append(("user", user_message))
    session["history"] = session["history"][-10:]
    reply = simple_ai_reply(session, user_message)
    session["history"].append(("assistant", reply))
    return sid, reply

# --- HTTP handler ------------------------------------------------------------
class ChatHandler(BaseHTTPRequestHandler):
    def _json_response(self, status=200, payload=None):
        body = json.dumps(payload or {}, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path != "/chat":
            return self._json_response(404, {"error": "Endpoint not found. Use POST /chat"})

        try:
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length) if length > 0 else b"{}"
            data = json.loads(raw.decode("utf-8"))
        except Exception:
            return self._json_response(400, {"error": "Invalid JSON body"})

        message = (data.get("message") or "").strip()
        session_id = data.get("session_id")
        if not message:
            return self._json_response(400, {"error": "Missing 'message' field"})

        sid, reply = generate_reply(session_id, message)
        return self._json_response(200, {"session_id": sid, "message": message, "reply": reply})

    # --- CORS preflight ---
    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Accept")
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS, GET")
        # (Optional) cache the preflight for a bit:
        self.send_header("Access-Control-Max-Age", "600")
        self.end_headers()

    # --- Optional: health check for quick browser test ---
    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/health":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"ok")
        else:
            self.send_response(404)
            self.end_headers()

    # Add CORS headers to *every* response (POST/GET/404/etc.)
    def end_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Vary", "Origin")
        super().end_headers()

def run():
    server = HTTPServer((HOST, PORT), ChatHandler)
    print(f"🚀 Chat API running at http://{HOST}:{PORT}  (POST /chat)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down…")
    finally:
        server.server_close()

if __name__ == "__main__":
    run()
