"""
memory.py — Persistent memory manager for Aisha
Stores user preferences, name, and conversation context in memory.json.
Includes memory rotation to prevent unbounded growth.
"""

from __future__ import annotations

import json
import os
import re
import threading
from copy import deepcopy
from datetime import datetime
from typing import Any

MEMORY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "memory.json")
_LOCK = threading.RLock()

DEFAULT_MEMORY = {
    "user_name": None,
    "preferences": {},
    "facts": [],
    "conversation_history": [],
    "conversation_summaries": [],  # Compressed digests of old conversations
    "daily_episodes": [],          # Day-by-day high-level interaction recaps (up to 14 days)
    "last_interaction": None,
}

# Rotation limits
MAX_HISTORY = 20        # Keep last 20 conversation turns in full detail
MAX_FACTS = 30          # Keep at most 30 unique facts
MAX_SUMMARIES = 10      # Keep at most 10 compressed summaries of older conversations
MAX_DAILY_EPISODES = 14 # Keep up to 14 days of episodic memory

INVALID_NAMES = {
    "kya", "sun", "suno", "mujhe", "batao", "bolo", "aisha", "aaj", "ab", "abhi",
    "kuch", "hai", "haan", "nahi", "yaar", "bhai", "hello", "theek", "shanti", "ek",
    "me", "you", "this", "that", "it", "what", "how", "who", "when", "where", "why",
}

# Patterns to extract user facts
NAME_PATTERNS = [
    r"(?:my name is|i'm|i am|call me|they call me)\s+([A-Z][a-z]+)",
    r"(?:mera naam)\s+([A-Za-z]+)(?:\s+hai)?",
]

PREFERENCE_PATTERNS = [
    (r"(?:i (?:like|love|enjoy|prefer))\s+(.+)", "likes"),
    (r"(?:i (?:hate|dislike|don't like))\s+(.+)", "dislikes"),
    (r"(?:my favorite .+ is)\s+(.+)", "favorites"),
    (r"(?:mujhe|main)\s+(.+?)\s+(?:pasand hai|pasand karta hoon|pasand karti hoon)", "likes"),
]


