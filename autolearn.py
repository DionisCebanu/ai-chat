#!/usr/bin/env python3
"""
Auto-learning helper for the Flask chat bot (topic-creating version).

Flow (state machine)
--------------------
Unknown prompt ->
  ask topic (existing or new) ->
    IF new: ask for keywords (comma-separated) OR 'auto' to extract ->
      create kb/<topic>.json and update kb/router.json ->
    ask for the answer ->
      append to kb/<topic>.json (merge by reply; dedupe patterns)

Commands:
- 'cancel' at any time cancels the learning flow.

Design:
- Pure stdlib; atomic writes using os.replace
- English-only tokenization for keyword 'auto' seeding
"""

from __future__ import annotations
from pathlib import Path
import os, json, time, threading, re, string
from typing import Dict, Tuple, List, Any

# --------- simple English tokenizer (for 'auto' keywords) ----------
STOPWORDS = {
    "the","a","an","is","it","to","of","and","in","on","for","with",
    "do","does","what","which","who","please","me","my","your","you",
    "are","i","im","i'm","am","be","can","could","would","should",
    "this","that","there","here","about","from","at","as","or","if",
    "how","when","where","why"
}
PUNCT = str.maketrans("", "", string.punctuation)

def tokenize(s: str) -> List[str]:
    s = (s or "").lower().translate(PUNCT)
    toks = [t for t in s.split() if t and t not in STOPWORDS]
    return toks

# -------------------------------------------------------------------

