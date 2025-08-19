# translate.py — parse "translate ..." requests and return a clean translation via aiapi.py

from __future__ import annotations
import re

# --- language normalization --------------------------------------------------

_LANG_ALIASES = {
    "english":   {"en","eng","english"},
    "french":    {"fr","fra","fre","francais","français","french"},
    "spanish":   {"es","spa","spanish","español","espanol"},
    "german":    {"de","ger","deu","german","deutsch"},
    "italian":   {"it","ita","italian","italiano"},
    "portuguese":{"pt","por","portuguese","português","portugues"},
    "romanian":  {"ro","ron","romana","română","romanian"},
    "russian":   {"ru","rus","russian","русский"},
    "arabic":    {"ar","ara","arabic","العربية"},
    "japanese":  {"ja","jpn","japanese","日本語"},
    "korean":    {"ko","kor","korean","한국어"},
    "chinese":   {"zh","zho","chi","chinese","中文","mandarin","simplified","traditional"},
    "hindi":     {"hi","hin","hindi"},
    "turkish":   {"tr","tur","turkish","türkçe","turkce"},
    "polish":    {"pl","pol","polish","polski"},
    "dutch":     {"nl","nld","dut","dutch","nederlands"},
}

def _norm_lang(s: str) -> str | None:
    if not s: return None
    key = s.strip().lower()
    key = re.sub(r"[^a-záàâäãåçéèêëíìîïñóòôöõúùûüășşțţąćęłńóśźżăîâ\- ]+", "", key)
    key = key.replace(" language","").strip()
    for canon, variants in _LANG_ALIASES.items():
        if key in variants or canon in key:
            return canon
    return None

# --- patterns ---------------------------------------------------------------

_PATTERNS = [
    # translate to French: text
    re.compile(r"^\s*translate\s+(?:to|in)\s+(?P<lang>[A-Za-z\- ]+)\s*:\s*(?P<text>.+)$", re.I),
    # translate "text" to French
    re.compile(r"^\s*translate\s+[\"“”']?(?P<text>.+?)[\"“”']?\s+to\s+(?P<lang>[A-Za-z\- ]+)\s*$", re.I),
    # translate text to fr
    re.compile(r"^\s*translate\s+(?P<text>.+?)\s+to\s+(?P<lang>[A-Za-z]{2,})\s*$", re.I),
    # translate en->fr: text
    re.compile(r"^\s*translate\s+(?P<src>[A-Za-z]{2,})\s*->\s*(?P<lang>[A-Za-z]{2,})\s*:\s*(?P<text>.+)$", re.I),
    # translate: text  (ask for target later)
    re.compile(r"^\s*translate\s*:\s*(?P<text>.+)$", re.I),
]

def parse_translate_command(message: str) -> tuple[str | None, str | None]:
    """
    Returns (text, target_lang) where target_lang is canonical (e.g., 'french').
    If target_lang is None but it's clearly a translate request, we still return (text, None).
    """
    msg = (message or "").strip()
    for pat in _PATTERNS:
        m = pat.match(msg)
        if not m: 
            continue
        text = (m.groupdict().get("text") or "").strip().strip('"“”\'')
        lang_raw = (m.groupdict().get("lang") or "").strip()
        lang = _norm_lang(lang_raw) if lang_raw else None
        return text or None, lang
    return None, None

# --- Gemini bridge ----------------------------------------------------------

def _call_gemini(prompt: str) -> str:
    try:
        import aiapi
    except Exception:
        return "Translation service is not available (aiapi.py not found)."
    # Prefer a single-turn call (your aiapi exposes get_gemini_general_response & text_reply)
    fn = getattr(aiapi, "text_reply", None) or getattr(aiapi, "get_gemini_general_response", None)
    if not callable(fn):
        return "Translation service is not available (Gemini adapter missing)."
    try:
        return (fn(prompt) or "").strip() or "No translation produced."
    except Exception as e:
        return f"Translation error: {e}"

def handle(message: str) -> tuple[bool, str]:
    text, lang = parse_translate_command(message)
    if text is None:
        return False, ""
    if not lang:
        return True, "Which language should I translate to? (e.g., English, French, Spanish)"

    # build a strict translation prompt
    lang_title = lang.capitalize()
    prompt = (
        f"Translate the text between triple backticks into {lang_title}.\n"
        "Important rules:\n"
        "1) Output ONLY the translation (no quotes, no preface, no notes).\n"
        "2) Preserve numbers, punctuation, emoji, and line breaks.\n"
        "3) Keep proper nouns and formatting.\n"
        "```\n"
        f"{text}\n"
        "```"
    )
    translated = _call_gemini(prompt)
    return True, translated
