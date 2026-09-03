"""Persistent tasks, notes, and reminders for Aisha.

The store is deliberately small and dependency-free.  It writes atomically so an
interrupted assistant process cannot leave the state file half-written.
"""

from __future__ import annotations

import json
import os
import threading
import uuid
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Any


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _short_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


class TaskStore:
    """Thread-safe JSON persistence for personal productivity data."""

    DEFAULT_STATE = {"tasks": [], "notes": [], "reminders": []}

    def __init__(self, data_dir: str | os.PathLike[str] | None = None):
        base = Path(data_dir or os.getenv("AISHA_DATA_DIR") or Path(__file__).parent / ".assistant_data")
        self.data_dir = base.expanduser().resolve()
        self.state_file = self.data_dir / "state.json"
        self._lock = threading.RLock()
        self._state = self._load()

    def _load(self) -> dict[str, list[dict[str, Any]]]:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        if not self.state_file.exists():
            return deepcopy(self.DEFAULT_STATE)
        try:
            with self.state_file.open("r", encoding="utf-8") as handle:
                state = json.load(handle)
            if not isinstance(state, dict):
                raise ValueError("state root must be an object")
            for key, default in self.DEFAULT_STATE.items():
                if not isinstance(state.get(key), list):
                    state[key] = deepcopy(default)
            return state
        except (OSError, ValueError, json.JSONDecodeError):
            backup = self.state_file.with_suffix(f".corrupt-{datetime.now().strftime('%Y%m%d%H%M%S')}.json")
            try:
                self.state_file.replace(backup)
            except OSError:
                pass
            return deepcopy(self.DEFAULT_STATE)

    def _save(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        temporary = self.state_file.with_suffix(".tmp")
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(self._state, handle, indent=2, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, self.state_file)

    def create_task(
        self,
        title: str,
        details: str = "",
        due_at: str | None = None,
        priority: str = "normal",
    ) -> dict[str, Any]:
        if not title.strip():
            raise ValueError("Task title cannot be empty")
        if priority not in {"low", "normal", "high"}:
            raise ValueError("Priority must be low, normal, or high")
        normalized_due = self._normalize_datetime(due_at) if due_at else None
        task = {
            "id": _short_id("task"),
            "title": title.strip(),
            "details": details.strip(),
            "priority": priority,
            "due_at": normalized_due,
            "status": "open",
            "created_at": _now(),
            "completed_at": None,
        }
        with self._lock:
            self._state["tasks"].append(task)
            self._save()
        return deepcopy(task)

    def list_tasks(self, status: str = "open", limit: int = 50) -> list[dict[str, Any]]:
        if status not in {"open", "completed", "all"}:
            raise ValueError("Status must be open, completed, or all")
        with self._lock:
            tasks = self._state["tasks"]
            if status != "all":
                tasks = [task for task in tasks if task.get("status") == status]
            return deepcopy(tasks[-max(1, min(limit, 100)) :])

    def update_task(
        self,
        task_id: str,
        status: str | None = None,
        title: str | None = None,
        details: str | None = None,
        due_at: str | None = None,
        priority: str | None = None,
    ) -> dict[str, Any]:
        if status is not None and status not in {"open", "completed"}:
            raise ValueError("Status must be open or completed")
        if priority is not None and priority not in {"low", "normal", "high"}:
            raise ValueError("Priority must be low, normal, or high")
        with self._lock:
            task = self._find("tasks", task_id)
            if title is not None:
                if not title.strip():
                    raise ValueError("Task title cannot be empty")
                task["title"] = title.strip()
            if details is not None:
                task["details"] = details.strip()
            if priority is not None:
                task["priority"] = priority
            if due_at is not None:
                task["due_at"] = self._normalize_datetime(due_at) if due_at else None
            if status is not None:
                task["status"] = status
                task["completed_at"] = _now() if status == "completed" else None
            self._save()
            return deepcopy(task)

    def delete_task(self, task_id: str) -> dict[str, Any]:
        with self._lock:
            task = self._find("tasks", task_id)
            self._state["tasks"].remove(task)
            self._save()
            return deepcopy(task)

    def add_note(self, title: str, content: str, tags: list[str] | None = None) -> dict[str, Any]:
        if not title.strip() or not content.strip():
            raise ValueError("A note needs both a title and content")
        note = {
            "id": _short_id("note"),
            "title": title.strip(),
            "content": content.strip(),
            "tags": [tag.strip() for tag in (tags or []) if tag.strip()][:10],
            "created_at": _now(),
        }
        with self._lock:
            self._state["notes"].append(note)
            self._save()
        return deepcopy(note)

    def search_notes(self, query: str = "", limit: int = 20) -> list[dict[str, Any]]:
        needle = query.casefold().strip()
        with self._lock:
            notes = self._state["notes"]
            if needle:
                notes = [
                    note
                    for note in notes
                    if needle in f"{note.get('title', '')} {note.get('content', '')} {' '.join(note.get('tags', []))}".casefold()
                ]
            return deepcopy(notes[-max(1, min(limit, 50)) :])

    def delete_note(self, note_id: str) -> dict[str, Any]:
        with self._lock:
            note = self._find("notes", note_id)
            self._state["notes"].remove(note)
            self._save()
            return deepcopy(note)

    def schedule_reminder(self, text: str, remind_at: str) -> dict[str, Any]:
        if not text.strip():
            raise ValueError("Reminder text cannot be empty")
        reminder = {
            "id": _short_id("rem"),
            "text": text.strip(),
            "remind_at": self._normalize_datetime(remind_at),
            "status": "scheduled",
            "created_at": _now(),
            "delivered_at": None,
        }
        with self._lock:
            self._state["reminders"].append(reminder)
            self._save()
        return deepcopy(reminder)

    def list_reminders(self, status: str = "scheduled", limit: int = 50) -> list[dict[str, Any]]:
        if status not in {"scheduled", "delivered", "all"}:
            raise ValueError("Status must be scheduled, delivered, or all")
        with self._lock:
            reminders = self._state["reminders"]
            if status != "all":
                reminders = [item for item in reminders if item.get("status") == status]
            return deepcopy(reminders[-max(1, min(limit, 100)) :])

    def delete_reminder(self, reminder_id: str) -> dict[str, Any]:
        with self._lock:
            reminder = self._find("reminders", reminder_id)
            self._state["reminders"].remove(reminder)
            self._save()
            return deepcopy(reminder)

    def pop_due_reminders(self, now: datetime | None = None) -> list[dict[str, Any]]:
        current = now.astimezone() if now else datetime.now().astimezone()
        due: list[dict[str, Any]] = []
        with self._lock:
            for reminder in self._state["reminders"]:
                if reminder.get("status") != "scheduled":
                    continue
                try:
                    when = datetime.fromisoformat(reminder["remind_at"])
                    if when.tzinfo is None:
                        when = when.astimezone()
                except (KeyError, TypeError, ValueError):
                    continue
                if when <= current:
                    reminder["status"] = "delivered"
                    reminder["delivered_at"] = _now()
                    due.append(deepcopy(reminder))
            if due:
                self._save()
        return due

    def _find(self, collection: str, item_id: str) -> dict[str, Any]:
        for item in self._state[collection]:
            if item.get("id") == item_id:
                return item
        raise KeyError(f"No {collection[:-1]} found with id '{item_id}'")

    @staticmethod
    def _normalize_datetime(value: str) -> str:
        if not value or not value.strip():
            raise ValueError("Date/time cannot be empty")
        normalized = value.strip().replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(normalized)
        except ValueError as exc:
            raise ValueError("Use an ISO date/time such as 2026-08-31T18:30:00+05:30") from exc
        if parsed.tzinfo is None:
            parsed = parsed.astimezone()
        return parsed.isoformat(timespec="seconds")