def load():
    """Load memory from disk, or create default if missing."""
    with _LOCK:
        if os.path.exists(MEMORY_FILE):
            try:
                with open(MEMORY_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    # Merge with independent defaults for any missing keys.
                    for key, value in DEFAULT_MEMORY.items():
                        if key not in data:
                            data[key] = deepcopy(value)
                    return data
            except (json.JSONDecodeError, IOError):
                pass
        return deepcopy(DEFAULT_MEMORY)


def save(memory):
    """Save memory to disk."""
    memory["last_interaction"] = datetime.now().isoformat()
    temporary = MEMORY_FILE + ".tmp"
    try:
        with _LOCK:
            with open(temporary, "w", encoding="utf-8") as f:
                json.dump(memory, f, indent=2, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
            os.replace(temporary, MEMORY_FILE)
    except IOError as e:
        print(f"[Memory] Warning: Could not save memory: {e}")


def update_from_conversation(memory, user_msg, assistant_msg):
    """Extract and store key facts from the conversation, with automatic rotation."""
    text = user_msg.strip()

    # Try to extract user's name
    for pattern in NAME_PATTERNS:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            name = match.group(1).strip().rstrip(".,!?")
            if len(name) > 1 and name.isalpha() and name.lower() not in INVALID_NAMES:
                memory["user_name"] = name.capitalize()
                break

    # Try to extract preferences
    for pattern, category in PREFERENCE_PATTERNS:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            value = match.group(1).strip().rstrip(".,!?")
            if category not in memory["preferences"]:
                memory["preferences"][category] = []
            if value not in memory["preferences"][category]:
                memory["preferences"][category].append(value)

    # Append new conversation turn
    memory["conversation_history"].append({
        "user": user_msg[:200],  # Truncate long messages
        "assistant": assistant_msg[:200],
        "time": datetime.now().strftime("%Y-%m-%d %H:%M"),
    })

    # Index into SQLite FTS5 for long-term search
    index_fts_entry(f"User: {user_msg} | Aisha: {assistant_msg}", category="history")

    # Rotate: if history exceeds MAX, compress oldest entries into a summary
    _rotate_history(memory)

    save(memory)
    return memory


def _rotate_history(memory):
    """Compress old conversation turns into a one-line summary when history exceeds MAX_HISTORY."""
    history = memory.get("conversation_history", [])
    if len(history) <= MAX_HISTORY:
        return

    # How many turns to compress (keep the latest MAX_HISTORY)
    overflow = len(history) - MAX_HISTORY
    old_turns = history[:overflow]

    # Build a compressed one-line summary of the old turns
    topics = []
    for turn in old_turns:
        user_text = turn.get("user", "").strip()
        if user_text and len(user_text) > 3:
            # Keep first 40 chars of each user message as a topic hint
            topics.append(user_text[:40].rstrip())

    if topics:
        time_range = old_turns[0].get("time", "?")
        summary_line = f"[{time_range}] Discussed: {'; '.join(topics[:5])}"
        if len(topics) > 5:
            summary_line += f" (+{len(topics) - 5} more)"

        summaries = memory.setdefault("conversation_summaries", [])
        summaries.append(summary_line)
        # Cap summaries list too
        memory["conversation_summaries"] = summaries[-MAX_SUMMARIES:]

    # Drop the old turns
    memory["conversation_history"] = history[overflow:]


def _update_daily_episodes(memory):
    """Organize conversations into daily episodic memories across dates."""
    today = datetime.now().strftime("%Y-%m-%d")
    history = memory.get("conversation_history", [])
    if not history:
        return

    # Extract user queries from today's turns
    today_queries = [
        turn.get("user", "").strip()
        for turn in history
        if turn.get("time", "").startswith(today) and turn.get("user", "").strip()
    ]
    if not today_queries:
        return

    episodes = memory.setdefault("daily_episodes", [])
    # Unique high-level queries
    short_topics = list(dict.fromkeys(q[:35].rstrip() for q in today_queries if len(q) > 3))[:6]
    if short_topics:
        new_entry = f"[{today}] Topics: {', '.join(short_topics)}"
        # Update today's entry if already present, or append
        for i, ep in enumerate(episodes):
            if ep.startswith(f"[{today}]"):
                episodes[i] = new_entry
                break
        else:
            episodes.append(new_entry)

        memory["daily_episodes"] = episodes[-MAX_DAILY_EPISODES:]


def get_context_string(memory):
    """Format memory as a string to inject into the system prompt."""
    parts = []

    if memory.get("user_name"):
        parts.append(f"The user's name is {memory['user_name']}.")

    prefs = memory.get("preferences", {})
    if prefs.get("likes"):
        parts.append(f"The user likes: {', '.join(prefs['likes'][:5])}.")
    if prefs.get("dislikes"):
        parts.append(f"The user dislikes: {', '.join(prefs['dislikes'][:5])}.")
    if prefs.get("favorites"):
        parts.append(f"The user's favorites: {', '.join(prefs['favorites'][:5])}.")

    facts = memory.get("facts", [])
    if facts:
        parts.append(f"Known facts: {'; '.join(facts[:8])}.")

    # Include recent daily episodic context
    episodes = memory.get("daily_episodes", [])
    if episodes:
        parts.append(f"Recent daily context: {' | '.join(episodes[-4:])}")
    elif memory.get("conversation_summaries"):
        summaries = memory.get("conversation_summaries", [])
        parts.append(f"Older topics: {' | '.join(summaries[-3:])}")

    if memory.get("last_interaction"):
        parts.append(f"Last interaction: {memory['last_interaction'][:16]}.")

    return " ".join(parts) if parts else ""


def add_fact(memory, fact):
    """Manually add a fact to memory with dedup, rotation, and FTS5 indexing."""
    facts = memory.setdefault("facts", [])
    # Case-insensitive dedup
    if not any(f.strip().casefold() == fact.strip().casefold() for f in facts):
        facts.append(fact)
        # Rotate: keep only latest MAX_FACTS
        memory["facts"] = facts[-MAX_FACTS:]
        save(memory)
        index_fts_entry(fact, category="fact")
    return memory


def remove_fact(memory, fact):
    """Remove a saved fact by case-insensitive exact match."""
    facts = memory.setdefault("facts", [])
    target = fact.strip().casefold()
    for existing in list(facts):
        if str(existing).strip().casefold() == target:
            facts.remove(existing)
            save(memory)
            return True
    return False


# ----------------------------------------------------------------------
# SQLite FTS5 Full-Text Search Engine for Long-Term Recall
# ----------------------------------------------------------------------
DB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".assistant_data")
DB_FILE = os.path.join(DB_DIR, "memory.db")


def _get_db():
    import sqlite3
    os.makedirs(DB_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_FILE, check_same_thread=False)
    conn.execute("""
        CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(
            content,
            category,
            created_at,
            metadata UNINDEXED
        )
    """)
    conn.commit()
    return conn


def index_fts_entry(content: str, category: str = "general", metadata: str = ""):
    """Index a text fact, conversation, or preference into SQLite FTS5."""
    if not content or len(content.strip()) < 3:
        return
    try:
        with _LOCK:
            conn = _get_db()
            now = datetime.now().isoformat(timespec="seconds")
            conn.execute(
                "INSERT INTO memory_fts (content, category, created_at, metadata) VALUES (?, ?, ?, ?)",
                (content.strip(), category, now, metadata),
            )
            conn.commit()
            conn.close()
    except Exception as e:
        print(f"[Memory FTS5] Index error: {e}")


def search_memory_fts(query: str, limit: int = 5) -> list[dict[str, Any]]:
    """Search long-term memory using SQLite FTS5 BM25 ranking."""
    if not query or len(query.strip()) < 2:
        return []

    # Clean query for FTS5 syntax
    cleaned_terms = [re.sub(r"[^\w\s]", "", t).strip() for t in query.split() if t.strip()]
    if not cleaned_terms:
        return []

    # Build FTS5 match expression: "term1* OR term2*"
    match_expr = " OR ".join(f'"{t}"*' for t in cleaned_terms[:6])

    results = []
    try:
        with _LOCK:
            conn = _get_db()
            cursor = conn.execute(
                "SELECT content, category, created_at, rank FROM memory_fts WHERE memory_fts MATCH ? ORDER BY rank LIMIT ?",
                (match_expr, limit),
            )
            for row in cursor.fetchall():
                results.append({
                    "content": row[0],
                    "category": row[1],
                    "created_at": row[2],
                    "score": round(float(row[3]), 4),
                })
            conn.close()
    except Exception as e:
        print(f"[Memory FTS5] Search error: {e}")

    return results

