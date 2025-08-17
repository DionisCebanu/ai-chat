#!/usr/bin/env python3
"""
Auto-learning helper for the Flask chat bot.

Goal
----
When the bot doesn't know an answer:
1) Ask user for a topic: "general/travel/hobbies/car" (configurable)
2) Ask user for the answer text to store
3) Append {"patterns":[<original prompt>], "reply": <answer>} to kb/<topic>.json

Design
------
- No external libraries, pure stdlib.
- Atomic file writes (tmp file + os.replace).
- Dedup patterns (case-insensitive). If an entry with the SAME reply exists,
  we append the new pattern to its "patterns" list; otherwise we create a new entry.
- Conversation state is kept inside the session dict (sess["vars"]["learn"]).
- Safe words: "cancel" aborts the teach flow.
"""

from __future__ import annotations
from pathlib import Path
import os, json, time, threading, re
from typing import Dict, Tuple, List, Any

class AutoLearner:
    def __init__(self, kb_dir: Path, allowed_topics: List[str]):
        self.kb_dir = Path(kb_dir)
        self.allowed = [t.strip().lower() for t in allowed_topics]
        self._lock = threading.RLock()
        self.kb_dir.mkdir(parents=True, exist_ok=True)

    # ---------- Public entry point ----------
    def handle(self, sess: Dict[str, Any], user_text: str) -> Tuple[bool, str]:
        """
        Advance or start learning flow if appropriate.
        Returns (handled, reply_text).
        - handled=True => we consumed this turn with a learning step/prompt.
        - handled=False => app should use another reply (fallback etc.)
        """
        learn = (sess.get("vars") or {}).get("learn") or {}
        state = learn.get("state", "idle")

        # Global cancel
        if user_text.strip().lower() == "cancel":
            self._reset(sess)
            return True, "Learning canceled. Got it. We’re back to regular chat."

        if state == "await_topic":
            return True, self._handle_topic(sess, user_text)

        if state == "await_reply":
            return True, self._handle_reply(sess, user_text)

        # Not in learning flow yet -> start with a topic ask,
        # but ONLY if caller decided this was "unknown".
        # The caller should only call handle() when there's no known answer
        # OR always call handle() and let us decide:
        # We choose to ALWAYS start here if state=idle, because app calls us when unknown.
        return True, self._start(sess, user_text)

    # ---------- Flow steps ----------
    def _start(self, sess: Dict[str, Any], original_prompt: str) -> str:
        # keep original casing for display; also a lowercase version for matching later
        self._set(sess, {
            "state": "await_topic",
            "prompt": original_prompt.strip(),
            "ts": time.time(),
        })
        options = ", ".join(self.allowed)
        return ( "Sorry, I’m not trained yet to respond to that.\n"
                 f"What topic is this? {options}\n"
                 f"Type just the topic (or type 'cancel')." )

    def _handle_topic(self, sess: Dict[str, Any], topic_input: str) -> str:
        topic = (topic_input or "").strip().lower()
        if topic not in self.allowed:
            options = ", ".join(self.allowed)
            return f"Please type one of: {options} (or 'cancel')."
        learn = self._get(sess)
        learn["state"] = "await_reply"
        learn["topic"] = topic
        self._set(sess, learn)
        return ( f"Thanks for the topic: '{topic}'.\n"
                 "Now type the answer I should reply with next time someone asks this.\n"
                 "You can still 'cancel' to abort." )

    def _handle_reply(self, sess: Dict[str, Any], reply_text: str) -> str:
        reply = (reply_text or "").strip()
        if not reply:
            return "Please type the answer text (or 'cancel')."

        learn = self._get(sess)
        topic = learn.get("topic")
        prompt = learn.get("prompt", "").strip()
        if not topic or not prompt:
            # corrupted state -> reset
            self._reset(sess)
            return "Something went wrong with learning state. Let’s start over. Ask me again."

        # Persist to kb/<topic>.json
        saved = self._add_entry(topic, prompt, reply)
        self._reset(sess)
        if saved:
            return ( "Saved! From now on, when someone asks something like:\n"
                     f'  “{prompt}”\n'
                     f"I’ll reply with:\n  “{reply}”\n"
                     f"(topic: {topic})" )
        else:
            return ( "I already knew a matching entry for that pattern/reply, so nothing changed.\n"
                     f"(topic: {topic})" )

    # ---------- Session helpers ----------
    def _get(self, sess: Dict[str, Any]) -> Dict[str, Any]:
        sess.setdefault("vars", {})
        sess["vars"].setdefault("learn", {})
        return sess["vars"]["learn"]

    def _set(self, sess: Dict[str, Any], learn_dict: Dict[str, Any]) -> None:
        sess.setdefault("vars", {})
        sess["vars"]["learn"] = learn_dict

    def _reset(self, sess: Dict[str, Any]) -> None:
        sess.setdefault("vars", {})
        sess["vars"]["learn"] = {"state": "idle"}

    # ---------- File IO ----------
    def _topic_path(self, topic: str) -> Path:
        return self.kb_dir / f"{topic}.json"

    def _load_entries(self, topic: str) -> List[Dict[str, Any]]:
        path = self._topic_path(topic)
        if not path.exists():
            return []
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return []
        # normalize structure
        entries = []
        if isinstance(data, dict):
            # legacy dict format: {pattern: reply}
            for pat, rep in data.items():
                entries.append({"patterns": [str(pat)], "reply": str(rep)})
        elif isinstance(data, list):
            for item in data:
                if isinstance(item, dict) and "reply" in item:
                    pats = item.get("patterns") or []
                    if isinstance(pats, list):
                        pats = [p for p in pats if isinstance(p, str) and p.strip()]
                    else:
                        pats = []
                    entries.append({"patterns": pats, "reply": str(item["reply"])})
        return entries

    def _save_entries(self, topic: str, entries: List[Dict[str, Any]]) -> None:
        path = self._topic_path(topic)
        tmp = path.with_suffix(".json.tmp")
        # pretty and UTF-8 human-friendly
        payload = json.dumps(entries, ensure_ascii=False, indent=2)
        with self._lock:
            tmp.write_text(payload, encoding="utf-8")
            os.replace(tmp, path)  # atomic on same filesystem

    def _add_entry(self, topic: str, pattern: str, reply: str) -> bool:
        """
        Return True if file changed, False if it was a no-op.
        - If an entry already has this pattern (case-insensitive), and same reply, do nothing.
        - If an entry has the same reply, append the new pattern (if new).
        - Else append a new entry {patterns:[pattern], reply}.
        """
        pattern_norm = pattern.strip()
        reply_norm = reply.strip()
        if not pattern_norm:
            return False

        with self._lock:
            entries = self._load_entries(topic)

            # Look for existing exact (case-insensitive) pattern
            for e in entries:
                pats_lower = {p.lower() for p in e.get("patterns", [])}
                if pattern_norm.lower() in pats_lower:
                    # If pattern exists and reply matches -> no change
                    if e.get("reply", "") == reply_norm:
                        return False
                    # Pattern exists with DIFFERENT reply: prefer adding a NEW entry
                    # to avoid silently changing meaning.
                    break

            # Try to merge into an entry that has the same reply
            for e in entries:
                if e.get("reply", "") == reply_norm:
                    if pattern_norm.lower() not in {p.lower() for p in e.get("patterns", [])}:
                        e.setdefault("patterns", []).append(pattern_norm)
                        self._save_entries(topic, entries)
                        return True
                    else:
                        return False  # already present

            # Otherwise create a fresh entry
            entries.append({"patterns": [pattern_norm], "reply": reply_norm})
            self._save_entries(topic, entries)
            return True
