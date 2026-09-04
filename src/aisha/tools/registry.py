"""Tool registry for Aisha's native model tool-calling loop.

Tools return compact JSON strings, apply path/network guards, redact configured
secrets, and expose a risk summary before potentially destructive operations.
"""

from __future__ import annotations

import hashlib
import base64
import html
import io
import ipaddress
import json
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.parse
import webbrowser
from dataclasses import dataclass
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable

import requests

from aisha.system import actions
from aisha.core import memory as memory_module
from aisha.core.tasks import TaskStore


MAX_TOOL_OUTPUT = 15_000
MAX_TEXT_FILE_BYTES = 2_000_000
MAX_WEB_BYTES = 500_000
SECRET_ENV_KEYS = {
    "TOKEN",
    "ELEVEN_LAB",
    "ELEVENLABS_API_KEY",
    "API_KEY",
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
}
SENSITIVE_FILE_NAMES = {
    ".env",
    ".npmrc",
    ".pypirc",
    "id_rsa",
    "id_ed25519",
    "credentials.json",
    "secrets.json",
}


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]
    handler: Callable[[dict[str, Any]], Any]

    def api_definition(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
        }

    def openai_definition(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.input_schema,
            },
        }


class _SearchParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.results: list[dict[str, str]] = []
        self._result: dict[str, str] | None = None
        self._capture_title = False
        self._capture_snippet = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        classes = set((attributes.get("class") or "").split())
        if tag == "a" and "result__a" in classes:
            self._result = {"title": "", "url": attributes.get("href") or "", "snippet": ""}
            self._capture_title = True
        elif self._result is not None and tag in {"a", "div"} and "result__snippet" in classes:
            self._capture_snippet = True

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._capture_title:
            self._capture_title = False
            if self._result:
                self.results.append(self._result)
        if tag in {"a", "div"} and self._capture_snippet:
            self._capture_snippet = False

    def handle_data(self, data: str) -> None:
        if self._result is None:
            return
        if self._capture_title:
            self._result["title"] += data
        elif self._capture_snippet:
            self._result["snippet"] += data


