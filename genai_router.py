#!/usr/bin/env python3
# genai_router.py — decides when to call Gemini and returns its reply.

from __future__ import annotations
import json, os, re

# ---- Load config
_DEF = {
  "explicit_prefixes": ["ai:", "gemini:", "/ai", "/g"],
  "keywords_any": ["write","message","characters","words","draft","compose","email","text","note","poem","story","caption"],
  "min_keywords": 1,
  "stopwords": ["the","a","an","and","or","to","of","for","is","are","be","in","on","at","by","with","from"]
}

def _load_conf():
    try:
        with open("genai_router.json","r",encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, dict):
                return {**_DEF, **data}
    except Exception:
        pass
    return _DEF

CONF = _load_conf()

# ---- Import aiapi with flexible lookup
try:
    import aiapi
except Exception:
    aiapi = None

def _ai_call(session_id: str, history: list[tuple[str,str]], user_message: str) -> str:
    if aiapi is None:
        return "Gemini is not available (aiapi.py not found)."

    # Prefer chat-style adapters you just added
    for name in ("chat_reply", "generate_reply", "gemini_reply"):
        fn = getattr(aiapi, name, None)
        if callable(fn):
            try:
                return fn(session_id=session_id, history=history, user_message=user_message)
            except TypeError:
                try:  # if signature is positional
                    return fn(session_id, history, user_message)
                except Exception:
                    pass
            except Exception:
                pass

    # Single-turn fallbacks (includes your text_reply wrapper)
    for name in ("text_reply", "ask", "get_gemini_general_response"):
        fn = getattr(aiapi, name, None)
        if callable(fn):
            try:
                # If it’s your original function (prompt only), just pass message
                return fn(user_message)
            except Exception:
                pass

    return "Gemini integration error: no compatible function found in aiapi.py."

# ---- Trigger logic
_WORD_RE = re.compile(r"[a-z0-9]+", re.I)
def _tok(s: str): return _WORD_RE.findall((s or "").lower())

def _by_prefix(msg: str):
    m = (msg or "").strip()
    for p in CONF["explicit_prefixes"]:
        if m.lower().startswith(p.lower()):
            return True, m[len(p):].strip()
    return False, msg

def _by_keywords(msg: str):
    words = set(_tok(msg))
    stops = set(CONF.get("stopwords") or [])
    hits = 0
    for kw in (CONF.get("keywords_any") or []):
        kw = kw.lower()
        if " " in kw:
            if kw in msg.lower():
                hits += 1
        else:
            if kw in words and kw not in stops:
                hits += 1
        if hits >= int(CONF.get("min_keywords", 2)):
            return True
    return False

# ---- Entry point used by app.py
def handle(sess: dict, message: str, session_id: str) -> tuple[bool, str]:
    if not message:
        return False, ""
    pref, stripped = _by_prefix(message)
    if pref:
        return True, _ai_call(session_id, sess.get("history") or [], stripped)
    if _by_keywords(message):
        return True, _ai_call(session_id, sess.get("history") or [], message)
    return False, ""