class AutoLearner:
    def __init__(self, kb_dir: Path, router_file: Path):
        self.kb_dir = Path(kb_dir)
        self.router_file = Path(router_file)
        self._lock = threading.RLock()
        self.kb_dir.mkdir(parents=True, exist_ok=True)
        if not self.router_file.exists():
            # bootstrap a minimal router
            self._write_router({"general": ["hello","hi","thanks","help"]})

    # ---------- Public API ----------
    def reload_router(self):
        """No-op shim; router is read lazily from disk when needed."""
        return True

    def topics(self) -> List[str]:
        r = self._read_router()
        return sorted(r.keys())

    def handle(self, sess: Dict[str, Any], user_text: str) -> Tuple[bool, str]:
        """
        Advance or start the learning flow.
        Returns (handled, reply_text).
        """
        learn = self._get(sess)
        state = learn.get("state", "idle")

        # Global cancel
        if (user_text or "").strip().lower() == "cancel":
            self._reset(sess)
            return True, "Learning canceled. Back to chat."

        if state == "await_topic":
            return True, self._step_topic(sess, user_text)

        if state == "await_new_topic_keywords":
            return True, self._step_new_topic_keywords(sess, user_text)

        if state == "await_reply":
            return True, self._step_reply(sess, user_text)

        # state = idle -> begin learn
        return True, self._start(sess, user_text)

    # ---------- Steps ----------
    def _start(self, sess: Dict[str, Any], original_prompt: str) -> str:
        self._set(sess, {
            "state": "await_topic",
            "prompt": (original_prompt or "").strip(),
            "ts": time.time(),
        })
        options = ", ".join(self.topics())
        return ( "Sorry, I’m not trained yet to respond to that.\n"
                 f"What topic is this? {options} …or type a *new* topic name.\n"
                 "Type just the topic (or type 'cancel')." )

    def _step_topic(self, sess: Dict[str, Any], topic_input: str) -> str:
        topic = (topic_input or "").strip()
        if not topic:
            return "Please type a topic (existing or new), or 'cancel'."

        existing = set(self.topics())
        if topic in existing:
            learn = self._get(sess)
            learn["state"] = "await_reply"
            learn["topic"] = topic
            self._set(sess, learn)
            return ( f"Great — topic set to '{topic}'.\n"
                     "Now type the answer I should reply with next time someone asks this.\n"
                     "You can still 'cancel' to abort." )

        # new topic -> ask for keywords
        learn = self._get(sess)
        learn["state"] = "await_new_topic_keywords"
        learn["new_topic"] = topic
        self._set(sess, learn)
        return ( f"New topic '{topic}' — nice!\n"
                 "Type a few *keywords* for routing (comma-separated), e.g. 'mercedes, bmw, car'.\n"
                 "Or type 'auto' to let me extract keywords from your question." )

    def _step_new_topic_keywords(self, sess: Dict[str, Any], kw_text: str) -> str:
        learn = self._get(sess)
        topic = learn.get("new_topic")
        if not topic:
            self._reset(sess)
            return "Something went wrong. Let’s start over — ask me again."

        kws = []
        txt = (kw_text or "").strip()
        if txt.lower() == "auto" or not txt:
            # seed from original prompt
            prompt = learn.get("prompt","")
            toks = tokenize(prompt)
            # keep up to 8 unique keywords
            seen = set()
            for t in toks:
                if t not in seen:
                    kws.append(t)
                    seen.add(t)
                if len(kws) >= 8:
                    break
            if not kws:
                kws = ["misc"]  # worst-case seed
        else:
            # split commas -> keywords
            kws = [k.strip().lower() for k in txt.split(",") if k.strip()]

        # write router + create empty topic file if missing
        self._add_topic_to_router(topic, kws)
        self._ensure_topic_file(topic)

        # advance to reply
        learn["state"] = "await_reply"
        learn["topic"] = topic
        learn.pop("new_topic", None)
        self._set(sess, learn)
        return ( f"Added topic '{topic}' with keywords: {', '.join(kws)}.\n"
                 "Now type the answer I should reply with next time someone asks this.\n"
                 "('cancel' to abort.)" )

    def _step_reply(self, sess: Dict[str, Any], reply_text: str) -> str:
        reply = (reply_text or "").strip()
        if not reply:
            return "Please type the answer text (or 'cancel')."

        learn = self._get(sess)
        topic = learn.get("topic")
        prompt = (learn.get("prompt") or "").strip()
        if not topic or not prompt:
            self._reset(sess)
            return "Learning state got lost — let’s start over."

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
        sess["vars"].setdefault("learn", {"state":"idle"})
        return sess["vars"]["learn"]

    def _set(self, sess: Dict[str, Any], learn_dict: Dict[str, Any]) -> None:
        sess.setdefault("vars", {})
        sess["vars"]["learn"] = learn_dict

    def _reset(self, sess: Dict[str, Any]) -> None:
        sess.setdefault("vars", {})
        sess["vars"]["learn"] = {"state":"idle"}

    # ---------- Router I/O ----------
    def _read_router(self) -> Dict[str, List[str]]:
        try:
            return json.loads(self.router_file.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _write_router(self, data: Dict[str, List[str]]) -> None:
        tmp = self.router_file.with_suffix(".json.tmp")
        payload = json.dumps(data, ensure_ascii=False, indent=2)
        with self._lock:
            tmp.write_text(payload, encoding="utf-8")
            os.replace(tmp, self.router_file)

    def _add_topic_to_router(self, topic: str, keywords: List[str]) -> None:
        with self._lock:
            r = self._read_router()
            if topic in r:
                # merge keywords
                old = set(k.lower() for k in r.get(topic, []))
                for k in keywords:
                    if k.lower() not in old:
                        old.add(k.lower())
                r[topic] = sorted(old)
            else:
                r[topic] = sorted({k.lower() for k in keywords})
            self._write_router(r)

    # ---------- Topic file I/O ----------
    def _topic_path(self, topic: str) -> Path:
        return self.kb_dir / f"{topic}.json"

    def _ensure_topic_file(self, topic: str) -> None:
        p = self._topic_path(topic)
        if not p.exists():
            with self._lock:
                payload = json.dumps([], ensure_ascii=False, indent=2)
                p.write_text(payload, encoding="utf-8")

    def _load_entries(self, topic: str) -> List[Dict[str, Any]]:
        p = self._topic_path(topic)
        if not p.exists():
            return []
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return []
        entries = []
        if isinstance(data, dict):
            for pat, rep in data.items():
                entries.append({"patterns":[str(pat)], "reply":str(rep)})
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
        p = self._topic_path(topic)
        tmp = p.with_suffix(".json.tmp")
        payload = json.dumps(entries, ensure_ascii=False, indent=2)
        with self._lock:
            tmp.write_text(payload, encoding="utf-8")
            os.replace(tmp, p)

    def _add_entry(self, topic: str, pattern: str, reply: str) -> bool:
        """
        Return True if file changed.
        - If entry with same reply exists -> append pattern if new.
        - Else -> append new entry.
        """
        pattern_norm = pattern.strip()
        reply_norm = reply.strip()
        if not pattern_norm:
            return False

        with self._lock:
            entries = self._load_entries(topic)

            # If an entry already has this (pattern, reply) -> no change
            for e in entries:
                pats_lower = {p.lower() for p in e.get("patterns", [])}
                if pattern_norm.lower() in pats_lower and e.get("reply","") == reply_norm:
                    return False

            # Prefer merging by reply (synonyms)
            for e in entries:
                if e.get("reply","") == reply_norm:
                    if pattern_norm.lower() not in {p.lower() for p in e.get("patterns", [])}:
                        e.setdefault("patterns", []).append(pattern_norm)
                        self._save_entries(topic, entries)
                        return True
                    else:
                        return False

            # Otherwise add a fresh entry
            entries.append({"patterns":[pattern_norm], "reply":reply_norm})
            self._save_entries(topic, entries)
            return True