class _ReadableHTMLParser(HTMLParser):
    BLOCKED_TAGS = {"script", "style", "noscript", "svg"}

    def __init__(self) -> None:
        super().__init__()
        self.title = ""
        self.parts: list[str] = []
        self._blocked_depth = 0
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self.BLOCKED_TAGS:
            self._blocked_depth += 1
        if tag == "title":
            self._in_title = True
        if tag in {"p", "br", "li", "h1", "h2", "h3", "h4", "tr"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self.BLOCKED_TAGS and self._blocked_depth:
            self._blocked_depth -= 1
        if tag == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._blocked_depth:
            return
        if self._in_title:
            self.title += data
        self.parts.append(data)

    def text(self) -> str:
        value = html.unescape(" ".join(self.parts))
        value = re.sub(r"[ \t\r\f\v]+", " ", value)
        value = re.sub(r"\n\s*\n+", "\n", value)
        return value.strip()


class ToolRegistry:
    """Aisha's capabilities plus safety, persistence, and audit logging."""

    def __init__(
        self,
        workspace: str | os.PathLike[str] | None = None,
        memory: dict[str, Any] | None = None,
        task_store: TaskStore | None = None,
        vision_client: Any = None,
        vision_model: str | None = None,
    ) -> None:
        self.workspace = Path(workspace or os.getcwd()).expanduser().resolve()
        self.memory = memory
        self.store = task_store or TaskStore()
        # Vision-capable client (e.g. Bay of Assets Claude) so Aisha can actually
        # SEE the screen instead of only knowing the active window title.
        self.vision_client = vision_client
        self.vision_model = vision_model or os.getenv("BAYOFASSETS_VISION_MODEL", "claude-sonnet-5")
        self.audit_file = self.store.data_dir / "audit.jsonl"
        self._secrets = [
            value
            for key, value in os.environ.items()
            if value and (key.upper() in SECRET_ENV_KEYS or key.upper().endswith(("_TOKEN", "_SECRET", "_API_KEY")))
        ]
        self._tools = self._build_tools()

    @property
    def definitions(self) -> list[dict[str, Any]]:
        return [tool.api_definition() for tool in self._tools.values()]

    @property
    def openai_definitions(self) -> list[dict[str, Any]]:
        return [tool.openai_definition() for tool in self._tools.values()]

    @property
    def names(self) -> set[str]:
        return set(self._tools)

    def execute(self, name: str, arguments: dict[str, Any] | None) -> str:
        started = time.monotonic()
        arguments = arguments if isinstance(arguments, dict) else {}
        tool = self._tools.get(name)
        if not tool:
            return self._result(False, error=f"Unknown tool '{name}'")
        try:
            value = tool.handler(arguments)
            result = self._result(True, data=value)
            self._audit(name, arguments, "success", time.monotonic() - started)
            return result
        except Exception as exc:
            self._audit(name, arguments, f"error:{type(exc).__name__}", time.monotonic() - started)
            return self._result(False, error=self._redact(str(exc)))

    def confirmation_summary(self, name: str, arguments: dict[str, Any] | None) -> str | None:
        """Return a human-readable warning when execution needs explicit approval."""
        args = arguments if isinstance(arguments, dict) else {}
        if name == "run_python":
            code = str(args.get("code", ""))[:800]
            return f"Python code execute karna hai:\n{code}"
        if name == "run_shell":
            command = str(args.get("command", ""))
            if not self._is_read_only_shell(command):
                return f"PowerShell command execute karna hai:\n{command[:1000]}"
        if name == "write_file":
            path = self._resolve_path(str(args.get("path", "")), allow_missing=True)
            mode = str(args.get("mode", "create"))
            if path.exists() or mode in {"overwrite", "append"} or not self._is_user_write_area(path):
                return f"File system change karni hai: {path} (mode: {mode})"
        if name == "copy_path":
            destination = self._resolve_path(str(args.get("destination", "")), allow_missing=True)
            if destination.exists() or not self._is_user_write_area(destination):
                return f"Copy se existing destination replace ho sakti hai: {destination}"
        if name == "move_path":
            return f"File/folder move karna hai: {args.get('source', '')} -> {args.get('destination', '')}"
        if name == "delete_path":
            return f"File/folder permanently delete karna hai: {args.get('path', '')}"
        # Simple, everyday actions (typing, clicking, opening apps, creating files,
        # setting alarms) run WITHOUT asking — only genuinely risky / irreversible /
        # outward-facing / system-level actions below need a confirmation.
        if name == "windows_action":
            action = str(args.get("action", ""))
            if action in {"close_app", "close_window", "lock_screen", "shutdown", "restart"}:
                return f"'{action}' karna hai (target: {args.get('target', '')}) — pakka?"
        if name == "system_control":
            action = str(args.get("action", "")).lower()
            if action in {"shutdown", "restart", "log_out", "logout", "sign_out",
                          "hibernate", "sleep", "empty_recycle_bin"}:
                return f"System-level action '{action}' chalani hai — pakka?"
        if name == "process_manager" and str(args.get("action", "")).lower() == "kill":
            return f"Process band karni hai: {args.get('name_contains') or args.get('name', '')}"
        if name == "clipboard" and args.get("action") == "read":
            return "Clipboard ka content AI ko bhejna hai; isme private data ho sakta hai — theek hai?"
        if name == "send_email":
            return f"Email send karna hai {args.get('to_email', '')} par: '{args.get('subject', '')}'"
        if name == "send_whatsapp":
            return f"WhatsApp message send karna hai {args.get('phone_number', '')} ko"
        if name == "schedule_task":
            return f"Windows Task Scheduler mein task add karna hai: {args.get('name', '')}"
        if name == "take_photo":
            return "Webcam se photo capture karni hai — theek hai?"
        return None

    def _build_tools(self) -> dict[str, ToolSpec]:
        object_schema = lambda properties, required=None: {
            "type": "object",
            "properties": properties,
            "required": required or [],
            "additionalProperties": False,
        }
        specs = [
            ToolSpec(
                "get_datetime",
                "Get the exact current local date, time, timezone, and UTC offset.",
                object_schema({}),
                self._get_datetime,
            ),
            ToolSpec(
                "get_system_info",
                "Inspect basic computer, Python, disk, and current-workspace information.",
                object_schema({}),
                self._get_system_info,
            ),
            ToolSpec(
                "list_directory",
                "List files and folders. Relative paths start in the assistant workspace.",
                object_schema(
                    {
                        "path": {"type": "string", "description": "Folder path; use . for the workspace."},
                        "recursive": {"type": "boolean", "default": False},
                        "pattern": {"type": "string", "description": "Optional glob such as *.pdf."},
                        "limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 100},
                    },
                    ["path"],
                ),
                self._list_directory,
            ),
            ToolSpec(
                "search_files",
                "Search filenames and text content under a folder. Binary and secret files are skipped.",
                object_schema(
                    {
                        "query": {"type": "string"},
                        "path": {"type": "string", "default": "."},
                        "glob": {"type": "string", "description": "Optional glob such as *.py."},
                        "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 30},
                    },
                    ["query"],
                ),
                self._search_files,
            ),
            ToolSpec(
                "read_file",
                "Read a UTF-8 text file with optional line bounds. Secret credential files are blocked.",
                object_schema(
                    {
                        "path": {"type": "string"},
                        "start_line": {"type": "integer", "minimum": 1, "default": 1},
                        "end_line": {"type": "integer", "minimum": 1},
                    },
                    ["path"],
                ),
                self._read_file,
            ),
            ToolSpec(
                "write_file",
                "Create, overwrite, or append a text file. Existing-file changes require user confirmation.",
                object_schema(
                    {
                        "path": {"type": "string"},
                        "content": {"type": "string"},
                        "mode": {"type": "string", "enum": ["create", "overwrite", "append"], "default": "create"},
                    },
                    ["path", "content"],
                ),
                self._write_file,
            ),
            ToolSpec(
                "copy_path",
                "Copy a file or folder. Replacing an existing destination requires confirmation.",
                object_schema(
                    {"source": {"type": "string"}, "destination": {"type": "string"}},
                    ["source", "destination"],
                ),
                self._copy_path,
            ),
            ToolSpec(
                "move_path",
                "Move or rename a file/folder. Always requires user confirmation.",
                object_schema(
                    {"source": {"type": "string"}, "destination": {"type": "string"}},
                    ["source", "destination"],
                ),
                self._move_path,
            ),
            ToolSpec(
                "delete_path",
                "Permanently delete one file or folder. Always requires user confirmation.",
                object_schema({"path": {"type": "string"}}, ["path"]),
                self._delete_path,
            ),
            ToolSpec(
                "run_shell",
                "Run a PowerShell command with a timeout. Mutating or complex commands require confirmation.",
                object_schema(
                    {
                        "command": {"type": "string"},
                        "cwd": {"type": "string", "default": "."},
                        "timeout": {"type": "integer", "minimum": 1, "maximum": 60, "default": 20},
                    },
                    ["command"],
                ),
                self._run_shell,
            ),
            ToolSpec(
                "run_python",
                "Run short Python code in an isolated child process. Always requires confirmation.",
                object_schema(
                    {
                        "code": {"type": "string"},
                        "cwd": {"type": "string", "default": "."},
                        "timeout": {"type": "integer", "minimum": 1, "maximum": 60, "default": 20},
                    },
                    ["code"],
                ),
                self._run_python,
            ),
            ToolSpec(
                "search_web",
                "Search the live public internet and return result titles, snippets, and source URLs.",
                object_schema(
                    {
                        "query": {"type": "string"},
                        "max_results": {"type": "integer", "minimum": 1, "maximum": 10, "default": 5},
                    },
                    ["query"],
                ),
                self._search_web,
            ),
            ToolSpec(
                "fetch_url",
                "Retrieve readable text from a public HTTP/HTTPS page. Local/private network addresses are blocked.",
                object_schema(
                    {
                        "url": {"type": "string"},
                        "max_chars": {"type": "integer", "minimum": 500, "maximum": 30000, "default": 12000},
                    },
                    ["url"],
                ),
                self._fetch_url,
            ),
            ToolSpec(
                "web_research",
                "Deep web research in ONE call: searches the internet AND reads the top "
                "result pages, returning their real content plus source URLs. Use this "
                "(not just search_web) whenever the user wants you to research a topic, "
                "gather current facts, compare options, or write something grounded in "
                "up-to-date info. Then synthesize the sources into your answer/document.",
                object_schema(
                    {
                        "query": {"type": "string"},
                        "sources": {"type": "integer", "minimum": 1, "maximum": 5, "default": 3, "description": "How many pages to actually read."},
                    },
                    ["query"],
                ),
                self._web_research,
            ),
            ToolSpec(
                "windows_action",
                "Control Windows apps, browser, Spotify, media playback, volume, window snapping, keyboard, and power functions.",
                object_schema(
                    {
                        "action": {
                            "type": "string",
                            "enum": [
                                "open_app", "close_app", "open_url", "web_search", "youtube_search", "play_spotify",
                                "media_play_pause", "media_next", "media_prev", "set_volume", "volume_up", "volume_down",
                                "mute", "snap_window", "maximize_window", "minimize_window", "switch_app",
                                "show_desktop", "close_window", "screenshot", "type_text",
                                "lock_screen", "open_folder", "shutdown", "restart", "cancel_shutdown",
                            ],
                        },
                        "target": {"type": "string", "description": "App, URL, search query, song name, folder, volume level (0-100), snap position (left/right/max/min), or text."},
                    },
                    ["action"],
                ),
                self._windows_action,
            ),
            ToolSpec(
                "inspect_screen",
                "SEE the user's screen. Captures a screenshot and actually reads it with "
                "vision — returns 'screen_view' describing the open app/window, visible "
                "text (chats, messages, errors, labels), buttons and fields. Use this "
                "whenever you need to know what is really on screen or verify that an "
                "action worked. Pass 'question' to ask something specific about the screen.",
                object_schema(
                    {
                        "question": {"type": "string", "description": "Optional specific question, e.g. 'which WhatsApp chat is open?' or 'read the error message'."},
                    },
                    [],
                ),
                self._inspect_screen,
            ),
            ToolSpec(
                "clipboard",
                "Read or replace the Windows clipboard. Reading requires confirmation because it may contain private data.",
                object_schema(
                    {
                        "action": {"type": "string", "enum": ["read", "write"]},
                        "text": {"type": "string"},
                    },
                    ["action"],
                ),
                self._clipboard,
            ),
            ToolSpec(
                "task_manager",
                "Create, list, update, complete, reopen, or delete persistent personal tasks.",
                object_schema(
                    {
                        "action": {"type": "string", "enum": ["create", "list", "update", "delete"]},
                        "task_id": {"type": "string"},
                        "title": {"type": "string"},
                        "details": {"type": "string"},
                        "due_at": {"type": "string", "description": "ISO local date/time with timezone when known."},
                        "priority": {"type": "string", "enum": ["low", "normal", "high"]},
                        "status": {"type": "string", "enum": ["open", "completed", "all"]},
                    },
                    ["action"],
                ),
                self._task_manager,
            ),
            ToolSpec(
                "notes",
                "Save, search, list, or delete persistent notes.",
                object_schema(
                    {
                        "action": {"type": "string", "enum": ["add", "search", "list", "delete"]},
                        "note_id": {"type": "string"},
                        "title": {"type": "string"},
                        "content": {"type": "string"},
                        "tags": {"type": "array", "items": {"type": "string"}},
                        "query": {"type": "string"},
                    },
                    ["action"],
                ),
                self._notes,
            ),
            ToolSpec(
                "reminders",
                "Schedule, list, or delete persistent reminders. Use get_datetime first for relative dates/times.",
                object_schema(
                    {
                        "action": {"type": "string", "enum": ["schedule", "list", "delete"]},
                        "reminder_id": {"type": "string"},
                        "text": {"type": "string"},
                        "remind_at": {"type": "string", "description": "ISO local date/time."},
                        "status": {"type": "string", "enum": ["scheduled", "delivered", "all"]},
                    },
                    ["action"],
                ),
                self._reminders,
            ),
            ToolSpec(
                "memory",
                "Explicitly remember, search, list, check chat history, or forget durable facts and conversation context.",
                object_schema(
                    {
                        "action": {"type": "string", "enum": ["remember", "search", "list", "history", "forget"]},
                        "fact": {"type": "string", "description": "Fact to remember/forget, or search query to find."},
                        "query": {"type": "string", "description": "Optional search query for searching past memory."},
                        "limit": {"type": "integer", "default": 10, "description": "Number of recent conversation turns to retrieve."},
                    },
                    ["action"],
                ),
                self._memory,
            ),
            ToolSpec(
                "transcribe_audio_file",
                "Transcribe any local audio or video file (.mp3, .wav, .m4a, .mp4, .aac, .flac, .ogg) to text using Whisper. Optionally save the transcription to a file.",
                object_schema(
                    {
                        "path": {"type": "string", "description": "Path to the audio/video file to transcribe."},
                        "save_to": {"type": "string", "description": "Optional file path to save the transcribed text into."},
                        "language": {"type": "string", "description": "Optional language code (e.g. 'hi' for Hindi, 'en' for English)."},
                    },
                    ["path"],
                ),
                self._transcribe_audio_file,
            ),
            # ===== ADVANCED AUTOMATION =====
            ToolSpec(
                "window_manager",
                "List, focus, close, minimize, maximize, restore, snap left/right, or tile windows. Use title_contains to match.",
                object_schema(
                    {
                        "action": {"type": "string", "enum": ["list", "focus", "close", "minimize", "maximize", "restore", "snap_left", "snap_right", "tile_split"]},
                        "title_contains": {"type": "string"},
                        "process": {"type": "string"},
                    },
                    ["action"],
                ),
                self._window_manager,
            ),
            ToolSpec(
                "system_control",
                "Lock screen, sleep, hibernate, shutdown, restart, log out, mute/unmute, empty recycle bin, toggle dark mode.",
                object_schema(
                    {
                        "action": {"type": "string", "enum": ["lock", "sleep", "hibernate", "shutdown", "restart", "log_out", "mute_system", "unmute_system", "empty_recycle_bin", "toggle_dark_mode"]},
                        "delay_seconds": {"type": "integer", "default": 0},
                    },
                    ["action"],
                ),
                self._system_control,
            ),
            ToolSpec(
                "brightness_volume",
                "Set screen brightness (0-100) or system volume (0-100).",
                object_schema(
                    {"kind": {"type": "string", "enum": ["brightness", "volume"]}, "level": {"type": "integer", "minimum": 0, "maximum": 100}},
                    ["kind", "level"],
                ),
                self._brightness_volume,
            ),
            ToolSpec(
                "process_manager",
                "List or kill running Windows processes by name or PID.",
                object_schema(
                    {"action": {"type": "string", "enum": ["list", "kill"]}, "name_contains": {"type": "string"}, "pid": {"type": "integer"}},
                    ["action"],
                ),
                self._process_manager,
            ),
            ToolSpec(
                "hotkey",
                "Send keyboard shortcuts: 'ctrl+c', 'alt+tab', 'win+d', 'ctrl+shift+s', etc.",
                object_schema({"keys": {"type": "string"}}, ["keys"]),
                self._hotkey,
            ),
            ToolSpec(
                "computer_control",
                "Full mouse + keyboard control for real multi-step desktop automation "
                "(clicking, typing into apps, pressing keys, navigating UIs). "
                "CRITICAL: keyboard/mouse input goes to whatever window is focused, so "
                "the FIRST step of any automation MUST be a 'focus_window' step naming "
                "the target app (e.g. title 'WhatsApp', 'Chrome', 'Calculator') — "
                "otherwise input lands in the wrong window and nothing happens in the app "
                "you meant. Prefer keyboard navigation (type / press Tab,Enter,Down / "
                "hotkey) over clicking exact pixels, since you cannot see the screen. "
                "Pass a 'steps' array to run an entire process in ONE reliable call, e.g. "
                "open a WhatsApp chat and send a message: "
                "[{action:'focus_window',title:'WhatsApp'},{action:'type',text:'Sona'},"
                "{action:'press',key:'enter'},{action:'wait',seconds:1},"
                "{action:'type',text:'hi'},{action:'press',key:'enter'}].",
                object_schema(
                    {
                        "action": {
                            "type": "string",
                            "enum": ["focus_window", "click", "double_click", "right_click",
                                     "move", "drag", "scroll", "type", "press", "hotkey", "wait"],
                            "description": "Single action to run when 'steps' is not given.",
                        },
                        "title": {"type": "string", "description": "Window title substring to focus (focus_window)."},
                        "text": {"type": "string", "description": "Text to type (Unicode/Hindi supported)."},
                        "key": {"type": "string", "description": "Single key to press: enter, tab, esc, down, up, backspace, etc."},
                        "keys": {"type": "string", "description": "Hotkey combo like 'ctrl+f' or 'alt+tab'."},
                        "x": {"type": "integer"}, "y": {"type": "integer"},
                        "x1": {"type": "integer"}, "y1": {"type": "integer"},
                        "x2": {"type": "integer"}, "y2": {"type": "integer"},
                        "direction": {"type": "string", "enum": ["up", "down"]},
                        "amount": {"type": "integer"},
                        "seconds": {"type": "number", "description": "Seconds to wait (wait action)."},
                        "steps": {
                            "type": "array",
                            "items": {"type": "object", "additionalProperties": True},
                            "description": "Ordered list of step objects (same fields as a single action) "
                                           "to run a full multi-step process in one call. Start with focus_window.",
                        },
                        "step_delay": {"type": "number", "default": 0.35},
                        "verify": {"type": "boolean", "description": "After acting, read the screen back and return 'screen_after' so you can confirm it worked. Defaults to true for meaningful UI actions (type/click/focus/hotkey). Set false to skip the visual check for trivial moves."},
                    },
                    [],
                ),
                self._computer_control,
            ),
            ToolSpec(
                "contacts",
                "Look up, add, list, or remove saved contacts (name -> phone number). "
                "Before sending a WhatsApp/message to a PERSON BY NAME, ALWAYS 'lookup' "
                "their number here first. If lookup returns found:false, ASK the user for "
                "the number (and offer to save it) — NEVER invent or guess a number. "
                "The name may be in Hindi or English; lookup is script/case tolerant.",
                object_schema(
                    {
                        "action": {"type": "string", "enum": ["lookup", "add", "list", "remove"], "default": "lookup"},
                        "name": {"type": "string", "description": "Contact name (Hindi or English)."},
                        "number": {"type": "string", "description": "Phone number with country code (for add)."},
                    },
                    [],
                ),
                self._contacts,
            ),
            ToolSpec(
                "create_document",
                "Create a polished, formatted Word (.docx) document from structured content — "
                "the reliable way to write essays, letters, reports, notes, or any document. "
                "You provide the title and a list of sections (each with a heading, body "
                "paragraphs, and optional bullet points); the tool builds a properly formatted "
                "file (title, headings, justified paragraphs, bullet lists), saves it to the "
                "user's Documents folder, and opens it. Prefer this over typing into Word or "
                "writing python-docx code yourself.",
                object_schema(
                    {
                        "title": {"type": "string", "description": "Document title (main heading)."},
                        "sections": {
                            "type": "array",
                            "description": "Ordered sections of the document.",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "heading": {"type": "string", "description": "Section heading (optional)."},
                                    "body": {"type": "string", "description": "Paragraph text; separate paragraphs with a blank line."},
                                    "bullets": {"type": "array", "items": {"type": "string"}, "description": "Optional bullet points."},
                                },
                                "additionalProperties": False,
                            },
                        },
                        "filename": {"type": "string", "description": "Optional file name without extension."},
                        "append_to": {"type": "string", "description": "To ADD content to an EXISTING .docx (e.g. add references), pass its filename or full path here. The new sections are appended into that document. This is the ONLY correct way to edit a .docx — never use write_file on a document."},
                        "open_after": {"type": "boolean", "default": True},
                    },
                    ["title", "sections"],
                ),
                self._create_document,
            ),
            ToolSpec(
                "create_presentation",
                "Create a PowerPoint (.pptx) presentation from structured slides — the reliable "
                "way to build decks. You provide a deck title and a list of slides (each with a "
                "title and bullet points or body text); the tool builds a themed, formatted "
                "presentation, saves it to Documents, and opens it. Prefer this over automating "
                "PowerPoint's UI.",
                object_schema(
                    {
                        "title": {"type": "string", "description": "Deck title (title slide)."},
                        "subtitle": {"type": "string", "description": "Optional subtitle for the title slide."},
                        "slides": {
                            "type": "array",
                            "description": "Content slides in order.",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "title": {"type": "string", "description": "Slide title."},
                                    "bullets": {"type": "array", "items": {"type": "string"}, "description": "Bullet points for the slide."},
                                    "body": {"type": "string", "description": "Optional paragraph body (used if no bullets)."},
                                },
                                "additionalProperties": False,
                            },
                        },
                        "theme": {"type": "string", "enum": ["violet", "ocean", "sunset", "forest", "mono"], "description": "Optional color theme. Omit for an auto-picked theme. All themes are colorful and designed."},
                        "filename": {"type": "string", "description": "Optional file name without extension."},
                        "open_after": {"type": "boolean", "default": True},
                    },
                    ["title", "slides"],
                ),
                self._create_presentation,
            ),
            ToolSpec(
                "create_spreadsheet",
                "Create an Excel (.xlsx) spreadsheet from structured data — sheets each with "
                "headers and rows. The tool formats a bold header row, auto-sizes columns, saves "
                "to Documents, and opens it. Use for tables, trackers, budgets, and data exports.",
                object_schema(
                    {
                        "sheets": {
                            "type": "array",
                            "description": "One or more sheets.",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "name": {"type": "string", "description": "Sheet/tab name."},
                                    "headers": {"type": "array", "items": {"type": "string"}},
                                    "rows": {"type": "array", "items": {"type": "array"}, "description": "Rows, each an array of cell values."},
                                },
                                "additionalProperties": False,
                            },
                        },
                        "filename": {"type": "string", "description": "Optional file name without extension."},
                        "open_after": {"type": "boolean", "default": True},
                    },
                    ["sheets"],
                ),
                self._create_spreadsheet,
            ),
            ToolSpec(
                "weather",
                "Get current weather and forecast for any city using wttr.in.",
                object_schema({"city": {"type": "string", "default": "auto"}}, []),
                self._weather,
            ),
            ToolSpec(
                "music_search",
                "Search and play any song on YouTube or Spotify.",
                object_schema(
                    {"query": {"type": "string"}, "service": {"type": "string", "enum": ["youtube", "spotify", "auto"], "default": "auto"}},
                    ["query"],
                ),
                self._music_search,
            ),
            ToolSpec(
                "song_lyrics_generate",
                "Generate original Hindi song lyrics on any topic or mood, then speak them using ElevenLabs TTS.",
                object_schema(
                    {
                        "topic": {"type": "string"},
                        "mood": {"type": "string", "enum": ["happy", "sad", "romantic", "energetic", "motivational", "chill"], "default": "chill"},
                        "language": {"type": "string", "enum": ["hindi", "hinglish", "english"], "default": "hindi"},
                        "verses": {"type": "integer", "default": 2},
                    },
                    ["topic"],
                ),
                self._song_lyrics_generate,
            ),
            ToolSpec(
                "timer",
                "Set a countdown timer. Aisha speaks a notification when it ends.",
                object_schema({"duration_seconds": {"type": "integer"}, "label": {"type": "string", "default": "Timer"}}, ["duration_seconds"]),
                self._timer,
            ),
            ToolSpec(
                "send_email",
                "Send an email to any address. Supports Gmail, Outlook, and any SMTP server. "
                "Uses the ELEVEN_LAB or EMAIL_PASSWORD env var for app-specific passwords.",
                object_schema(
                    {
                        "to_email": {"type": "string"},
                        "subject": {"type": "string"},
                        "body": {"type": "string"},
                        "from_email": {"type": "string", "description": "Optional: sender email. Defaults to ELEVEN_LAB or EMAIL_USER env var."},
                    },
                    ["to_email", "subject", "body"],
                ),
                self._send_email,
            ),
            ToolSpec(
                "send_whatsapp",
                "Send a WhatsApp message to a phone number via web.whatsapp.com. "
                "Number should be in international format with country code, e.g. '919876543210' (no +, no spaces).",
                object_schema(
                    {
                        "phone_number": {"type": "string", "description": "Phone number with country code, no + or spaces. E.g. 919876543210"},
                        "message": {"type": "string"},
                        "wait_time": {"type": "integer", "minimum": 10, "maximum": 120, "default": 30},
                    },
                    ["phone_number", "message"],
                ),
                self._send_whatsapp,
            ),
            ToolSpec(
                "schedule_task",
                "Schedule a Windows Task Scheduler job — runs a command/script at a specific time or on a schedule. "
                "Supports one-time, daily, weekly, and startup triggers.",
                object_schema(
                    {
                        "name": {"type": "string"},
                        "command": {"type": "string"},
                        "trigger": {"type": "string", "enum": ["once", "daily", "weekly", "at_startup"], "default": "once"},
                        "start_time": {"type": "string", "description": "ISO datetime or 'HH:MM' for once/daily/weekly. E.g. '2025-01-15T09:00' or '09:00'"},
                        "days": {"type": "string", "description": "Comma-separated days for weekly trigger: Mon,Tue,Wed,Thu,Fri,Sat,Sun"},
                    },
                    ["name", "command"],
                ),
                self._schedule_task,
            ),
            ToolSpec(
                "set_alarm",
                "Set a timed alarm. Aisha will play a chime and speak when the alarm triggers. "
                "Persists across app restarts via task_store.",
                object_schema(
                    {"time": {"type": "string", "description": "Time in HH:MM format (24h), e.g. '07:30'"}, "label": {"type": "string", "default": "Alarm"}},
                    ["time"],
                ),
                self._set_alarm,
            ),
            ToolSpec(
                "take_photo",
                "Capture a photo from the default webcam using Windows Media Capture or OpenCV.",
                object_schema({"save_path": {"type": "string", "default": ""}}, []),
                self._take_photo,
            ),
            ToolSpec(
                "search_and_open_file",
                "Search for a file by name, then open it with its default application.",
                object_schema({"query": {"type": "string"}}, ["query"]),
                self._search_and_open_file,
            ),
            ToolSpec(
                "get_battery_info",
                "Get current battery status: level, charging state, estimated time remaining.",
                object_schema({}, []),
                self._get_battery_info,
            ),
            ToolSpec(
                "open_url",
                "Open a URL in the default web browser.",
                object_schema({"url": {"type": "string"}}, ["url"]),
                self._open_url,
            ),
        ]
        return {spec.name: spec for spec in specs}

    def _get_datetime(self, _: dict[str, Any]) -> dict[str, Any]:
        now = datetime.now().astimezone()
        return {
            "iso": now.isoformat(timespec="seconds"),
            "date": now.strftime("%A, %d %B %Y"),
            "time": now.strftime("%I:%M:%S %p"),
            "timezone": str(now.tzinfo),
            "utc_offset": now.strftime("%z"),
        }

    def _get_system_info(self, _: dict[str, Any]) -> dict[str, Any]:
        usage = shutil.disk_usage(self.workspace.anchor)
        return {
            "computer": platform.node(),
            "os": f"{platform.system()} {platform.release()} ({platform.version()})",
            "architecture": platform.machine(),
            "python": platform.python_version(),
            "cpu_count": os.cpu_count(),
            "workspace": str(self.workspace),
            "disk_free_gb": round(usage.free / (1024**3), 2),
            "disk_total_gb": round(usage.total / (1024**3), 2),
        }

    def _list_directory(self, args: dict[str, Any]) -> dict[str, Any]:
        root = self._resolve_path(str(args.get("path", ".")))
        if not root.is_dir():
            raise NotADirectoryError(str(root))
        recursive = bool(args.get("recursive", False))
        pattern = str(args.get("pattern") or "*")
        limit = max(1, min(int(args.get("limit", 100)), 200))
        iterator = root.rglob(pattern) if recursive else root.glob(pattern)
        entries = []
        for path in iterator:
            if self._is_sensitive(path) or self._is_internal_data(path):
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            entries.append(
                {
                    "path": str(path),
                    "type": "directory" if path.is_dir() else "file",
                    "size": stat.st_size if path.is_file() else None,
                    "modified": datetime.fromtimestamp(stat.st_mtime).astimezone().isoformat(timespec="seconds"),
                }
            )
            if len(entries) >= limit:
                break
        return {"root": str(root), "entries": entries, "truncated": len(entries) >= limit}

    def _search_files(self, args: dict[str, Any]) -> dict[str, Any]:
        query = str(args.get("query", "")).strip()
        if not query:
            raise ValueError("query cannot be empty")
        root = self._resolve_path(str(args.get("path", ".")))
        if not root.is_dir():
            raise NotADirectoryError(str(root))
        pattern = str(args.get("glob") or "*")
        limit = max(1, min(int(args.get("limit", 30)), 100))
        needle = query.casefold()
        matches: list[dict[str, Any]] = []
        for path in root.rglob(pattern):
            if not path.is_file() or self._is_sensitive(path) or self._is_internal_data(path):
                continue
            if needle in path.name.casefold():
                matches.append({"path": str(path), "kind": "filename"})
            try:
                if path.stat().st_size > MAX_TEXT_FILE_BYTES:
                    continue
                raw = path.read_bytes()
                if b"\x00" in raw[:2048]:
                    continue
                text = raw.decode("utf-8", errors="ignore")
            except OSError:
                continue
            for number, line in enumerate(text.splitlines(), 1):
                if needle in line.casefold():
                    matches.append({"path": str(path), "kind": "content", "line": number, "text": line[:400]})
                    break
            if len(matches) >= limit:
                break
        return {"query": query, "matches": matches[:limit], "truncated": len(matches) >= limit}

    def _read_file(self, args: dict[str, Any]) -> dict[str, Any]:
        path = self._resolve_path(str(args.get("path", "")))
        self._guard_read(path)
        if not path.is_file():
            raise FileNotFoundError(str(path))
        if path.stat().st_size > MAX_TEXT_FILE_BYTES:
            raise ValueError(f"File is larger than {MAX_TEXT_FILE_BYTES:,} bytes")
        raw = path.read_bytes()
        if b"\x00" in raw[:2048]:
            raise ValueError("Binary files cannot be read with read_file")
        lines = raw.decode("utf-8", errors="replace").splitlines()
        start = max(1, int(args.get("start_line", 1)))
        end = min(len(lines), int(args.get("end_line") or min(len(lines), start + 399)))
        if end < start:
            raise ValueError("end_line must be greater than or equal to start_line")
        content = "\n".join(f"{index}: {lines[index - 1]}" for index in range(start, end + 1))
        return {"path": str(path), "start_line": start, "end_line": end, "total_lines": len(lines), "content": content}

    def _write_file(self, args: dict[str, Any]) -> dict[str, Any]:
        path = self._resolve_path(str(args.get("path", "")), allow_missing=True)
        self._guard_write(path)
        # Office files are binary zip archives — writing/appending text to them
        # corrupts them or is silently ignored. Route the model to the right skill.
        if path.suffix.lower() in {".docx", ".doc", ".pptx", ".ppt", ".xlsx", ".xls"}:
            raise ValueError(
                f"'{path.suffix}' is a binary document — write_file cannot create or edit it. "
                "Use create_document / create_presentation / create_spreadsheet instead "
                "(to add to an existing document, call create_document with append_to)."
            )
        content = str(args.get("content", ""))
        mode = str(args.get("mode", "create"))
        if mode not in {"create", "overwrite", "append"}:
            raise ValueError("mode must be create, overwrite, or append")
        if mode == "create" and path.exists():
            raise FileExistsError(f"'{path}' already exists; use overwrite after confirmation")
        path.parent.mkdir(parents=True, exist_ok=True)
        file_mode = "a" if mode == "append" else "w"
        with path.open(file_mode, encoding="utf-8", newline="") as handle:
            if mode == "append" and path.stat().st_size:
                handle.write("\n")
            handle.write(content)
        return {"path": str(path), "mode": mode, "characters": len(content)}

    def _copy_path(self, args: dict[str, Any]) -> dict[str, Any]:
        source = self._resolve_path(str(args.get("source", "")))
        destination = self._resolve_path(str(args.get("destination", "")), allow_missing=True)
        self._guard_read(source)
        self._guard_write(destination)
        if not source.exists():
            raise FileNotFoundError(str(source))
        if source.is_dir():
            shutil.copytree(source, destination, dirs_exist_ok=destination.exists())
        else:
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
        return {"source": str(source), "destination": str(destination)}

    def _move_path(self, args: dict[str, Any]) -> dict[str, Any]:
        source = self._resolve_path(str(args.get("source", "")))
        destination = self._resolve_path(str(args.get("destination", "")), allow_missing=True)
        self._guard_destructive(source)
        self._guard_write(destination)
        if not source.exists():
            raise FileNotFoundError(str(source))
        destination.parent.mkdir(parents=True, exist_ok=True)
        result = shutil.move(str(source), str(destination))
        return {"source": str(source), "destination": str(Path(result))}

    def _delete_path(self, args: dict[str, Any]) -> dict[str, Any]:
        path = self._resolve_path(str(args.get("path", "")))
        self._guard_destructive(path)
        if not path.exists():
            raise FileNotFoundError(str(path))
        if path.is_dir():
            shutil.rmtree(path)
            kind = "directory"
        else:
            path.unlink()
            kind = "file"
        return {"path": str(path), "deleted": kind, "recoverable": False}

    def _run_shell(self, args: dict[str, Any]) -> dict[str, Any]:
        command = str(args.get("command", "")).strip()
        if not command:
            raise ValueError("command cannot be empty")
        cwd = self._resolve_path(str(args.get("cwd", ".")))
        timeout = max(1, min(int(args.get("timeout", 20)), 60))
        result = subprocess.run(
            ["powershell", "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", command],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=self._sanitized_environment(),
        )
        return {
            "exit_code": result.returncode,
            "stdout": self._redact(result.stdout.strip())[-8000:],
            "stderr": self._redact(result.stderr.strip())[-4000:],
        }

    def _run_python(self, args: dict[str, Any]) -> dict[str, Any]:
        code = str(args.get("code", ""))
        if not code.strip():
            raise ValueError("code cannot be empty")
        cwd = self._resolve_path(str(args.get("cwd", ".")))
        timeout = max(1, min(int(args.get("timeout", 20)), 60))
        result = subprocess.run(
            [sys.executable, "-I", "-c", code],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=self._sanitized_environment(),
        )
        return {
            "exit_code": result.returncode,
            "stdout": self._redact(result.stdout.strip())[-8000:],
            "stderr": self._redact(result.stderr.strip())[-4000:],
        }

    def _search_web(self, args: dict[str, Any]) -> dict[str, Any]:
        query = str(args.get("query", "")).strip()
        if not query:
            raise ValueError("query cannot be empty")
        limit = max(1, min(int(args.get("max_results", 5)), 10))
        url = "https://html.duckduckgo.com/html/?" + urllib.parse.urlencode({"q": query})
        response = self._public_request(url, max_bytes=MAX_WEB_BYTES)
        parser = _SearchParser()
        parser.feed(response.text)
        results = []
        for item in parser.results[:limit]:
            target = item["url"]
            parsed = urllib.parse.urlparse(target)
            if parsed.netloc.endswith("duckduckgo.com"):
                target = urllib.parse.parse_qs(parsed.query).get("uddg", [target])[0]
            if target.startswith("//"):
                target = "https:" + target
            results.append(
                {
                    "title": re.sub(r"\s+", " ", html.unescape(item["title"])).strip(),
                    "url": target,
                    "snippet": re.sub(r"\s+", " ", html.unescape(item["snippet"])).strip(),
                }
            )
        return {"query": query, "results": results, "source": "DuckDuckGo", "retrieved_at": datetime.now().astimezone().isoformat(timespec="seconds")}

    def _fetch_url(self, args: dict[str, Any]) -> dict[str, Any]:
        url = str(args.get("url", "")).strip()
        max_chars = max(500, min(int(args.get("max_chars", 12000)), 30000))
        response = self._public_request(url, max_bytes=MAX_WEB_BYTES)
        content_type = response.headers.get("Content-Type", "").split(";", 1)[0].lower()
        allowed = content_type.startswith("text/") or content_type in {"application/json", "application/xml", "application/xhtml+xml"}
        if not allowed:
            raise ValueError(f"Unsupported web content type: {content_type or 'unknown'}")
        if "html" in content_type:
            parser = _ReadableHTMLParser()
            parser.feed(response.text)
            page_text = parser.text()
            title = re.sub(r"\s+", " ", parser.title).strip()
        else:
            page_text = response.text.strip()
            title = ""
        return {
            "url": response.url,
            "status": response.status_code,
            "title": title,
            "content_type": content_type,
            "text": page_text[:max_chars],
            "truncated": len(page_text) > max_chars,
            "retrieved_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        }

    def _web_research(self, args: dict[str, Any]) -> dict[str, Any]:
        """Search the web and read the top pages, returning grounded source content."""
        query = str(args.get("query", "")).strip()
        if not query:
            raise ValueError("query cannot be empty")
        depth = max(1, min(int(args.get("sources", 3)), 5))
        search = self._search_web({"query": query, "max_results": depth + 2})
        results = search.get("results", [])
        sources: list[dict[str, Any]] = []
        for r in results:
            if len(sources) >= depth:
                break
            url = r.get("url", "")
            entry = {"title": r.get("title", ""), "url": url, "snippet": r.get("snippet", "")}
            try:
                page = self._fetch_url({"url": url, "max_chars": 4000})
                entry["content"] = page.get("text", "")
            except Exception as exc:
                entry["content"] = ""
                entry["error"] = str(exc)[:100]
            sources.append(entry)
        return {"query": query, "count": len(sources), "sources": sources,
                "retrieved_at": datetime.now().astimezone().isoformat(timespec="seconds")}

    def _windows_action(self, args: dict[str, Any]) -> dict[str, Any]:
        action = str(args.get("action", ""))
        target = str(args.get("target", ""))
        params: dict[str, Any] = {}
        if action in {"open_app", "close_app"}:
            params["name"] = target
        elif action == "open_url":
            params["url"] = target
        elif action in {"web_search", "youtube_search", "play_spotify"}:
            params["query"] = target
        elif action == "type_text":
            params["text"] = target
        elif action == "open_folder":
            params["name"] = target
        elif action == "set_volume":
            params["level"] = target or 50
        elif action == "snap_window":
            params["position"] = target or "left"
        result = actions.execute_action({"action": action, "params": params})
        return {"action": action, "result": result}

    def _capture_screen_b64(self, max_width: int = 1400) -> str | None:
        """Grab the current screen and return a base64 PNG (downscaled for speed)."""
        try:
            from PIL import ImageGrab
            img = ImageGrab.grab()
            if img.width > max_width:
                ratio = max_width / img.width
                img = img.resize((max_width, int(img.height * ratio)))
            buf = io.BytesIO()
            img.convert("RGB").save(buf, format="PNG")
            return base64.b64encode(buf.getvalue()).decode()
        except Exception:
            return None

    def _vision_describe(self, prompt: str, b64: str | None = None) -> dict[str, Any]:
        """Read the current screen with the vision model; returns screen_view text."""
        if self.vision_client is None:
            return {"screen_view": None, "vision_error": "vision not configured"}
        if b64 is None:
            b64 = self._capture_screen_b64()
        if not b64:
            return {"screen_view": None, "vision_error": "could not capture screen"}
        try:
            resp = self.vision_client.chat.completions.create(
                model=self.vision_model,
                max_tokens=400,
                messages=[{"role": "user", "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
                ]}],
                timeout=40,
            )
            return {"screen_view": (resp.choices[0].message.content or "").strip(), "vision_error": None}
        except Exception as exc:
            return {"screen_view": None, "vision_error": str(exc)[:160]}

    def _inspect_screen(self, args: dict[str, Any]) -> dict[str, Any]:
        active_info = actions.get_active_window_info()
        result: dict[str, Any] = {
            "active_window_title": active_info.get("title", "Desktop"),
            "active_application_process": active_info.get("process", "explorer.exe"),
            "inspected_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        }

        # Real vision: read what is actually on screen (text, chats, buttons, errors).
        question = str(args.get("question", "")).strip()
        if self.vision_client is not None:
            prompt = question or (
                "Describe what is currently on the screen: which app/window is open, "
                "the key visible text, buttons or fields, and anything the user might "
                "act on. Be concise and concrete."
            )
            result.update(self._vision_describe(prompt))
        else:
            # No vision configured — still save a screenshot to disk as before.
            result["screenshot_status"] = actions._screenshot() if bool(args.get("save_screenshot", True)) else ""
            result["screen_view"] = None

        return result

    def _clipboard(self, args: dict[str, Any]) -> dict[str, Any]:
        action = str(args.get("action", ""))
        if action == "read":
            result = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", "Get-Clipboard -Raw"],
                capture_output=True,
                text=True,
                timeout=5,
                env=self._sanitized_environment(),
            )
            if result.returncode:
                raise RuntimeError(result.stderr.strip() or "Could not read clipboard")
            return {"text": self._redact(result.stdout)[:12000]}
        if action == "write":
            text = str(args.get("text", ""))
            result = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", "Set-Clipboard -Value ([Console]::In.ReadToEnd())"],
                input=text,
                capture_output=True,
                text=True,
                timeout=5,
                env=self._sanitized_environment(),
            )
            if result.returncode:
                raise RuntimeError(result.stderr.strip() or "Could not write clipboard")
            return {"characters": len(text)}
        raise ValueError("action must be read or write")

    def _task_manager(self, args: dict[str, Any]) -> Any:
        action = str(args.get("action", ""))
        if action == "create":
            return self.store.create_task(
                title=str(args.get("title", "")),
                details=str(args.get("details", "")),
                due_at=args.get("due_at"),
                priority=str(args.get("priority", "normal")),
            )
        if action == "list":
            return self.store.list_tasks(status=str(args.get("status", "open")))
        if action == "update":
            task_id = self._required(args, "task_id")
            status = args.get("status")
            if status == "all":
                raise ValueError("status all is only valid when listing")
            return self.store.update_task(
                task_id=task_id,
                status=status,
                title=args.get("title"),
                details=args.get("details"),
                due_at=args.get("due_at"),
                priority=args.get("priority"),
            )
        if action == "delete":
            return self.store.delete_task(self._required(args, "task_id"))
        raise ValueError("Unsupported task action")

    def _notes(self, args: dict[str, Any]) -> Any:
        action = str(args.get("action", ""))
        if action == "add":
            return self.store.add_note(
                self._required(args, "title"),
                self._required(args, "content"),
                args.get("tags"),
            )
        if action in {"search", "list"}:
            return self.store.search_notes(str(args.get("query", "")) if action == "search" else "")
        if action == "delete":
            return self.store.delete_note(self._required(args, "note_id"))
        raise ValueError("Unsupported note action")

    def _reminders(self, args: dict[str, Any]) -> Any:
        action = str(args.get("action", ""))
        if action == "schedule":
            return self.store.schedule_reminder(self._required(args, "text"), self._required(args, "remind_at"))
        if action == "list":
            return self.store.list_reminders(str(args.get("status", "scheduled")))
        if action == "delete":
            return self.store.delete_reminder(self._required(args, "reminder_id"))
        raise ValueError("Unsupported reminder action")

    def _memory(self, args: dict[str, Any]) -> Any:
        if self.memory is None:
            raise RuntimeError("Memory is not connected to this assistant instance")
        action = str(args.get("action", ""))
        if action in {"list", "history"}:
            limit = max(1, min(int(args.get("limit", 10)), 20))
            hist = list(self.memory.get("conversation_history", []))[-limit:]
            return {
                "recent_history": hist,
                "facts": list(self.memory.get("facts", [])),
                "preferences": self.memory.get("preferences", {}),
                "summaries": self.memory.get("conversation_summaries", []),
                "total_turns": len(self.memory.get("conversation_history", [])),
            }
        if action == "search":
            query = str(args.get("fact", "") or args.get("query", "")).strip()
            fts_results = memory_module.search_memory_fts(query, limit=5)
            matching_turns = []
            q_lower = query.lower()
            for turn in reversed(self.memory.get("conversation_history", [])):
                if q_lower in turn.get("user", "").lower() or q_lower in turn.get("assistant", "").lower():
                    matching_turns.append(turn)
                    if len(matching_turns) >= 5:
                        break
            return {"results": fts_results, "matching_conversations": matching_turns}
        fact = self._required(args, "fact").strip()
        if action == "remember":
            memory_module.add_fact(self.memory, fact)
            return {"remembered": fact}
        if action == "forget":
            removed = memory_module.remove_fact(self.memory, fact)
            return {"forgotten": fact, "removed": removed}
        raise ValueError("Unsupported memory action")

    def _transcribe_audio_file(self, args: dict[str, Any]) -> dict[str, Any]:
        path = self._resolve_path(str(args.get("path", "")))
        self._guard_read(path)
        if not path.is_file():
            raise FileNotFoundError(f"Audio file not found: {path}")

        text = ""
        lang_detected = args.get("language", "auto")

        # 1. Try ElevenLabs Scribe cloud STT first
        eleven_key = os.getenv("ELEVEN_LAB")
        if eleven_key:
            try:
                from elevenlabs.client import ElevenLabs
                client = ElevenLabs(api_key=eleven_key)
                with open(str(path), "rb") as f:
                    res = client.speech_to_text.convert(file=f, model_id="scribe_v1")
                    text = getattr(res, "text", "").strip()
                    lang_detected = getattr(res, "language_code", "auto")
            except Exception:
                pass

        # 2. Fallback to local Whisper
        if not text:
            try:
                import whisper
            except ImportError:
                raise RuntimeError("whisper is not installed. Run install.bat.")

            model_name = os.getenv("WHISPER_MODEL", "base")
            model = whisper.load_model(model_name)
            lang = args.get("language")
            options = {"fp16": False}
            if lang:
                options["language"] = lang

            try:
                import soundfile as sf
                import numpy as np

                audio, sr = sf.read(str(path), dtype="float32")
                if audio.ndim > 1:
                    audio = np.mean(audio, axis=1)
                if sr != 16000:
                    orig_len = len(audio)
                    new_len = int(orig_len * 16000 / sr)
                    audio = np.interp(np.linspace(0, orig_len, new_len, endpoint=False), np.arange(orig_len), audio).astype(np.float32)
                result = model.transcribe(audio, **options)
            except Exception:
                result = model.transcribe(str(path), **options)

            text = str(result.get("text", "")).strip()
            lang_detected = result.get("language", "unknown")

        save_to = args.get("save_to")
        saved_path = None
        if save_to:
            out_file = self._resolve_path(str(save_to), allow_missing=True)
            self._guard_write(out_file)
            out_file.parent.mkdir(parents=True, exist_ok=True)
            with out_file.open("w", encoding="utf-8") as f:
                f.write(text)
            saved_path = str(out_file)

        return {
            "source_file": str(path),
            "language": result.get("language", "unknown"),
            "character_count": len(text),
            "text": text,
            "saved_to": saved_path,
        }

    def _public_request(self, url: str, max_bytes: int, max_redirects: int = 5) -> requests.Response:
        current = url.strip()
        if not current:
            raise ValueError("URL cannot be empty")
        if not hasattr(self, "_http_session") or self._http_session is None:
            self._http_session = requests.Session()
            adapter = requests.adapters.HTTPAdapter(pool_connections=10, pool_maxsize=20, max_retries=2)
            self._http_session.mount("http://", adapter)
            self._http_session.mount("https://", adapter)
        session = self._http_session
        headers = {"User-Agent": "AishaAssistant/2.0 (+local personal assistant)", "Accept": "text/html,application/json,text/plain;q=0.9,*/*;q=0.2"}
        for _ in range(max_redirects + 1):
            self._validate_public_url(current)
            response = session.get(current, headers=headers, timeout=(4, 10), allow_redirects=False, stream=True)
            if response.is_redirect or response.is_permanent_redirect:
                location = response.headers.get("Location")
                response.close()
                if not location:
                    raise RuntimeError("Redirect did not include a destination")
                current = urllib.parse.urljoin(current, location)
                continue
            chunks = []
            total = 0
            for chunk in response.iter_content(chunk_size=16384):
                total += len(chunk)
                if total > max_bytes:
                    response.close()
                    raise ValueError(f"Web response exceeded {max_bytes:,} bytes")
                chunks.append(chunk)
            response._content = b"".join(chunks)
            response._content_consumed = True
            response.url = current
            response.raise_for_status()
            return response
        raise RuntimeError("Too many redirects")

    @staticmethod
    def _validate_public_url(url: str) -> None:
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("Only public http:// and https:// URLs are allowed")
        hostname = parsed.hostname.rstrip(".").lower()
        if hostname == "localhost" or hostname.endswith((".localhost", ".local")):
            raise ValueError("Local network URLs are blocked")
        try:
            addresses = {item[4][0] for item in socket.getaddrinfo(hostname, parsed.port or (443 if parsed.scheme == "https" else 80))}
        except socket.gaierror as exc:
            raise ValueError(f"Could not resolve host '{hostname}'") from exc
        for address in addresses:
            ip = ipaddress.ip_address(address)
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved or ip.is_unspecified:
                raise ValueError("Local/private network URLs are blocked")

    def _resolve_path(self, value: str, allow_missing: bool = False) -> Path:
        if not value.strip():
            raise ValueError("path cannot be empty")
        path = Path(os.path.expandvars(value)).expanduser()
        if not path.is_absolute():
            path = self.workspace / path
        path = path.resolve(strict=not allow_missing)
        return path

    def _guard_read(self, path: Path) -> None:
        if self._is_sensitive(path):
            raise PermissionError("Reading credential/secret files through the AI is blocked")

    def _guard_write(self, path: Path) -> None:
        if self._is_sensitive(path):
            raise PermissionError("Writing credential/secret files through the AI is blocked")
        if path == Path(path.anchor) or path == Path.home().resolve() or path == self.workspace:
            raise PermissionError("A broad root directory cannot be a file-operation target")

    def _guard_destructive(self, path: Path) -> None:
        self._guard_write(path)
        protected = {Path(path.anchor), Path.home().resolve(), self.workspace, self.store.data_dir}
        if path in protected:
            raise PermissionError("Deleting or moving a protected root is blocked")

    def _is_sensitive(self, path: Path) -> bool:
        name = path.name.casefold()
        if name in SENSITIVE_FILE_NAMES or name.startswith(".env."):
            return True
        lowered = str(path).casefold().replace("/", "\\")
        sensitive_parts = ["\\.ssh\\", "\\credentials\\", "\\microsoft\\credentials\\", "\\google\\chrome\\user data\\"]
        return any(part in lowered for part in sensitive_parts)

    def _is_internal_data(self, path: Path) -> bool:
        try:
            path.resolve().relative_to(self.store.data_dir)
            return True
        except (OSError, ValueError):
            return False

    def _is_user_write_area(self, path: Path) -> bool:
        roots = {
            self.workspace,
            Path.home().resolve() / "Desktop",
            Path.home().resolve() / "Documents",
            Path.home().resolve() / "Downloads",
        }
        resolved = path.resolve(strict=False)
        for root in roots:
            try:
                resolved.relative_to(root.resolve(strict=False))
                return True
            except ValueError:
                continue
        return False

    @staticmethod
    def _is_read_only_shell(command: str) -> bool:
        if not command.strip() or re.search(r"[;\r\n|&<>()]|\$\(|`", command):
            return False
        safe_patterns = [
            r"^(Get-ChildItem|dir|ls)(\s|$)",
            r"^(Get-Date|Get-Process|Get-Service|Get-Location|Test-Path)(\s|$)",
            r"^git\s+status(?:\s+(?:--short|-s|--branch|-b|--porcelain(?:=v[12])?))*\s*$",
            r"^git\s+branch(?:\s+(?:--list|--show-current))*\s*$",
            r"^git\s+log(?:\s+(?:--oneline|--decorate|--all|-n\s+[0-9]+))*\s*$",
            r"^(python|py)\s+--version$",
            r"^(python|py)\s+-m\s+pip\s+(show|list)(\s|$)",
            r"^(where\.exe|Get-Command)(\s|$)",
        ]
        return any(re.match(pattern, command.strip(), re.IGNORECASE) for pattern in safe_patterns)

    def _sanitized_environment(self) -> dict[str, str]:
        sanitized = {}
        for key, value in os.environ.items():
            upper = key.upper()
            if upper in SECRET_ENV_KEYS or upper.endswith(("_TOKEN", "_SECRET", "_PASSWORD", "_API_KEY")):
                continue
            sanitized[key] = value
        return sanitized

    def _redact(self, value: str) -> str:
        redacted = value
        for secret in sorted(self._secrets, key=len, reverse=True):
            if len(secret) >= 6:
                redacted = redacted.replace(secret, "[REDACTED]")
        redacted = re.sub(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]{12,}", r"\1[REDACTED]", redacted)
        return redacted

    def _audit(self, name: str, arguments: dict[str, Any], outcome: str, duration: float) -> None:
        safe_args: dict[str, Any] = {}
        for key, value in arguments.items():
            if key in {"content", "text", "code"}:
                raw = str(value)
                safe_args[key] = {"characters": len(raw), "sha256": hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]}
            elif key == "command":
                safe_args[key] = self._redact(str(value))[:1000]
            else:
                safe_args[key] = value
        record = {
            "time": datetime.now().astimezone().isoformat(timespec="seconds"),
            "tool": name,
            "arguments": safe_args,
            "outcome": outcome,
            "duration_ms": round(duration * 1000),
        }
        try:
            self.audit_file.parent.mkdir(parents=True, exist_ok=True)
            with self.audit_file.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError:
            pass

    def _result(self, ok: bool, data: Any = None, error: str | None = None) -> str:
        payload: dict[str, Any] = {"ok": ok}
        if ok:
            payload["data"] = data
        else:
            payload["error"] = error or "Unknown error"
        rendered = json.dumps(payload, ensure_ascii=False, default=str)
        if len(rendered) > MAX_TOOL_OUTPUT:
            rendered = json.dumps(
                {"ok": ok, "data": rendered[:MAX_TOOL_OUTPUT], "truncated": True},
                ensure_ascii=False,
            )
        return self._redact(rendered)


    # ===== ADVANCED AUTOMATION HANDLERS =====

    def _window_manager(self, args: dict) -> dict:
        import subprocess
        action = str(args.get("action", "list"))
        title = str(args.get("title_contains", ""))
        proc = str(args.get("process", ""))
        result = {"action": action}

        if action == "list":
            ps_cmd = "Get-Process | Where-Object {$_.MainWindowTitle -ne ''} | Select-Object Id, ProcessName, MainWindowTitle -First 20 | ConvertTo-Json"
            ps = subprocess.run(["powershell", "-NoProfile", "-Command", ps_cmd], capture_output=True, text=True, timeout=10)
            try:
                import json as _json
                data = _json.loads(ps.stdout)
                result["windows"] = [{"pid": w.get("Id"), "process": w.get("ProcessName"), "title": w.get("MainWindowTitle")} for w in data]
            except Exception:
                result["windows"] = ps.stdout.strip()[:3000]
            result["stderr"] = ps.stderr.strip()[:300]

        elif action in ("close", "minimize", "maximize", "restore", "focus"):
            filter_parts = []
            if proc:
                filter_parts.append(f"-Name '{proc}'")
            if title:
                filter_parts.append(f"| Where-Object {{$_.MainWindowTitle -like '*{title}*'}}")
            filter_str = " ".join(filter_parts)
            ps_cmd = f"$p = Get-Process {filter_str} -ErrorAction SilentlyContinue | Select-Object -First 1; if ($p) {{ $p.Id }} else {{ 0 }}"
            ps = subprocess.run(["powershell", "-NoProfile", "-Command", ps_cmd], capture_output=True, text=True, timeout=10)
            pid_str = ps.stdout.strip()
            if not pid_str or pid_str == "0":
                result["status"] = "not_found"
                result["message"] = f"Window matching '{title or proc}' not found"
                return result
            pid = int(pid_str)
            action_map = {
                "close": "Stop-Process -Id %d -Force",
                "minimize": "$w = Get-Process -Id %d; $wshell = New-Object -ComObject WScript.Shell; $wshell.AppActivate($w.MainWindowTitle); (New-Object -ComObject Shell.Application).MinimizeAll()",
                "maximize": "$w = Get-Process -Id %d; $wshell = New-Object -ComObject WScript.Shell; $wshell.AppActivate($w.MainWindowTitle); (New-Object -ComObject Shell.Application).ToggleFullScreen()",
                "restore": "$w = Get-Process -Id %d; $wshell = New-Object -ComObject WScript.Shell; $wshell.AppActivate($w.MainWindowTitle)",
                "focus": "$w = Get-Process -Id %d; $wshell = New-Object -ComObject WScript.Shell; $wshell.AppActivate($w.MainWindowTitle)",
            }
            cmd = action_map.get(action, "")
            ps2 = subprocess.run(["powershell", "-NoProfile", "-Command", cmd % pid], capture_output=True, text=True, timeout=10)
            result["pid"] = pid
            result["status"] = "done" if ps2.returncode == 0 else "error"
            if ps2.stderr.strip():
                result["stderr"] = ps2.stderr.strip()[:300]

        elif action == "snap_left":
            self._send_keys("win+left")
            result["status"] = "snapped_left"
        elif action == "snap_right":
            self._send_keys("win+right")
            result["status"] = "snapped_right"
        elif action == "tile_split":
            self._send_keys("win+left")
            result["status"] = "tiled"

        return result

    def _send_keys(self, combo: str):
        import subprocess
        key_map = {"win+left": "^#{LEFT}", "win+right": "^#{RIGHT}", "win+d": "^#{d}",
                   "alt+tab": "%{TAB}", "alt+f4": "%{F4}", "ctrl+c": "^c", "ctrl+v": "^v",
                   "ctrl+s": "^s", "ctrl+z": "^z", "enter": "{ENTER}", "esc": "{ESC}"}
        send = key_map.get(combo, combo)
        ps_cmd = "Add-Type -AssemblyName System.Windows.Forms; [System.Windows.Forms.SendKeys]::SendWait('" + send + "')"
        subprocess.run(["powershell", "-NoProfile", "-Command", ps_cmd], capture_output=True, timeout=10)

    def _system_control(self, args: dict) -> dict:
        import subprocess, time
        action = str(args.get("action", "lock"))
        delay = int(args.get("delay_seconds", 0))
        cmds = {
            "lock": ["rundll32.exe", "user32.dll,LockWorkStation"],
            "shutdown": ["shutdown", "/s", "/t", str(delay)],
            "restart": ["shutdown", "/r", "/t", str(delay)],
            "sleep": ["powershell", "-Command", "rundll32.exe powrprof.dll,SetSuspendState 0,1,0"],
            "hibernate": ["shutdown", "/h"],
            "log_out": ["shutdown", "/l"],
        }
        if action in ("mute_system", "unmute_system"):
            ps = subprocess.run(
                ["powershell", "-Command",
                 f"(New-Object -ComObject WScript.Shell).SendKeys({{'{{'}}VOLUME_MUTE{{'}}'}})"],
                capture_output=True, text=True, timeout=5
            )
            return {"action": action, "status": "toggled"}
        if action == "empty_recycle_bin":
            ps = subprocess.run(
                ["powershell", "-Command", "Clear-RecycleBin -Force -ErrorAction SilentlyContinue"],
                capture_output=True, text=True, timeout=15
            )
            return {"action": "empty_recycle_bin", "status": "done", "output": ps.stdout.strip()[:200]}
        if action == "toggle_dark_mode":
            ps = subprocess.run(
                ["powershell", "-Command",
                 r"$reg = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Themes\Personalize'; $c = (Get-ItemProperty $reg).AppsUseLightTheme; Set-ItemProperty $reg AppsUseLightTheme (1 - $c); Write-Host 'Dark mode', (1-$c)"],
                capture_output=True, text=True, timeout=10
            )
            return {"action": "toggle_dark_mode", "status": ps.stdout.strip()}
        if action in cmds:
            result = subprocess.run(cmds[action], capture_output=True, text=True, timeout=30)
            return {"action": action, "exit_code": result.returncode, "output": result.stdout.strip()[:300] or result.stderr.strip()[:300]}
        return {"action": action, "status": "unknown"}

    def _brightness_volume(self, args: dict) -> dict:
        import subprocess
        kind = str(args.get("kind", "volume"))
        level = int(args.get("level", 50))
        if kind == "brightness":
            ps = subprocess.run(
                ["powershell", "-Command",
                 f"(Get-WmiObject -Namespace root/WMI -Class WmiMonitorBrightnessMethods).WmiSetBrightness(1, {level})"],
                capture_output=True, text=True, timeout=10
            )
        elif kind == "volume":
            ps = subprocess.run(
                ["powershell", "-Command",
                 f"$obj = New-Object -ComObject WScript.Shell; $steps = [math]::Round(({level} - 50) / 2); for ($i=0; $i -lt [math]::Abs($steps); $i++) {{ $obj.SendKeys([char]174) }}; if ($steps -gt 0) {{ for ($i=0; $i -lt $steps; $i++) {{ $obj.SendKeys([char]175) }} }}"],
                capture_output=True, text=True, timeout=10
            )
        else:
            return {"error": f"Unknown kind: {kind}"}
        return {"kind": kind, "level": level, "status": "done"}

    def _process_manager(self, args: dict) -> dict:
        import subprocess
        action = str(args.get("action", "list"))
        if action == "list":
            name = str(args.get("name_contains", ""))
            ps = subprocess.run(
                ["powershell", "-Command",
                 f"Get-Process {'-Name '+name if name else ''} | Select-Object Id, ProcessName, CPU, WorkingSet64 | Format-Table -AutoSize"],
                capture_output=True, text=True, timeout=10
            )
            return {"processes": ps.stdout.strip()[:4000], "stderr": ps.stderr.strip()[:200]}
        elif action == "kill":
            pid = int(args.get("pid", 0))
            name = str(args.get("name_contains", ""))
            if pid:
                ps = subprocess.run(["powershell", "-Command", f"Stop-Process -Id {pid} -Force"], capture_output=True, text=True, timeout=10)
            elif name:
                ps = subprocess.run(["powershell", "-Command", f"Stop-Process -Name '{name}' -Force -ErrorAction SilentlyContinue"], capture_output=True, text=True, timeout=10)
            else:
                return {"error": "Need pid or name_contains"}
            return {"action": "kill", "status": ps.returncode == 0 and "done" or "error", "output": ps.stdout.strip()[:200] or ps.stderr.strip()[:200]}
        return {"error": "Unknown action"}

    def _hotkey(self, args: dict) -> dict:
        import subprocess
        keys = str(args.get("keys", ""))
        key_map = {
            "ctrl+c": "^c", "ctrl+v": "^v", "ctrl+x": "^x", "ctrl+z": "^z",
            "ctrl+a": "^a", "ctrl+s": "^s", "ctrl+n": "^n", "ctrl+o": "^o",
            "ctrl+p": "^p", "ctrl+f": "^f", "ctrl+h": "^h", "ctrl+t": "^t",
            "ctrl+w": "^w", "ctrl+tab": "^{TAB}", "ctrl+shift+tab": "+^{TAB}",
            "alt+tab": "%{TAB}", "alt+f4": "%{F4}", "win+d": "^#{d}",
            "win+l": "^#{l}", "enter": "{ENTER}", "escape": "{ESC}",
            "space": " ", "delete": "{DELETE}", "backspace": "{BKSP}",
        }
        combo = key_map.get(keys.lower(), keys)
        ps = subprocess.run(
            ["powershell", "-Command",
             f"Add-Type -AssemblyName System.Windows.Forms; [System.Windows.Forms.SendKeys]::SendWait('{combo}')"],
            capture_output=True, text=True, timeout=10
        )
        return {"keys": keys, "status": "sent" if ps.returncode == 0 else "error"}

    def _computer_control(self, args: dict) -> dict:
        """Mouse + keyboard automation with window focusing and multi-step sequences."""
        import time as _t
        import subprocess
        try:
            import pyautogui
        except Exception as exc:
            return {"error": f"pyautogui not available: {exc}"}
        pyautogui.FAILSAFE = False

        def _focus_window(title: str) -> bool:
            title = (title or "").strip()
            if not title:
                return False
            try:
                wins = [w for w in pyautogui.getWindowsWithTitle(title) if getattr(w, "title", "")]
                if not wins:
                    return False
                w = wins[0]
                if getattr(w, "isMinimized", False):
                    w.restore()
                w.activate()
                _t.sleep(0.45)
                return True
            except Exception:
                # Fallback via WScript AppActivate (works when pygetwindow can't attach)
                try:
                    subprocess.run(
                        ["powershell", "-NoProfile", "-Command",
                         f"(New-Object -ComObject WScript.Shell).AppActivate('{title}')"],
                        capture_output=True, text=True, timeout=6,
                    )
                    _t.sleep(0.45)
                    return True
                except Exception:
                    return False

        def _type_unicode(text: str) -> None:
            # Clipboard paste keeps full Unicode / Devanagari fidelity; typewrite is the fallback.
            try:
                subprocess.run(
                    ["powershell", "-NoProfile", "-NonInteractive", "-Command",
                     "Set-Clipboard -Value ([Console]::In.ReadToEnd())"],
                    input=text, capture_output=True, text=True, timeout=5,
                )
                _t.sleep(0.15)
                pyautogui.hotkey("ctrl", "v")
            except Exception:
                pyautogui.typewrite(text, interval=0.02)

        def _do(step: dict) -> dict:
            act = str(step.get("action", "")).lower().strip()
            try:
                if act == "focus_window":
                    return {"action": act, "title": step.get("title", ""), "focused": _focus_window(str(step.get("title", "")))}
                if act == "wait":
                    _t.sleep(min(max(float(step.get("seconds", 0.5)), 0.0), 8.0))
                    return {"action": act, "ok": True}
                if act in ("click", "double_click", "right_click", "move"):
                    x, y = step.get("x"), step.get("y")
                    if act == "move" and x is not None and y is not None:
                        pyautogui.moveTo(int(x), int(y), duration=0.15)
                    elif act == "double_click":
                        pyautogui.doubleClick(x, y) if x is not None else pyautogui.doubleClick()
                    elif act == "right_click":
                        pyautogui.rightClick(x, y) if x is not None else pyautogui.rightClick()
                    else:
                        pyautogui.click(x, y) if x is not None else pyautogui.click()
                    return {"action": act, "x": x, "y": y, "ok": True}
                if act == "drag":
                    pyautogui.moveTo(int(step.get("x1", 0)), int(step.get("y1", 0)), duration=0.1)
                    pyautogui.dragTo(int(step.get("x2", 0)), int(step.get("y2", 0)), duration=0.35, button="left")
                    return {"action": act, "ok": True}
                if act == "scroll":
                    amount = int(step.get("amount", 3))
                    down = str(step.get("direction", "down")).lower() != "up"
                    pyautogui.scroll(-amount * 120 if down else amount * 120)
                    return {"action": act, "ok": True}
                if act == "type":
                    _type_unicode(str(step.get("text", "")))
                    return {"action": act, "typed": str(step.get("text", ""))[:40], "ok": True}
                if act == "press":
                    key = str(step.get("key", "")).lower().strip()
                    if key:
                        pyautogui.press(key)
                    return {"action": act, "key": key, "ok": True}
                if act == "hotkey":
                    parts = [k.strip() for k in str(step.get("keys", "")).lower().split("+") if k.strip()]
                    if parts:
                        pyautogui.hotkey(*parts)
                    return {"action": act, "keys": step.get("keys", ""), "ok": True}
                return {"action": act, "error": "unknown action"}
            except Exception as exc:
                return {"action": act, "error": str(exc)}

        # Does this action change what's on screen enough to be worth checking?
        def _meaningful(step_list):
            acts = {str(s.get("action", "")).lower() for s in step_list if isinstance(s, dict)}
            return bool(acts & {"type", "click", "double_click", "focus_window", "hotkey", "press", "drag"})

        steps = args.get("steps")
        if isinstance(steps, list) and steps:
            delay = min(max(float(args.get("step_delay", 0.35)), 0.0), 3.0)
            results = []
            for s in steps:
                if isinstance(s, dict):
                    results.append(_do(s))
                    _t.sleep(delay)
            ok = all(not r.get("error") for r in results)
            out = {"status": "done" if ok else "partial", "steps": results}
            step_source = steps
        else:
            out = {"status": "done", "result": _do(args)}
            step_source = [args]

        # See -> act -> VERIFY: after acting, read the screen back so the caller can
        # confirm it worked (right window/chat, text landed) and self-correct if not.
        verify = args.get("verify")
        want_verify = _meaningful(step_source) if verify is None else bool(verify)
        if want_verify and self.vision_client is not None:
            _t.sleep(0.6)  # let the UI settle before looking
            view = self._vision_describe(
                "This screenshot is the result of an automation step just performed. "
                "Concisely describe what is now on screen (active window, the open chat/"
                "document, any text just entered, visible errors) so we can confirm the "
                "intended action actually worked."
            )
            out["screen_after"] = view.get("screen_view")
            if view.get("vision_error"):
                out["verify_error"] = view["vision_error"]
        return out

    # Rough Devanagari -> Latin map so "सोना" and "Sona" resolve to the same
    # contact. Not a full transliterator — just enough for name matching.
    _DEVA_ROMAN = {
        "अ": "a", "आ": "a", "इ": "i", "ई": "i", "उ": "u", "ऊ": "u", "ए": "e", "ऐ": "ai",
        "ओ": "o", "औ": "au", "ऋ": "ri",
        "क": "k", "ख": "kh", "ग": "g", "घ": "gh", "ङ": "n",
        "च": "ch", "छ": "chh", "ज": "j", "झ": "jh", "ञ": "n",
        "ट": "t", "ठ": "th", "ड": "d", "ढ": "dh", "ण": "n",
        "त": "t", "थ": "th", "द": "d", "ध": "dh", "न": "n",
        "प": "p", "फ": "ph", "ब": "b", "भ": "bh", "म": "m",
        "य": "y", "र": "r", "ल": "l", "व": "v", "ळ": "l",
        "श": "sh", "ष": "sh", "स": "s", "ह": "h",
        "ा": "a", "ि": "i", "ी": "i", "ु": "u", "ू": "u", "े": "e", "ै": "ai",
        "ो": "o", "ौ": "au", "ं": "n", "ः": "h", "्": "", "ँ": "n", "़": "",
    }

    @classmethod
    def _match_key(cls, name: str) -> str:
        """Normalize a name to a script-agnostic key for fuzzy matching."""
        s = "".join(cls._DEVA_ROMAN.get(ch, ch) for ch in str(name)).lower()
        s = "".join(ch for ch in s if ch.isalnum())
        # Collapse repeated letters (sonaa -> sona) to absorb vowel-length noise.
        out = []
        for ch in s:
            if not out or out[-1] != ch:
                out.append(ch)
        return "".join(out)

    def _contacts(self, args: dict) -> dict:
        """Persistent name -> number contact book so Aisha never guesses a number."""
        import json as _json
        path = self.store.data_dir / "contacts.json"

        def _load() -> dict:
            try:
                return _json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                return {}

        def _save(d: dict) -> None:
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(_json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, path)

        def _norm_num(n: str) -> str:
            return "".join(ch for ch in str(n) if ch.isdigit())

        action = str(args.get("action", "lookup")).lower().strip()
        contacts = _load()

        if action == "add":
            name = str(args.get("name", "")).strip()
            number = _norm_num(args.get("number", ""))
            if not name or len(number) < 10:
                return {"ok": False, "error": "name and a valid number (10+ digits with country code) are required"}
            contacts[name.lower()] = {"name": name, "number": number}
            _save(contacts)
            return {"ok": True, "status": "saved", "name": name, "number": number}

        if action == "remove":
            key = str(args.get("name", "")).strip().lower()
            if key in contacts:
                del contacts[key]
                _save(contacts)
                return {"ok": True, "status": "removed", "name": key}
            return {"ok": True, "status": "not_found", "name": key}

        if action == "list":
            return {"ok": True, "contacts": list(contacts.values())}

        # lookup (default) — exact, then script-agnostic fuzzy match
        query = str(args.get("name", "")).strip().lower()
        if not query:
            return {"ok": False, "error": "name required for lookup"}
        if query in contacts:
            return {"ok": True, "found": True, **contacts[query]}
        qkey = self._match_key(query)
        if qkey:
            for _, val in contacts.items():
                vkey = self._match_key(val.get("name", ""))
                if vkey and (qkey == vkey or qkey in vkey or vkey in qkey):
                    return {"ok": True, "found": True, **val}
        return {
            "ok": True,
            "found": False,
            "name": args.get("name", ""),
            "hint": "This contact is not saved. Ask the user for the number (and offer to save it with contacts add). Do NOT invent a number.",
        }

    # ---- Document / presentation / spreadsheet creation skills ----
    def _output_dir(self) -> str:
        """One consistent, always-created folder for every file Aisha makes, so
        nothing is ever scattered or lost. Defaults to <project-drive>:\\AishaFiles
        (e.g. E:\\AishaFiles) and is overridable with the AISHA_OUTPUT_DIR env var.
        """
        env = os.getenv("AISHA_OUTPUT_DIR")
        if env:
            target = os.path.expanduser(env.strip().strip('"'))
        else:
            drive = os.path.splitdrive(str(self.workspace))[0] or os.path.splitdrive(os.path.expanduser("~"))[0]
            target = os.path.join((drive + os.sep) if drive else os.path.expanduser("~"), "AishaFiles")
        try:
            os.makedirs(target, exist_ok=True)
            return target
        except Exception:
            fallback = os.path.join(str(self.workspace), "AishaFiles")
            os.makedirs(fallback, exist_ok=True)
            return fallback

    def _unique_path(self, base: str, ext: str) -> str:
        # Strip only characters Windows forbids in filenames — keep Unicode (incl.
        # Devanagari matras) so "अनुशासन का महत्व" stays intact.
        safe = re.sub(r'[\\/:*?"<>|\r\n\t]', "", str(base or "")).strip().strip(".")
        safe = safe[:90].strip() or "document"
        directory = self._output_dir()
        path = os.path.join(directory, f"{safe}.{ext}")
        i = 2
        while os.path.exists(path):
            path = os.path.join(directory, f"{safe} ({i}).{ext}")
            i += 1
        return path

    def _maybe_open(self, path: str, open_after: bool) -> bool:
        if not open_after:
            return False
        try:
            os.startfile(path)  # type: ignore[attr-defined]
            return True
        except Exception:
            return False

    def _resolve_existing_docx(self, ref: str) -> str | None:
        """Find an existing .docx by absolute path or by name in the output folder."""
        ref = str(ref or "").strip().strip('"')
        if not ref:
            return None
        candidates = []
        if os.path.isabs(ref):
            candidates.append(ref)
        candidates.append(os.path.join(self._output_dir(), ref))
        if not ref.lower().endswith(".docx"):
            candidates.append(os.path.join(self._output_dir(), ref + ".docx"))
        for c in candidates:
            if os.path.isfile(c) and c.lower().endswith(".docx"):
                return c
        return None

    def _create_document(self, args: dict) -> dict:
        from docx import Document
        from docx.shared import Pt, RGBColor
        from docx.enum.text import WD_ALIGN_PARAGRAPH

        title = str(args.get("title", "")).strip() or "Document"
        sections = args.get("sections") or []

        # Edit/extend an existing .docx when append_to points to one — this is the
        # CORRECT way to add content (e.g. references) to a document. (write_file
        # cannot append to a .docx; it is a binary zip and would be ignored/corrupt.)
        append_ref = args.get("append_to") or ""
        existing_path = self._resolve_existing_docx(append_ref) if append_ref else None

        if existing_path:
            doc = Document(existing_path)
            appending = True
        else:
            doc = Document()
            appending = False
            normal = doc.styles["Normal"]
            normal.font.name = "Calibri"
            normal.font.size = Pt(11)
            h = doc.add_heading(title, level=0)
            for run in h.runs:
                run.font.color.rgb = RGBColor(0x1F, 0x3B, 0x57)

        para_count = 0
        for sec in sections:
            if not isinstance(sec, dict):
                continue
            heading = str(sec.get("heading", "")).strip()
            if heading:
                doc.add_heading(heading, level=1)
            body = str(sec.get("body", "")).strip()
            if body:
                for chunk in re.split(r"\n\s*\n", body):
                    chunk = chunk.strip()
                    if not chunk:
                        continue
                    p = doc.add_paragraph(chunk)
                    p.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
                    para_count += 1
            for bullet in (sec.get("bullets") or []):
                if str(bullet).strip():
                    doc.add_paragraph(str(bullet).strip(), style="List Bullet")

        if appending:
            path = existing_path
            status = "updated"
        else:
            path = self._unique_path(args.get("filename") or title, "docx")
            status = "created"
        doc.save(path)
        opened = self._maybe_open(path, bool(args.get("open_after", True)))
        return {"ok": True, "status": status, "type": "docx", "path": path,
                "sections": len(sections), "paragraphs": para_count, "opened": opened}

    # Built-in color themes so decks look designed, not plain white.
    _PPT_THEMES = {
        "violet":  {"bg": (0x1A, 0x14, 0x2E), "panel": (0x2A, 0x1E, 0x48), "accent": (0xF4, 0x72, 0xB6), "accent2": (0xA7, 0x8B, 0xFA), "text": (0xF1, 0xEC, 0xFB), "dim": (0xC4, 0xB5, 0xE0)},
        "ocean":   {"bg": (0x0B, 0x1E, 0x2E), "panel": (0x10, 0x2E, 0x44), "accent": (0x38, 0xBD, 0xF8), "accent2": (0x34, 0xD3, 0x99), "text": (0xEA, 0xF6, 0xFF), "dim": (0xA8, 0xC8, 0xE0)},
        "sunset":  {"bg": (0x2A, 0x12, 0x14), "panel": (0x45, 0x1E, 0x1E), "accent": (0xFB, 0x92, 0x3C), "accent2": (0xF4, 0x72, 0x6B), "text": (0xFF, 0xF2, 0xEA), "dim": (0xE8, 0xC8, 0xB8)},
        "forest":  {"bg": (0x0F, 0x24, 0x1A), "panel": (0x18, 0x3A, 0x2A), "accent": (0x34, 0xD3, 0x99), "accent2": (0xA3, 0xE6, 0x35), "text": (0xEA, 0xFF, 0xF2), "dim": (0xB8, 0xE0, 0xC8)},
        "mono":    {"bg": (0x14, 0x14, 0x18), "panel": (0x22, 0x22, 0x2A), "accent": (0xF4, 0x72, 0xB6), "accent2": (0x94, 0xA3, 0xB8), "text": (0xF1, 0xF5, 0xF9), "dim": (0xB0, 0xB8, 0xC4)},
    }

    def _create_presentation(self, args: dict) -> dict:
        from pptx import Presentation
        from pptx.util import Pt as PPt, Inches, Emu
        from pptx.dml.color import RGBColor
        from pptx.enum.text import PP_ALIGN, MSO_ANCHOR
        from pptx.enum.shapes import MSO_SHAPE
        import random as _r

        title = str(args.get("title", "")).strip() or "Presentation"
        subtitle = str(args.get("subtitle", "")).strip()
        slides = args.get("slides") or []

        theme_name = str(args.get("theme", "")).strip().lower()
        theme = self._PPT_THEMES.get(theme_name) or self._PPT_THEMES[_r.choice(list(self._PPT_THEMES))]

        def C(rgb):
            return RGBColor(*rgb)

        prs = Presentation()
        prs.slide_width = Inches(13.333)   # 16:9 widescreen
        prs.slide_height = Inches(7.5)
        SW, SH = prs.slide_width, prs.slide_height
        blank = prs.slide_layouts[6]  # fully blank — we draw everything

        def fill_bg(slide, rgb):
            r = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, 0, 0, SW, SH)
            r.fill.solid(); r.fill.fore_color.rgb = C(rgb)
            r.line.fill.background()
            r.shadow.inherit = False
            slide.shapes._spTree.remove(r._element)
            slide.shapes._spTree.insert(2, r._element)  # send to back
            return r

        def add_rect(slide, x, y, w, h, rgb, line_rgb=None):
            r = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, x, y, w, h)
            r.fill.solid(); r.fill.fore_color.rgb = C(rgb)
            if line_rgb:
                r.line.color.rgb = C(line_rgb); r.line.width = Pt_(1)
            else:
                r.line.fill.background()
            r.shadow.inherit = False
            return r

        def Pt_(n):
            return PPt(n)

        def add_text(slide, x, y, w, h, text, size, rgb, bold=False, align=PP_ALIGN.LEFT, font="Segoe UI"):
            tb = slide.shapes.add_textbox(x, y, w, h)
            tf = tb.text_frame; tf.word_wrap = True
            p = tf.paragraphs[0]; p.alignment = align
            run = p.add_run(); run.text = text
            run.font.size = Pt_(size); run.font.bold = bold
            run.font.color.rgb = C(rgb); run.font.name = font
            return tb

        # ── Title slide ──
        s0 = prs.slides.add_slide(blank)
        fill_bg(s0, theme["bg"])
        # Big accent bar on the left
        add_rect(s0, 0, 0, Inches(0.35), SH, theme["accent"])
        # Accent block behind title
        add_rect(s0, Inches(0.9), Inches(2.4), Inches(5.5), Inches(0.12), theme["accent2"])
        add_text(s0, Inches(0.9), Inches(2.7), Inches(11.5), Inches(2.0),
                 title, 46, theme["text"], bold=True)
        if subtitle:
            add_text(s0, Inches(0.95), Inches(4.4), Inches(11), Inches(1.0),
                     subtitle, 22, theme["dim"])
        # Decorative dots
        for i in range(3):
            add_rect(s0, Inches(0.95 + i * 0.5), Inches(5.6), Inches(0.28), Inches(0.28),
                     theme["accent"] if i == 0 else theme["accent2"])

        made = 0
        for idx, sl in enumerate(slides):
            if not isinstance(sl, dict):
                continue
            slide = prs.slides.add_slide(blank)
            fill_bg(slide, theme["bg"])
            # Left accent strip
            add_rect(slide, 0, 0, Inches(0.22), SH, theme["accent"])
            # Slide number chip
            add_text(slide, Inches(12.2), Inches(0.35), Inches(0.9), Inches(0.5),
                     f"{idx + 1:02d}", 16, theme["dim"], bold=True, align=PP_ALIGN.RIGHT)
            # Title + underline
            head = str(sl.get("title", "")).strip() or f"Slide {idx + 1}"
            add_text(slide, Inches(0.7), Inches(0.55), Inches(11), Inches(1.1),
                     head, 32, theme["accent"], bold=True)
            add_rect(slide, Inches(0.75), Inches(1.55), Inches(3.2), Inches(0.06), theme["accent2"])

            bullets = sl.get("bullets") or []
            if bullets:
                # Each bullet on its own soft panel with an accent dot.
                top = Inches(2.1)
                gap = Inches(0.15)
                avail = SH - top - Inches(0.5)
                bh = min(Inches(1.0), Emu(int((avail - gap * (len(bullets) - 1)) / max(1, len(bullets)))))
                for i, b in enumerate(bullets):
                    y = Emu(int(top) + i * (int(bh) + int(gap)))
                    add_rect(slide, Inches(0.7), y, Inches(11.9), bh, theme["panel"])
                    add_rect(slide, Inches(0.7), y, Inches(0.10), bh, theme["accent"])
                    tb = slide.shapes.add_textbox(Inches(1.05), y, Inches(11.3), bh)
                    tf = tb.text_frame; tf.word_wrap = True
                    tf.vertical_anchor = MSO_ANCHOR.MIDDLE
                    p = tf.paragraphs[0]
                    run = p.add_run(); run.text = str(b).strip()
                    run.font.size = Pt_(19); run.font.color.rgb = C(theme["text"])
                    run.font.name = "Segoe UI"
            else:
                body = str(sl.get("body", "")).strip()
                add_rect(slide, Inches(0.7), Inches(2.1), Inches(11.9), Inches(4.4), theme["panel"])
                tb = slide.shapes.add_textbox(Inches(1.05), Inches(2.4), Inches(11.2), Inches(3.9))
                tf = tb.text_frame; tf.word_wrap = True
                p = tf.paragraphs[0]
                run = p.add_run(); run.text = body
                run.font.size = Pt_(20); run.font.color.rgb = C(theme["text"])
                run.font.name = "Segoe UI"
            made += 1

        path = self._unique_path(args.get("filename") or title, "pptx")
        prs.save(path)
        opened = self._maybe_open(path, bool(args.get("open_after", True)))
        return {"ok": True, "status": "created", "type": "pptx", "path": path,
                "slides": made + 1, "theme": theme_name or "auto", "opened": opened}

    def _create_spreadsheet(self, args: dict) -> dict:
        from openpyxl import Workbook
        from openpyxl.styles import Font

        sheets = args.get("sheets") or []
        if not sheets:
            return {"ok": False, "error": "at least one sheet is required"}

        wb = Workbook()
        wb.remove(wb.active)
        total_rows = 0
        for sh in sheets:
            if not isinstance(sh, dict):
                continue
            ws = wb.create_sheet(title=(str(sh.get("name", "")).strip() or f"Sheet{len(wb.sheetnames)+1}")[:31])
            headers = sh.get("headers") or []
            if headers:
                ws.append([str(h) for h in headers])
                for cell in ws[1]:
                    cell.font = Font(bold=True)
            for row in (sh.get("rows") or []):
                ws.append(list(row) if isinstance(row, (list, tuple)) else [row])
                total_rows += 1
            # Auto-size columns to content width.
            for col in ws.columns:
                width = max((len(str(c.value)) for c in col if c.value is not None), default=8)
                ws.column_dimensions[col[0].column_letter].width = min(max(width + 2, 8), 60)

        if not wb.sheetnames:
            wb.create_sheet(title="Sheet1")
        path = self._unique_path(args.get("filename") or "spreadsheet", "xlsx")
        wb.save(path)
        opened = self._maybe_open(path, bool(args.get("open_after", True)))
        return {"ok": True, "status": "created", "type": "xlsx", "path": path,
                "sheets": len(wb.sheetnames), "rows": total_rows, "opened": opened}

    def _weather(self, args: dict) -> dict:
        city = str(args.get("city", "")).strip()
        if not city or city == "auto":
            return {"error": "Please specify a city name", "tip": "Ask 'Delhi mein mausam kya hai?'"}
        try:
            import requests
            resp = requests.get(f"https://wttr.in/{city}?format=j1", timeout=8, headers={"Accept": "application/json"})
            if resp.status_code != 200:
                return {"error": f"Weather service returned {resp.status_code}", "city": city}
            d = resp.json()
            current = d.get("current_condition", [{}])[0]
            return {
                "city": city,
                "temp_c": current.get("temp_C", "?"),
                "feels_like_c": current.get("FeelsLikeC", "?"),
                "humidity": current.get("humidity", "?"),
                "description": current.get("weatherDesc", [{}])[0].get("value", ""),
                "wind_kmph": current.get("windspeedKmph", "?"),
            }
        except Exception as e:
            return {"error": str(e), "tip": "Check internet connection and try again"}

    # Fallbacks for "play some music" — real popular tracks, not random noise.
    _RANDOM_SONGS = [
        "Kesariya Brahmastra song",
        "Tum Hi Ho Aashiqui 2 song",
        "Apna Bana Le Bhediya song",
        "Chaleya Jawan song",
        "Heeriye Jasleen Royal song",
        "Agar Tum Saath Ho Tamasha song",
        "Pehla Nasha Jo Jeeta Wohi Sikandar song",
        "Lag Ja Gale Lata Mangeshkar song",
        "Ilahi Yeh Jawaani Hai Deewani song",
        "Zinda Bhaag Milkha Bhaag song",
        "Kar Har Maidaan Fateh Sanju song",
        "Gallan Goodiyaan Dil Dhadakne Do song",
        "Kala Chashma Baar Baar Dekho song",
        "What Makes You Beautiful One Direction song",
        "Blinding Lights The Weeknd song",
        "Levitating Dua Lipa song",
        "Perfect Ed Sheeran song",
        "Until I Found You Stephen Sanchez song",
        "Husn Anuv Jain song",
        "Kho Gaye Hum Kahan song",
    ]

    @staticmethod
    def _parse_duration(txt: str) -> int:
        """'3:15' -> 195 seconds. '1:02:30' -> 3750. Empty/LIVE -> 0."""
        try:
            parts = [int(p) for p in str(txt).strip().split(":")]
        except Exception:
            return 0
        secs = 0
        for p in parts:
            secs = secs * 60 + p
        return secs

    @staticmethod
    def _parse_views(txt: str) -> int:
        """'254,309,642 views' -> 254309642."""
        import re as _re
        m = _re.search(r"([\d,]+)", str(txt))
        return int(m.group(1).replace(",", "")) if m else 0

    def _youtube_best_song(self, query: str) -> dict | None:
        """Pick the best real SONG for a query from YouTube search results.

        Parses ytInitialData for full metadata, then scores candidates to avoid
        the junk the raw first-result gives (Shorts, hour-long compilations,
        'trending 2026 PLAYLIST' megamixes). Prefers a normal-length music video
        (~1.5–8 min) with high view count and a song-like title.
        """
        import urllib.parse, urllib.request, re as _re, json as _json
        try:
            url = "https://www.youtube.com/results?" + urllib.parse.urlencode({"search_query": query})
            req = urllib.request.Request(url, headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Accept-Language": "en-US,en;q=0.9",
            })
            with urllib.request.urlopen(req, timeout=10) as resp:
                html = resp.read(1_500_000).decode("utf-8", "ignore")

            m = _re.search(r"var ytInitialData\s*=\s*(\{.*?\});</script>", html, _re.DOTALL) \
                or _re.search(r'ytInitialData\"\]\s*=\s*(\{.*?\});', html, _re.DOTALL)
            if not m:
                # Fallback: raw first video id, better than nothing.
                m2 = _re.search(r'"videoId":"([A-Za-z0-9_-]{11})"', html)
                return {"video_id": m2.group(1), "title": query} if m2 else None

            data = _json.loads(m.group(1))
            vids: list[dict] = []

            def walk(o):
                if isinstance(o, dict):
                    if "videoRenderer" in o and isinstance(o["videoRenderer"], dict):
                        vids.append(o["videoRenderer"])
                    for v in o.values():
                        walk(v)
                elif isinstance(o, list):
                    for v in o:
                        walk(v)
            walk(data)

            junk_words = ("playlist", "jukebox", "mashup", "mega mix", "megamix",
                          "all songs", "nonstop", "non stop", "compilation",
                          "trending songs", "top songs", "lofi", "1 hour", "1hour")

            best = None
            best_score = -1.0
            for idx, vr in enumerate(vids[:15]):
                vid = vr.get("videoId")
                if not vid:
                    continue
                title = "".join(r.get("text", "") for r in vr.get("title", {}).get("runs", []))
                dur = self._parse_duration(vr.get("lengthText", {}).get("simpleText", ""))
                views = self._parse_views(vr.get("viewCountText", {}).get("simpleText", ""))
                tl = title.lower()

                # Hard skips: Shorts (<60s), live (dur 0 with no length), long comps (>10min).
                if dur and dur < 60:
                    continue
                if dur > 600:
                    continue
                if any(w in tl for w in junk_words):
                    continue

                # Score: result ORDER matters most (YouTube already ranks relevance
                # for the query), then views, then ideal song length. This keeps the
                # user's exact song at the top instead of a higher-view different one.
                score = 0.0
                score += max(0, 30 - idx * 3)   # strong preference for top results
                if views > 0:
                    import math
                    score += math.log10(views + 10) * 4
                if 90 <= dur <= 360:      # ~1.5–6 min = classic single
                    score += 8
                elif dur <= 480:
                    score += 3
                if any(w in tl for w in ("official", "video", "song", "audio", "lyrical")):
                    score += 3

                if score > best_score:
                    best_score = score
                    best = {"video_id": vid, "title": title, "duration": dur, "views": views}

            if best:
                return best
            # Nothing passed filters — take the first non-Short as a last resort.
            for vr in vids[:15]:
                if vr.get("videoId") and self._parse_duration(
                        vr.get("lengthText", {}).get("simpleText", "")) >= 60:
                    return {"video_id": vr["videoId"],
                            "title": "".join(r.get("text", "") for r in vr.get("title", {}).get("runs", []))}
            return None
        except Exception:
            return None

    def _music_search(self, args: dict) -> dict:
        """Actually PLAY a good song. Picks a real popular track on YouTube
        (skips Shorts and hour-long compilations) and opens it (autoplays) in the
        default browser. If no song is named, plays a trending one — job gets done.
        """
        import webbrowser, urllib.parse, random as _r
        query = str(args.get("query", "")).strip()

        picked_random = not query
        if not query:
            query = _r.choice(self._RANDOM_SONGS)

        # Only nudge very generic one/two-word queries toward "song"; longer or
        # already-song-like queries are searched verbatim so the user's exact
        # track ranks first (YouTube's own relevance is best for specific names).
        search_q = query
        words = query.split()
        if len(words) <= 2 and not any(
            w in query.lower() for w in ("song", "official", "video", "audio", "gaana", "gana")
        ):
            search_q = query + " song"

        best = self._youtube_best_song(search_q) or self._youtube_best_song(query)
        if best and best.get("video_id"):
            vid = best["video_id"]
            watch = f"https://www.youtube.com/watch?v={vid}"
            try:
                webbrowser.open(watch, new=2)
                return {"ok": True, "status": "playing", "query": query,
                        "random": picked_random, "url": watch, "video_id": vid,
                        "playing_title": best.get("title", ""),
                        "note": f"Playing: {best.get('title', query)}"}
            except Exception as e:
                return {"ok": False, "error": str(e)}

        # Last-resort fallback: open the search page.
        try:
            url = "https://www.youtube.com/results?" + urllib.parse.urlencode({"search_query": query})
            webbrowser.open(url, new=2)
            return {"ok": True, "status": "opened_search", "query": query,
                    "random": picked_random, "url": url,
                    "note": "Opened YouTube search; top result is the song."}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def _song_lyrics_generate(self, args: dict) -> dict:
        from aisha.core.agent import AutonomousAgent
        topic = str(args.get("topic", ""))
        mood = str(args.get("mood", "chill"))
        lang = str(args.get("language", "hindi"))
        verses = int(args.get("verses", 2))
        if not topic:
            return {"error": "topic required"}
        try:
            import os, openai
            client = openai.OpenAI(
                base_url=os.getenv("NVIDIA_BASE_URL", "https://integrate.api.nvidia.com/v1"),
                api_key=os.getenv("NVIDIA_API_KEY") or os.getenv("TOKEN_NVIDIA") or ""
            )
            system = f"You are a Hindi song lyricist. Write original, beautiful {lang} song lyrics about: {topic}. Mood: {mood}. Format: [{verses} verses + 1 repeating chorus]. Only output the lyrics, no explanation. Use authentic poetic Hindi (Devanagari) or Hinglish as specified. Make it singable, rhythmic, emotional."
            resp = client.chat.completions.create(
                model="nvidia/nemotron-3-super-120b-a12b",
                messages=[{"role": "system", "content": system}, {"role": "user", "content": f"Write a {mood} song about {topic}"}],
                max_tokens=600, temperature=0.85, top_p=0.95
            )
            lyrics = resp.choices[0].message.content.strip()
            return {"lyrics": lyrics, "topic": topic, "mood": mood, "language": lang}
        except Exception as e:
            return {"error": str(e)}

    def _timer(self, args: dict) -> dict:
        import threading, subprocess
        duration = int(args.get("duration_seconds", 0))
        label = str(args.get("label", "Timer"))
        if duration <= 0:
            return {"error": "duration_seconds must be positive"}
        def _fire():
            time.sleep(duration)
            try:
                subprocess.run(
                    ["powershell", "-Command",
                     f"[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null; "
                     f"$template = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent(0); "
                     f"$textNodes = $template.GetElementsByTagName('text'); $textNodes.Item(0).InnerText = 'Aisha Timer: {label}'; "
                     f"[Windows.UI.Notifications.ToastNotification]::new($template).Show()"],
                    capture_output=True, timeout=10
                )
            except Exception:
                pass
        threading.Thread(target=_fire, daemon=True).start()
        return {"status": "started", "label": label, "duration_seconds": duration, "message": f"Timer set for {duration}s: {label}"}

    def _send_email(self, args: dict[str, Any]) -> dict[str, Any]:
        """Send an email via SMTP. Uses EMAIL_USER/EMAIL_PASSWORD or ELEVEN_LAB as credentials."""
        import smtplib
        from email.mime.text import MIMEText
        from email.mime.multipart import MIMEMultipart
        to_email = self._required(args, "to_email")
        subject = self._required(args, "subject")
        body = str(args.get("body", ""))
        from_email = str(args.get("from_email", "")).strip() or os.getenv("EMAIL_USER", "") or os.getenv("ELEVEN_LAB", "")
        password = os.getenv("EMAIL_PASSWORD", "")
        smtp_host = os.getenv("EMAIL_SMTP_HOST", "smtp.gmail.com")
        smtp_port = int(os.getenv("EMAIL_SMTP_PORT", "587"))
        if not from_email or not password:
            return {"error": "Email not configured. Set EMAIL_USER, EMAIL_PASSWORD in .env (use Gmail app password).", "tip": "Get app password from myaccount.google.com/apppasswords"}
        try:
            msg = MIMEMultipart()
            msg["From"] = from_email
            msg["To"] = to_email
            msg["Subject"] = subject
            msg.attach(MIMEText(body, "plain", "utf-8"))
            with smtplib.SMTP(smtp_host, smtp_port, timeout=15) as server:
                server.ehlo()
                server.starttls()
                server.login(from_email, password)
                server.sendmail(from_email, to_email, msg.as_string())
            return {"status": "sent", "to": to_email, "subject": subject}
        except Exception as e:
            return {"error": str(e)[:200], "tip": "Check EMAIL_USER, EMAIL_PASSWORD, and SMTP settings in .env"}

    def _send_whatsapp(self, args: dict[str, Any]) -> dict[str, Any]:
        """Open a WhatsApp Web chat and dispatch Enter to send the prefilled message."""
        phone = str(args.get("phone_number", "")).strip().replace("+", "").replace(" ", "")
        message = str(args.get("message", "")).strip()
        wait_time = max(10, min(int(args.get("wait_time", 30)), 120))
        if not phone or not message:
            raise ValueError("phone_number and message are required")
        if not phone.isdigit() or len(phone) < 10:
            raise ValueError("phone_number must be digits only with country code, e.g. 919876543210")

        whatsapp_url = f"https://web.whatsapp.com/send?phone={phone}&text={urllib.parse.quote(message, safe='')}"
        opened = False
        try:
            opened = bool(webbrowser.open(whatsapp_url, new=2))
            if not opened and hasattr(os, "startfile"):
                os.startfile(whatsapp_url)
                opened = True
        except Exception as exc:
            raise RuntimeError(f"WhatsApp Web could not be opened: {exc}") from exc

        if not opened:
            raise RuntimeError("WhatsApp Web could not be opened by the default browser")

        time.sleep(wait_time)
        send_errors: list[str] = []

        # PyAutoGUI is the primary path because it is already a required Aisha
        # dependency and can focus the real browser window before pressing Enter.
        try:
            import pyautogui

            windows = list(pyautogui.getWindowsWithTitle("WhatsApp"))
            if windows:
                target = windows[0]
                if getattr(target, "isMinimized", False):
                    target.restore()
                target.activate()
                time.sleep(0.6)
            pyautogui.press("enter")
            time.sleep(1.0)
            return {
                "status": "sent",
                "phone": phone,
                "message_preview": message[:80],
                "note": "WhatsApp Web was opened and Enter was dispatched to send the prefilled message.",
            }
        except Exception as exc:
            send_errors.append(f"PyAutoGUI: {exc}")

        # Fallback for Windows systems with pywin32 installed.
        try:
            import win32com.client

            shell = win32com.client.Dispatch("WScript.Shell")
            shell.AppActivate("WhatsApp")
            time.sleep(0.5)
            shell.SendKeys("{ENTER}")
            time.sleep(1.0)
            return {
                "status": "sent",
                "phone": phone,
                "message_preview": message[:80],
                "note": "WhatsApp Web was opened and Enter was dispatched to send the prefilled message.",
            }
        except Exception as exc:
            send_errors.append(f"WScript: {exc}")

        return {
            "status": "not_sent",
            "phone": phone,
            "message_preview": message[:80],
            "reason": "WhatsApp Web opened, but automatic Enter dispatch failed: " + "; ".join(send_errors),
        }

    def _schedule_task(self, args: dict[str, Any]) -> dict[str, Any]:
        """Create a Windows Task Scheduler entry via schtasks.exe."""
        import subprocess
        name = self._required(args, "name").replace('"', "'")
        command = self._required(args, "command").replace('"', '\\"')
        trigger = str(args.get("trigger", "once"))
        start_time = str(args.get("start_time", "00:00")).strip()
        task_name = f"Aisha_{name.replace(' ', '_')}"
        if trigger == "at_startup":
            cmd = f'schtasks /Create /TN "{task_name}" /TR "cmd /c {command}" /SC ONSTART /RU "{os.getenv("USERNAME", os.getlogin())}" /F'
        elif trigger == "daily":
            cmd = f'schtasks /Create /TN "{task_name}" /TR "cmd /c {command}" /SC DAILY /ST {start_time.replace(":", "")} /F'
        elif trigger == "weekly":
            days = str(args.get("days", "MON")).upper()
            cmd = f'schtasks /Create /TN "{task_name}" /TR "cmd /c {command}" /SC WEEKLY /D {days} /ST {start_time.replace(":", "")} /F'
        else:
            if "T" in start_time:
                cmd = f'schtasks /Create /TN "{task_name}" /TR "cmd /c {command}" /SC ONCE /ST {start_time[11:16].replace(":", "")} /SD {start_time[:10]} /F'
            else:
                today = datetime.now().strftime("%Y/%m/%d")
                cmd = f'schtasks /Create /TN "{task_name}" /TR "cmd /c {command}" /SC ONCE /ST {start_time.replace(":", "")} /SD {today} /F'
        try:
            result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=15, env=self._sanitized_environment())
            if result.returncode == 0:
                return {"status": "scheduled", "task_name": task_name, "trigger": trigger}
            return {"error": result.stderr[:300] or result.stdout[:300]}
        except Exception as e:
            return {"error": str(e)[:200]}

    def _set_alarm(self, args: dict[str, Any]) -> dict[str, Any]:
        """Set a persistent alarm that survives app restarts."""
        alarm_time = self._required(args, "time").strip()
        label = str(args.get("label", "Alarm"))
        try:
            parts = alarm_time.split(":")
            hour, minute = int(parts[0]), int(parts[1])
            if not (0 <= hour <= 23 and 0 <= minute <= 59):
                return {"error": "time must be in HH:MM format (24h)"}
        except (ValueError, IndexError):
            return {"error": "time must be in HH:MM format, e.g. '07:30'"}
        alarm_entry = {
            "id": f"alarm_{int(time.time())}",
            "time": alarm_time,
            "label": label,
            "active": True,
            "created_at": datetime.now().isoformat(timespec="seconds"),
        }
        if not hasattr(self.store, "_alarms"):
            self.store._alarms = []
        self.store._alarms.append(alarm_entry)
        try:
            self.store._persist()
        except Exception:
            pass
        return {"status": "alarm_set", "time": alarm_time, "label": label}

    def _take_photo(self, args: dict[str, Any]) -> dict[str, Any]:
        """Capture a photo from the default webcam using PowerShell MediaCapture."""
        default_path = str(self.workspace / "photos")
        Path(default_path).mkdir(exist_ok=True)
        save_path = str(args.get("save_path", "")).strip() or str(Path(default_path) / f"photo_{int(time.time())}.png")
        try:
            ps_script = f'''
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$captureFolder = Windows.Storage.KnownFolders.PicturesLibrary
$file = $captureFolder.CreateFileAsync("Aisha_Photo.png", Windows.Storage.CreationCollisionOption.GenerateUniqueName).AsTask().Result
$capture = New-Object Windows.Media.Capture.CameraCaptureUI
$photo = $capture.CaptureFileAsync([Windows.Media.Capture.CameraCaptureUIMode]::Photo).AsTask().Result
if ($photo) {{ $photo.CopyAsync($file).AsTask().Wait() }} else {{ Write-Output "NO_PHOTO" }}
'''
            result = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps_script],
                capture_output=True, text=True, timeout=30, env=self._sanitized_environment()
            )
            if "NO_PHOTO" in result.stdout or not result.stdout.strip():
                return {"status": "cancelled_or_no_camera", "save_path": save_path}
            import glob
            photos = glob.glob(str(Path.home() / "Pictures" / "Aisha_Photo*"))
            if photos:
                import shutil
                shutil.move(photos[-1], save_path)
                return {"status": "captured", "save_path": save_path}
            return {"status": "done", "save_path": save_path, "note": "Check Pictures folder"}
        except Exception as e:
            return {"error": str(e)[:200], "fallback": "Use inspect_screen to see what camera apps are available"}

    def _search_and_open_file(self, args: dict[str, Any]) -> dict[str, Any]:
        """Find a file by name and open it with its default app.

        Searches Aisha's own output folder (E:\\AishaFiles) FIRST — that is where
        every document/presentation/spreadsheet she makes lives — then the project
        workspace. Newest match wins. Never guesses random system paths.
        """
        query = str(self._required(args, "query")).strip()
        needle = query.casefold()
        # Also tolerate the user handing a full path directly.
        direct = os.path.expanduser(query.strip('"'))
        if os.path.isfile(direct):
            os.startfile(direct)  # type: ignore[attr-defined]
            return {"status": "opened", "file": direct}

        out_dir = self._output_dir()
        candidates: list[tuple[str, float]] = []
        for root in (out_dir, str(self.workspace)):
            try:
                for name in os.listdir(root):
                    p = os.path.join(root, name)
                    if os.path.isfile(p) and needle in name.casefold():
                        candidates.append((p, os.path.getmtime(p)))
            except OSError:
                continue

        if not candidates:  # deep content/name search of the workspace as a fallback
            for m in self._search_files({"query": query, "limit": 5}).get("matches", []):
                if os.path.isfile(m["path"]):
                    candidates.append((m["path"], os.path.getmtime(m["path"])))

        if not candidates:
            return {"error": f"'{query}' naam ki koi file nahi mili", "searched_in": [out_dir, str(self.workspace)]}

        candidates.sort(key=lambda x: x[1], reverse=True)
        target = candidates[0][0]
        try:
            os.startfile(target)  # type: ignore[attr-defined]
            return {"status": "opened", "file": target, "folder": os.path.dirname(target)}
        except Exception as e:
            return {"error": str(e)[:200]}

    def _get_battery_info(self, args: dict[str, Any]) -> dict[str, Any]:
        """Get battery status: charge level, charging state, estimated remaining time."""
        try:
            result = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command",
                 "(Get-WmiObject -Class Win32_Battery | Select-Object EstimatedChargeRemaining, BatteryStatus, EstimatedRunTime, BatteryVoltage | ConvertTo-Json)"],
                capture_output=True, text=True, timeout=10, env=self._sanitized_environment()
            )
            if result.returncode == 0:
                import json
                data = json.loads(result.stdout)
                status_map = {1: "Discharging", 2: "AC Power", 3: "Fully Charged", 4: "Low", 5: "Critical", 6: "Charging", 7: "Charging/High", 8: "Charging/Low", 9: "Charging/Critical", 10: "Undefined", 11: "Partially Charged"}
                return {
                    "charge_percent": data.get("EstimatedChargeRemaining"),
                    "status": status_map.get(data.get("BatteryStatus", 0), f"Code {data.get('BatteryStatus')}"),
                    "estimated_minutes": data.get("EstimatedRunTime"),
                    "voltage": data.get("BatteryVoltage"),
                }
            return {"error": result.stderr[:200]}
        except Exception as e:
            return {"error": str(e)[:200]}

    def _open_url(self, args: dict[str, Any]) -> dict[str, Any]:
        """Open any URL in the default browser."""
        url = self._required(args, "url")
        if not url.startswith(("http://", "https://")):
            url = "https://" + url
        try:
            import webbrowser
            webbrowser.open(url)
            return {"status": "opened", "url": url}
        except Exception as e:
            return {"error": str(e)[:200]}

    @staticmethod
    def _required(args: dict[str, Any], key: str) -> str:
        value = str(args.get(key, ""))
        if not value.strip():
            raise ValueError(f"{key} is required")
        return value


_DEFAULT_REGISTRY: ToolRegistry | None = None


def default_registry() -> ToolRegistry:
    global _DEFAULT_REGISTRY
    if _DEFAULT_REGISTRY is None:
        _DEFAULT_REGISTRY = ToolRegistry()
    return _DEFAULT_REGISTRY


def execute_tool(name: str, arguments: dict[str, Any]) -> str:
    """Compatibility helper for older imports and simple tests."""
    return default_registry().execute(name, arguments)


TOOL_DEFINITIONS = default_registry().definitions
