#!/usr/bin/env python3
"""
Auto-learning helper for the Flask chat bot (topic-creating version, refactored).

Purpose
-------
When the bot cannot answer a user's prompt, this module runs a small
state machine to *teach* the bot:

  1) Ask for a topic (existing or NEW).
  2) If NEW: ask for routing keywords (or 'auto' to extract from the prompt),
     then create kb/<topic>.json and update kb/router.json.
  3) Ask for the answer text to save.
  4) Append {"patterns":[<original prompt>], "reply": <answer>} to that topic file.

Key qualities
-------------
- Pure standard library; no external dependencies.
- Atomic file writes (tmp + os.replace) → safe on Windows/macOS/Linux.
- English-only tokenization (for 'auto' keyword extraction).
- Deduplicates entries smartly:
    * If same reply exists, we append the new pattern to its "patterns".
    * If identical (pattern, reply) already exists → no change.

User commands
-------------
- 'cancel' at any step aborts the learning flow and resets state.

Public API (used by app.py)
---------------------------
- AutoLearner(kb_dir: Path, router_file: Path)
- handle(session: dict, user_message: str) -> (handled: bool, reply_text: str)
- reload_router() -> bool  (no-op placeholder, returns True)
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, Tuple, List, Any
import json
import os
import re
import string
import threading
import time

# --------------------------- Tokenization utils ---------------------------

# English-only stopwords for lightweight keyword extraction during 'auto'.
STOPWORDS: set[str] = {
    "the", "a", "an", "is", "it", "to", "of", "and", "in", "on", "for", "with",
    "do", "does", "what", "which", "who", "please", "me", "my", "your", "you",
    "are", "i", "im", "i'm", "am", "be", "can", "could", "would", "should",
    "this", "that", "there", "here", "about", "from", "at", "as", "or", "if",
    "how", "when", "where", "why"
}
_PUNCT_XLATE = str.maketrans("", "", string.punctuation)


def extract_keywords(text: str, max_keywords: int = 8) -> List[str]:
    """
    Very small tokenizer used for seeding router keywords when the user types 'auto'.
    Lowercases, strips punctuation, drops stopwords, keeps order and uniqueness.
    """
    if not text:
        return []
    lowered = text.lower().translate(_PUNCT_XLATE)
    unique_seen: set[str] = set()
    keywords: List[str] = []
    for token in lowered.split():
        if not token or token in STOPWORDS or token in unique_seen:
            continue
        unique_seen.add(token)
        keywords.append(token)
        if len(keywords) >= max_keywords:
            break
    return keywords


# ------------------------------ AutoLearner -------------------------------

class AutoLearner:
    """
    Tiny state machine to capture a topic (existing or new), optional keywords,
    and a reply, then persist it to kb/<topic>.json and kb/router.json.
    """

    # Learning states
    _STATE_IDLE = "idle"
    _STATE_AWAIT_TOPIC = "await_topic"
    _STATE_AWAIT_NEW_TOPIC_KEYWORDS = "await_new_topic_keywords"
    _STATE_AWAIT_REPLY = "await_reply"

    # Default router bootstrap (if router.json doesn't exist yet)
    _DEFAULT_ROUTER = {"general": ["hello", "hi", "thanks", "help"]}

    def __init__(self, kb_dir: Path, router_file: Path) -> None:
        self.kb_dir: Path = Path(kb_dir)
        self.router_file: Path = Path(router_file)
        self._fs_lock = threading.RLock()  # protects router & topic writes

        # Ensure kb directory exists; bootstrap router if missing.
        self.kb_dir.mkdir(parents=True, exist_ok=True)
        if not self.router_file.exists():
            self._write_router(self._DEFAULT_ROUTER)

    # --------------------------- Public methods ---------------------------

    def reload_router(self) -> bool:
        """No-op shim; kept for compatibility with app.py admin/reload."""
        return True

    def topics(self) -> List[str]:
        """Return the list of topic names present in router.json (sorted)."""
        router = self._read_router()
        return sorted(router.keys())

    def handle(self, session_obj: Dict[str, Any], user_message: str) -> Tuple[bool, str]:
        """
        Advance or start the learning flow for a given session.

        Returns:
          handled (bool): True if this module produced a response for this turn.
          reply_text (str): The text to send back to the user.
        """
        learning_state_data = self._get_learning_state(session_obj)
        state = learning_state_data.get("state", self._STATE_IDLE)

        # Global cancel
        if (user_message or "").strip().lower() == "cancel":
            self._reset_learning_state(session_obj)
            return True, "Learning canceled. Back to chat."

        if state == self._STATE_AWAIT_TOPIC:
            return True, self._step_choose_topic(session_obj, user_message)

        if state == self._STATE_AWAIT_NEW_TOPIC_KEYWORDS:
            return True, self._step_collect_new_topic_keywords(session_obj, user_message)

        if state == self._STATE_AWAIT_REPLY:
            return True, self._step_collect_answer(session_obj, user_message)

        # Not in a flow yet → start.
        return True, self._start_flow(session_obj, user_message)

    # --------------------------- State transitions ------------------------

    def _start_flow(self, session_obj: Dict[str, Any], original_prompt: str) -> str:
        """
        Entry point when no answer was found.
        We store the original prompt and ask for a topic.
        """
        self._set_learning_state(session_obj, {
            "state": self._STATE_AWAIT_TOPIC,
            "prompt": (original_prompt or "").strip(),
            "ts": time.time(),
        })
        available = ", ".join(self.topics())
        return (
            "Sorry, I’m not trained yet to respond to that.\n"
            f"What topic is this? {available} …or type a *new* topic name.\n"
            "Type just the topic (or type 'cancel')."
        )

    def _step_choose_topic(self, session_obj: Dict[str, Any], topic_input: str) -> str:
        """
        Accept an existing topic (advance directly to answer)
        or a new topic (ask for routing keywords next).
        """
        proposed_topic = (topic_input or "").strip()
        if not proposed_topic:
            return "Please type a topic (existing or new), or 'cancel'."

        existing_topics = set(self.topics())
        if proposed_topic in existing_topics:
            learning = self._get_learning_state(session_obj)
            learning["state"] = self._STATE_AWAIT_REPLY
            learning["topic"] = proposed_topic
            self._set_learning_state(session_obj, learning)
            return (
                f"Great — topic set to '{proposed_topic}'.\n"
                "Now type the answer I should reply with next time someone asks this.\n"
                "You can still 'cancel' to abort."
            )

        # New topic path → gather routing keywords.
        learning = self._get_learning_state(session_obj)
        learning["state"] = self._STATE_AWAIT_NEW_TOPIC_KEYWORDS
        learning["new_topic"] = proposed_topic
        self._set_learning_state(session_obj, learning)
        return (
            f"New topic '{proposed_topic}' — nice!\n"
            "Type a few *keywords* for routing (comma-separated), e.g. 'mercedes, bmw, car'.\n"
            "Or type 'auto' to let me extract keywords from your question."
        )

    def _step_collect_new_topic_keywords(self, session_obj: Dict[str, Any], keyword_text: str) -> str:
        """
        Create a new topic:
          - collect keywords (manual list or 'auto' to extract)
          - update router.json
          - ensure kb/<topic>.json exists
          - advance to answer collection
        """
        learning = self._get_learning_state(session_obj)
        new_topic_name = learning.get("new_topic")
        if not new_topic_name:
            # Defensive: unexpected loss of state.
            self._reset_learning_state(session_obj)
            return "Something went wrong. Let’s start over — ask me again."

        raw = (keyword_text or "").strip()
        if raw.lower() == "auto" or not raw:
            # Extract from original prompt.
            prompt_text = learning.get("prompt", "")
            keywords = extract_keywords(prompt_text) or ["misc"]
        else:
            # Manual comma-separated list.
            keywords = [kw.strip().lower() for kw in raw.split(",") if kw.strip()]

        # Persist router + topic file.
        self._add_topic_to_router(new_topic_name, keywords)
        self._ensure_topic_file_exists(new_topic_name)

        # Advance to collecting the answer.
        learning["state"] = self._STATE_AWAIT_REPLY
        learning["topic"] = new_topic_name
        learning.pop("new_topic", None)
        self._set_learning_state(session_obj, learning)
        return (
            f"Added topic '{new_topic_name}' with keywords: {', '.join(keywords)}.\n"
            "Now type the answer I should reply with next time someone asks this.\n"
            "('cancel' to abort.)"
        )

    def _step_collect_answer(self, session_obj: Dict[str, Any], answer_text: str) -> str:
        """
        Final step: persist the (pattern, reply) pair into kb/<topic>.json.
        - 'pattern' is the original unknown user prompt stored at _start_flow.
        - 'reply' is what the user provides now.
        """
        reply_text = (answer_text or "").strip()
        if not reply_text:
            return "Please type the answer text (or 'cancel')."

        learning = self._get_learning_state(session_obj)
        topic_name = learning.get("topic")
        original_prompt = (learning.get("prompt") or "").strip()
        if not topic_name or not original_prompt:
            self._reset_learning_state(session_obj)
            return "Learning state got lost — let’s start over."

        file_changed = self._append_or_merge_entry(topic_name, original_prompt, reply_text)

        # Always reset the state after attempting to save.
        self._reset_learning_state(session_obj)

        if file_changed:
            return (
                "Saved! From now on, when someone asks something like:\n"
                f'  “{original_prompt}”\n'
                "I’ll reply with:\n"
                f'  “{reply_text}”\n'
                f"(topic: {topic_name})"
            )
        else:
            return (
                "I already knew a matching entry for that pattern/reply, so nothing changed.\n"
                f"(topic: {topic_name})"
            )

    # ------------------------- Session state helpers -----------------------

    def _get_learning_state(self, session_obj: Dict[str, Any]) -> Dict[str, Any]:
        """Return a mutable dict for the learning sub-state within the session."""
        session_obj.setdefault("vars", {})
        session_obj["vars"].setdefault("learn", {"state": self._STATE_IDLE})
        return session_obj["vars"]["learn"]

    def _set_learning_state(self, session_obj: Dict[str, Any], new_state: Dict[str, Any]) -> None:
        session_obj.setdefault("vars", {})
        session_obj["vars"]["learn"] = new_state

    def _reset_learning_state(self, session_obj: Dict[str, Any]) -> None:
        session_obj.setdefault("vars", {})
        session_obj["vars"]["learn"] = {"state": self._STATE_IDLE}

    # ------------------------------ Router I/O -----------------------------

    def _read_router(self) -> Dict[str, List[str]]:
        """Load router.json; return {} on error."""
        try:
            return json.loads(self.router_file.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _write_router(self, router_data: Dict[str, List[str]]) -> None:
        """
        Atomically write router.json (pretty, UTF-8).
        Uses a temp file + os.replace to avoid partial writes.
        """
        temp_path = self.router_file.with_suffix(".json.tmp")
        payload = json.dumps(router_data, ensure_ascii=False, indent=2)
        with self._fs_lock:
            temp_path.write_text(payload, encoding="utf-8")
            os.replace(temp_path, self.router_file)

    def _add_topic_to_router(self, topic_name: str, keywords: List[str]) -> None:
        """
        Add a new topic or merge keywords with an existing topic in router.json.
        Ensures keywords are lowercase and deduplicated.
        """
        with self._fs_lock:
            router = self._read_router()
            if topic_name in router:
                existing = {kw.lower() for kw in router.get(topic_name, [])}
                for kw in keywords:
                    existing.add(kw.lower())
                router[topic_name] = sorted(existing)
            else:
                router[topic_name] = sorted({kw.lower() for kw in keywords})
            self._write_router(router)

    # ---------------------------- Topic file I/O ---------------------------

    def _topic_file_path(self, topic_name: str) -> Path:
        return self.kb_dir / f"{topic_name}.json"

    def _ensure_topic_file_exists(self, topic_name: str) -> None:
        """Create an empty list-based topic file if missing."""
        path = self._topic_file_path(topic_name)
        if not path.exists():
            with self._fs_lock:
                path.write_text("[]\n", encoding="utf-8")

    def _load_topic_entries(self, topic_name: str) -> List[Dict[str, Any]]:
        """
        Load the topic JSON and normalize to a list of {patterns: [str], reply: str}.
        Supports legacy dict format {pattern: reply}.
        """
        path = self._topic_file_path(topic_name)
        if not path.exists():
            return []
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return []

        normalized: List[Dict[str, Any]] = []
        if isinstance(raw, dict):
            # Legacy: { "pattern": "reply", ... }
            for pattern, reply in raw.items():
                normalized.append({"patterns": [str(pattern)], "reply": str(reply)})
        elif isinstance(raw, list):
            for item in raw:
                if not isinstance(item, dict) or "reply" not in item:
                    continue
                patterns_raw = item.get("patterns") or []
                if isinstance(patterns_raw, list):
                    patterns = [p for p in patterns_raw if isinstance(p, str) and p.strip()]
                else:
                    patterns = []
                normalized.append({"patterns": patterns, "reply": str(item["reply"])})
        return normalized

    def _save_topic_entries(self, topic_name: str, entries: List[Dict[str, Any]]) -> None:
        """
        Atomically persist the given entries list to kb/<topic>.json (pretty, UTF-8).
        """
        path = self._topic_file_path(topic_name)
        temp_path = path.with_suffix(".json.tmp")
        payload = json.dumps(entries, ensure_ascii=False, indent=2)
        with self._fs_lock:
            temp_path.write_text(payload, encoding="utf-8")
            os.replace(temp_path, path)

    def _append_or_merge_entry(self, topic_name: str, pattern_text: str, reply_text: str) -> bool:
        """
        Ensure (pattern_text, reply_text) is represented in the topic file.
        Return True if the file changed, False if it was a no-op.

        Merge strategy:
          - If an entry already has this exact reply, append the pattern if it's new (case-insensitive).
          - Else append a new entry with this pattern and reply.
          - If an entry already contains this *pattern* with the same *reply*, do nothing.
        """
        pattern_norm = (pattern_text or "").strip()
        reply_norm = (reply_text or "").strip()
        if not pattern_norm:
            return False

        with self._fs_lock:
            entries = self._load_topic_entries(topic_name)

            # If exact (pattern, reply) already exists → no change.
            for entry in entries:
                existing_patterns_lower = {p.lower() for p in entry.get("patterns", [])}
                if pattern_norm.lower() in existing_patterns_lower and entry.get("reply", "") == reply_norm:
                    return False

            # If there's an entry with the same reply → merge by adding the new pattern.
            for entry in entries:
                if entry.get("reply", "") == reply_norm:
                    lower_set = {p.lower() for p in entry.get("patterns", [])}
                    if pattern_norm.lower() not in lower_set:
                        entry.setdefault("patterns", []).append(pattern_norm)
                        self._save_topic_entries(topic_name, entries)
                        return True
                    return False  # pattern already present under same reply

            # Otherwise, create a fresh entry.
            entries.append({"patterns": [pattern_norm], "reply": reply_norm})
            self._save_topic_entries(topic_name, entries)
            return True
