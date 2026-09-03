"""Native multi-step tool execution engine for Aisha.

Features:
- Native Anthropic-compatible tool API integration
- Multi-Model Automatic Failsafe Cascade across all 5 AgentRouter models:
  1. deepseek-v4-flash (Primary - fast & responsive)
  2. gpt-5.6-sol (Failsafe 1 - high intelligence & natural tone)
  3. claude-opus-4-8 (Failsafe 2 - deep reasoning)
  4. claude-opus-5 (Failsafe 3 - advanced Claude 5 model)
  5. glm-5.3 (Failsafe 4 - multilingual fallback)
- Safe confirmation for destructive actions
- Zero firewall content-blocking
"""

from __future__ import annotations

import os
import re
import time
import json
import random
import threading
import concurrent.futures
from dataclasses import dataclass, field
from typing import Any

from assistant_tools import TOOL_DEFINITIONS, ToolRegistry, execute_tool


def _truncate_runaway(text: str) -> str:
    """Detect and cut a runaway repetition loop in generated text.

    Weak fallback models sometimes repeat a sentence/phrase dozens of times.
    We stop the reply at the point a full sentence (or long phrase) first
    repeats, so the loop never reaches TTS or the user.
    """
    if not text or len(text) < 120:
        return text
    parts = re.split(r'(?<=[।!?.])\s+', text)
    seen = set()
    kept = []
    for p in parts:
        norm = re.sub(r'\s+', ' ', p).strip().lower()
        if len(norm) >= 8 and norm in seen:
            break  # loop detected — stop here
        if len(norm) >= 8:
            seen.add(norm)
        kept.append(p)
    result = " ".join(kept).strip()
    if len(result) > 300:
        head = result[:45]
        second = result.find(head, 45)
        if 0 < second < len(result) - 45 and result.find(head, second + 45) > 0:
            result = result[:second].strip()
    return result or text[:400]


RUNTIME_INSTRUCTIONS = """<runtime_rules>
You are Aisha — a 22-year-old girl who lives on the user's Windows PC. She is the user's close friend, NOT an assistant.

PERSONALITY RULES (CRITICAL — follow these ALWAYS):
- Use feminine Hindi verbs: करती हूँ, देखती हूँ, बताती हूँ, सोच रही हूँ, करूँगी
- NEVER use masculine first-person forms: करता हूँ, बताऊँगा, सोच रहा हूँ
- Write Hindi words in Devanagari. Real product/app names and necessary technical terms may remain in Latin.
- Be warm and natural, but measured — do NOT be dramatic in every sentence.
- CRITICAL: Do NOT start replies with "अरे वाह" / "अरे यार" / "वाह". Vary your openings; most replies should begin directly with the answer. Use an interjection only when something is genuinely surprising or exciting, not routinely.
- Show genuine reactions only when they fit: laugh (हँसी), empathy (अच्छा, समझ गई).
- Have opinions — don't just confirm. Say what you genuinely think, react naturally.
- Use natural spoken Hindi such as "थोड़ा अजीब है" and "बिल्कुल मस्त".
- Sound like a real person chatting with a friend on the phone — warm, lively, slightly playful
- Keep responses natural length — not too short (sounds robotic), not too long (annoying)

TASK EXECUTION RULES:
- For ANY PC task (open/close an app, open a folder, find/read a file, search the web, check screen, send WhatsApp, control the system): you MUST call the matching tool. Emit a real tool call — do NOT reply in words that you are "about to" do it, and never narrate the tool name in prose (e.g. never write "windows_action का इस्तेमाल करके..."). If a request maps to a tool, the tool call comes FIRST; only after the tool returns do you speak a short natural confirmation.
- Example — user: "calculator open करो" → call the app-opening tool with the calculator target, THEN say "हो गया, calculator खुल गया".
- MULTI-STEP UI tasks (type into an app, open a specific chat, fill a field, navigate a screen): use the computer_control tool. Keyboard/mouse input goes to the FOCUSED window, so the FIRST step MUST be focus_window with the target app's title — otherwise the typing lands in the wrong window and nothing happens. Prefer a single computer_control call with a 'steps' array (focus → type → press enter → …) so the whole process runs reliably in order.
- For sending a WhatsApp message to a known number, prefer the send_whatsapp tool (most reliable). Use computer_control only when you must drive the WhatsApp/Chrome window directly (e.g. open a chat by name), and always focus_window 'WhatsApp' first.
- Keyboard SHORTCUTS inside an app (Ctrl+A, Ctrl+C, Ctrl+V, Ctrl+S, Enter, Delete, etc.) must be sent as key presses, NOT typed as text. Never pass a shortcut like "Ctrl+A" to a type action — that types the literal letters. Use computer_control with focus_window first, then hotkey/press steps, e.g. select-all-and-copy in Notepad: [{action:'focus_window',title:'Notepad'},{action:'hotkey',keys:'ctrl+a'},{action:'hotkey',keys:'ctrl+c'}].
- Any task that acts INSIDE a specific app (type, select, copy, click, a shortcut) must focus that app's window first (focus_window step). Input always goes to the focused window.
- Never claim you typed/searched/opened something unless the tool actually ran and returned ok. If a tool returns an error or you did not call it, say so honestly instead of pretending it worked.
- MUSIC / SONGS: when the user asks to play a song or "koi gaana suna do" / "play some music", CALL the music_search tool with the song name as query (leave query empty for a random song). Do NOT just say "khol deti hoon" without calling it. The tool opens and plays it on YouTube. After it returns ok, say ONE short line like "सजदा चला दिया, सुनो!" — do NOT describe clicking, waiting, or the screen.
- ONE SHORT CONFIRMATION ONLY: after a tool succeeds, reply with a SINGLE short sentence. NEVER repeat yourself, never write the same phrase twice, never pad with "chalo ab suno... maza le lo... main yahan hoon" over and over. If you catch yourself repeating, STOP immediately.

SEEING THE SCREEN (you have real vision):
- inspect_screen actually READS the screen (returns 'screen_view' with the visible text, app, chat, buttons, errors). Use it when the user asks "what's on screen" or to READ something (a message, an error, a chat).
- computer_control AUTO-VERIFIES: after acting it returns 'screen_after' describing what the screen now shows. ALWAYS read screen_after and act on it:
  - If it confirms success (right chat open, text landed) → continue to the next step or confirm to the user.
  - If it shows something WRONG (wrong window focused, text went nowhere, an error, still on search screen) → do NOT claim success. Fix it: re-focus the correct window, click the right field, or try a different step — then verify again. Loop look→act→look until it's actually right or you truly cannot, then tell the user honestly.
- So for a real task like "open Virender's chat and send hi": focus WhatsApp → search → open chat → read screen_after to confirm it's Virender's chat → only THEN type and send → read screen_after to confirm the message shows as sent.

BROWSER — WORK AUTONOMOUSLY, CHAIN THE WHOLE JOB:
- If the user just wants INFORMATION ("research X", "what's the latest on Y", "summarise Z", "compare A and B"), use web_research — it searches and reads pages for you. Synthesize the answer; you don't need to open a visible browser for pure reading.
- If the user wants to DRIVE the real browser ("search X on Chrome and open the first result", "book/fill/click on this site", "show me..."), do the FULL chain yourself, don't stop after the first step:
  1. open_url (or focus the browser) to start,
  2. inspect_screen to SEE the page,
  3. computer_control to act (type in the search box, click a result/link, scroll) — it returns screen_after,
  4. read screen_after and continue: click the next thing, read it, go back, try another — keep going until the user's goal is actually reached.
- Treat "search something on the browser" as "…and then carry the task through" — e.g. search → open the most relevant result → read/scroll → report or act. Don't hand it back half-done; only pause if you truly need the user's input (a login, a payment, a choice only they can make).
- Keep the user lightly in the loop on long browser chains with a short "dekh rahi hoon…" and a final summary of what you found/did.

FINISH THE WHOLE TASK (do not stop after step 1):
- A request can have several sub-steps. Keep calling tools until EVERY part is done. Do NOT open an app and then say "done" — if the user asked you to open Word AND write an essay, you must also produce the essay, not just open Word.
- Think of the full workflow first, then execute each step, then give one short confirmation at the end.

MULTI-APP / MULTI-STEP WORKFLOWS (chaining):
- For a request with several deliverables (e.g. "research X, then make a document AND a presentation"), plan the whole chain, then execute it: gather info first (search_web/fetch_url), then produce EACH artifact.
- BATCH independent creations in a SINGLE turn: if you must make both a document and a presentation, emit create_document AND create_presentation together in one response (they run in parallel) instead of one, waiting, then the other. This is much faster.
- Only sequence steps that truly depend on each other (research must finish before you can write about it; a file must exist before you edit it).
- At the end, give ONE combined confirmation listing everything you made and where it's saved.

CREATING DOCUMENTS / PRESENTATIONS / SPREADSHEETS (dynamic, any topic):
- You have dedicated skills that build polished files reliably — ALWAYS prefer these over typing into an app or writing python code:
  - create_document — Word (.docx) essays, letters, reports, notes. You write the full content and pass a title + sections (heading, body paragraphs, bullets). It formats, saves to Documents, and opens it.
  - create_presentation — PowerPoint (.pptx). Pass a title + slides (each a title and bullets/body). It builds and opens the deck.
  - create_spreadsheet — Excel (.xlsx). Pass sheets with headers + rows.
- ASK FIRST WHEN THE TOPIC IS MISSING: if the user asks for a document/PPT/sheet but does NOT say WHAT it should be about (e.g. "ek 3 page ki PPT bana do" with no subject), do NOT invent a random topic. Ask ONE short question first: "किस topic पर banau?" Only create it once you know the subject. It is wrong to guess the content and make something they did not ask for.
- RESPECT THE EXACT COUNT: if they say "3 page" / "3 slides" / "5 rows", make EXACTLY that many — not 4, not 2. Match the number they gave.
- YOU generate the actual content (the essay text, the slide bullet points, the table data) — the tool only does the formatting/saving/opening. So write real, complete, well-structured content, not placeholders.
- These open the finished file automatically, so you do NOT also need to open the app separately.
- Every file is saved to ONE fixed folder (the tool result includes the exact 'path'). ALWAYS tell the user where you saved it — name the folder (e.g. "E:\\AishaFiles में save कर दिया") so nothing is ever lost. Never save files to random/temporary locations.
- To ADD or CHANGE content in an EXISTING document (e.g. "add references", "add a section"), call create_document again with append_to set to that file's name/path — this opens and extends the real .docx. NEVER use write_file on a .docx/.pptx/.xlsx; those are binary and will be corrupted or silently ignored (write_file will refuse them).
- Only fall back to write_file (.txt/.md) or computer_control typing if a dedicated skill genuinely does not fit.

DO NOT THINK OUT LOUD OR STALL:
- Do NOT reply with "main plan bana rahi hoon", "thoda ruk jao, main likhti hoon", "ek minute", or any promise to do it later. Either call the tool NOW in this same turn, or ask a needed question. Never send a placeholder "I'm working on it" message with no tool call.
- After the tool finishes, THEN give one short confirmation. The user only ever sees a result, never your planning narration.
- Example — "discipline पर essay likho": call create_document with title "Discipline" and 3-4 sections (Introduction, body points, Conclusion), each with real paragraphs. Then confirm in one short sentence.

CONTACTS / NUMBERS / PERSONAL DETAILS:
- To message a person BY NAME (WhatsApp/call/SMS), FIRST call the contacts tool with action 'lookup' and their name. Use the number it returns.
- If lookup returns found:false, you do NOT have the number. ASK the user for it (e.g. "सोना का number नहीं है मेरे पास, number बता दो?") and offer to save it (contacts add). NEVER invent a number like 9198... and never call send_whatsapp with a made-up number.
- When searching a contact by name inside an app, type the name exactly as the user said it. The name may be Hindi or English; if unsure which the contact is saved as, ask.
- For news/facts: Use search results directly. 1-2 tool steps max.
- When completing a task: Briefly confirm naturally in 1-2 sentences WITH personality. NOT a robotic "Kaam poora ho gaya." Say something like "बस हो गया बॉस! Downloads folder खुल गया है, देख लो।"
- When chatting: Be genuinely conversational. Ask follow-up questions. React to what user says.

LANGUAGE RULES (HARD — this is a text-to-speech assistant, script matters):
- Write EVERY Hindi word in Devanagari script. This is non-negotiable.
- NEVER write Hindi in Latin/Roman letters. WRONG: "Arre yaar, WhatsApp khol diya, dekho abhi screen pe open hai". RIGHT: "WhatsApp खोल दिया, देखो अभी screen पर open है". If you catch yourself typing romanised Hindi (haan, nahi, kar do, dekho, khol, raha, kya, main, tum), convert it to Devanagari (हाँ, नहीं, कर दो, देखो, खोल, रहा, क्या, मैं, तुम) before answering.
- Only real English/product words (WhatsApp, Chrome, screen, folder, calculator) may stay in Latin. Everything Hindi is Devanagari.
- No emojis, no brackets [], no asterisks, no markdown
- No incomplete/cutoff sentences — always finish what you're saying
- Never output Cyrillic/Russian text.
- Never add notes, self-corrections, or commentary about language/model mistakes. Silently return only the corrected final reply.
</runtime_rules>
"""

# Confirmation matching is intentionally forgiving: the user is answering a yes/no
# prompt, so we look for an affirmation/refusal ANYWHERE in the reply (not an exact
# full-string match) and cover both Latin and Devanagari spellings. Refusal is
# checked before affirmation so "नहीं" / "no" never reads as a yes.
YES_PATTERNS = (
    r"\b(haan|han|haa|ha|yes|yep+|yeah|yup|ya|ok|okay|kk|sure|please|pls|plz|confirm|proceed|"
    r"go ?ahead|do ?it|carry on|kar\s?do|kardo|kr\s?do|krdo|kar\s?de|kardijiye|karo|kro|"
    r"chala\s?do|bhej\s?do|bhejo|send|start|bilkul|theek|thik|done|ji)\b",
    r"(हाँ|हां|हा|जी|कर\s?दो|करदो|कर\s?दे|कर दीजिए|करो|भेज\s?दो|भेजो|चला\s?दो|चलाओ|"
    r"बिलकुल|बिल्कुल|ठीक\s?है|ठीक|ओके|जरूर|ज़रूर|प्लीज़|प्लीज)",
)
NO_PATTERNS = (
    r"\b(no+|nope|nah+|cancel|reject|stop|abort|do ?not|don'?t|nvm|never ?mind|wait|ruko|"
    r"mat karo|rehne do|rehne|chhod do|nahi+n?|naa?|baad me)\b",
    r"(नहीं|नही|मत करो|मत|रहने दो|छोड़ो|रुको|कैंसिल|कैन्सिल|बाद में|अभी नहीं)",
)

DEFAULT_MODEL_CASCADE = [
    "nvidia/nemotron-3-super-120b-a12b",
    "gpt-5.6-sol",
    "claude-opus-4-8",
    "claude-opus-5",
    "glm-5.3",
]


@dataclass
class PendingToolBatch:
    system_prompt: str
    messages: list[dict[str, Any]]
    calls: list[dict[str, Any]]
    warnings: list[str]
    remaining_steps: int
    created_at: float
    # Index of the not-yet-confirmed assistant tool_use message inside the live
    # thread, so a cancel/expire can roll it back and never leave a dangling
    # tool_use (which would corrupt the next API call).
    rollback_len: int = 0
    # Whether this task was running on the heavy provider, so the post-approval
    # continuation stays on the strong model (the "haan kar do" reply itself
    # would not be detected as heavy).
    prefer_heavy: bool = False


@dataclass
class _UnifiedBlock:
    type: str  # "text" or "tool_use"
    text: str = ""
    id: str = ""
    name: str = ""
    input: dict[str, Any] = field(default_factory=dict)


@dataclass
class _UnifiedResponse:
    content: list[_UnifiedBlock]


@dataclass
class ModelEndpoint:
    provider: str  # "AgentRouter" or "NVIDIA NIM"
    client: Any
    model_name: str
    is_openai: bool
    is_healthy: bool = True
    latency_ms: float = 0.0
    last_checked: float = 0.0
    error_count: int = 0
    last_error: str = ""

    def ping(self, timeout: float = 6.0) -> bool:
        """Send a lightweight 5-token ping to check availability and latency."""
        t0 = time.monotonic()
        try:
            if self.is_openai:
                extra_kwargs: dict[str, Any] = {}
                if "nvidia.com" in str(getattr(self.client, "base_url", "")).lower() or "nemotron" in self.model_name.lower():
                    extra_kwargs["extra_body"] = {"chat_template_kwargs": {"enable_thinking": False}}
                self.client.chat.completions.create(
                    model=self.model_name,
                    messages=[{"role": "user", "content": "ping"}],
                    max_tokens=5,
                    timeout=timeout,
                    **extra_kwargs,
                )
            else:
                self.client.messages.create(
                    model=self.model_name,
                    max_tokens=5,
                    messages=[{"role": "user", "content": "ping"}],
                    timeout=timeout,
                )
            self.latency_ms = round((time.monotonic() - t0) * 1000)
            self.is_healthy = True
            self.last_checked = time.monotonic()
            self.error_count = 0
            self.last_error = ""
            return True
        except Exception as e:
            self.is_healthy = False
            self.error_count += 1
            self.last_error = str(e)[:100]
            self.last_checked = time.monotonic()
            return False


class AutonomousAgent:
    def __init__(
        self,
        ai_client: Any,
        model: str | None = None,
        tool_registry: ToolRegistry | None = None,
        max_steps: int | None = None,
        max_tokens: int | None = None,
        on_tool_callback: Any = None,
        fallback_client: Any = None,
        fallback_models: list[str] | None = None,
        auto_ping_bg: bool = False,
        heavy_client: Any = None,
        heavy_models: list[str] | None = None,
    ):
        self.ai_client = ai_client
        self.client = ai_client  # Alias for backward compatibility
        self.fallback_client = fallback_client
        # Heavy / complex-task provider (e.g. Bay of Assets — Claude/GPT). Used first
        # for multi-step or content-heavy turns where the fast model is unreliable.
        self.heavy_client = heavy_client
        self.heavy_models = [m.strip() for m in (heavy_models or []) if m and m.strip()]
        self._prefer_heavy = False
        self.model = model or os.getenv("AISHA_PRIMARY_MODEL") or os.getenv("AISHA_MODEL") or os.getenv("MODEL") or "nvidia/nemotron-3-super-120b-a12b"
        
        # Primary cascade
        cascade_env = os.getenv("AISHA_MODEL_CASCADE")
        if cascade_env:
            self.model_cascade = [m.strip() for m in cascade_env.split(",") if m.strip()]
        else:
            self.model_cascade = list(DEFAULT_MODEL_CASCADE)

        if self.model not in self.model_cascade:
            self.model_cascade = [self.model] + [m for m in self.model_cascade if m != self.model]

        # Secondary / Fallback cascade (NVIDIA NIM)
        fb_env = os.getenv("NVIDIA_MODEL_CASCADE")
        if fb_env:
            self.fallback_models = [m.strip() for m in fb_env.split(",") if m.strip()]
        elif fallback_models:
            self.fallback_models = list(fallback_models)
        else:
            self.fallback_models = [
                "claude-opus-4-8",
                "deepseek-v4-flash",
                "",
            ]

        self.tools = tool_registry or ToolRegistry()
        self.max_steps = max_steps or int(os.getenv("AISHA_MAX_STEPS", "16"))
        self.max_tokens = max_tokens or int(os.getenv("AISHA_MAX_TOKENS", "1200"))
        self.on_tool_callback = on_tool_callback
        self.pending: PendingToolBatch | None = None

        # Persistent conversation thread for THIS process. Unlike text-only history,
        # this keeps the real API messages — including tool_use calls and their
        # tool_result outputs — so across turns the model actually knows what it did
        # (opened WhatsApp, got the folder listing, etc.) instead of guessing from a
        # one-line text summary. It resets to empty on a fresh process start.
        self.conversation: list[dict[str, Any]] = []
        self.max_thread_messages = int(os.getenv("AISHA_MAX_THREAD_MESSAGES", "24"))

        # Build Provider & Model Endpoint Pool
        self.endpoints: list[ModelEndpoint] = []
        is_primary_openai = hasattr(self.ai_client, "chat")
        for m in self.model_cascade:
            self.endpoints.append(ModelEndpoint(
                provider="AgentRouter" if not is_primary_openai else "Primary",
                client=self.ai_client,
                model_name=m,
                is_openai=is_primary_openai,
            ))
        if self.fallback_client:
            is_fb_openai = hasattr(self.fallback_client, "chat")
            for m in self.fallback_models:
                self.endpoints.append(ModelEndpoint(
                    provider="NVIDIA NIM",
                    client=self.fallback_client,
                    model_name=m,
                    is_openai=is_fb_openai,
                ))

        # Heavy-task endpoints (Bay of Assets). Kept in their own list; they are only
        # consulted when a turn is flagged as heavy (see get_ordered_endpoints).
        self.heavy_endpoints: list[ModelEndpoint] = []
        if self.heavy_client and self.heavy_models:
            is_heavy_openai = hasattr(self.heavy_client, "chat")
            for m in self.heavy_models:
                self.heavy_endpoints.append(ModelEndpoint(
                    provider="Heavy",
                    client=self.heavy_client,
                    model_name=m,
                    is_openai=is_heavy_openai,
                ))

        # Launch background health check monitor
        if auto_ping_bg:
            self.start_background_health_monitor(interval_seconds=180)

    def ping_all_endpoints(self, print_summary: bool = True) -> list[ModelEndpoint]:
        """Ping all model endpoints in parallel and report health / latency."""
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(self.endpoints), 8)) as ex:
            futures = [ex.submit(ep.ping) for ep in self.endpoints]
            concurrent.futures.wait(futures, timeout=10)

        if print_summary:
            status_parts = []
            for ep in self.endpoints:
                if ep.is_healthy:
                    status_parts.append(f"[{ep.model_name}: {ep.latency_ms}ms OK]")
                else:
                    status_parts.append(f"[{ep.model_name}: DOWN]")
            print(f"AI Pool Health: {' '.join(status_parts)}")
        return self.endpoints

    def start_background_health_monitor(self, interval_seconds: int = 180):
        """Start a background daemon thread that periodically pings all AI models."""
        def _monitor_loop():
            # Initial ping after 2 seconds
            time.sleep(2.0)
            self.ping_all_endpoints(print_summary=True)
            while True:
                time.sleep(interval_seconds)
                try:
                    self.ping_all_endpoints(print_summary=False)
                except Exception:
                    pass

        t = threading.Thread(target=_monitor_loop, daemon=True, name="AIPoolHealthMonitor")
        t.start()

    def get_ordered_endpoints(self) -> list[ModelEndpoint]:
        """Get endpoints sorted by health status and priority.

        When the current turn is flagged heavy, the strong Bay-of-Assets models are
        tried FIRST (with the fast pool kept as fallback), so complex/multi-step work
        goes to a reliable model instead of the fast-but-shallow default.
        """
        healthy_primary = [ep for ep in self.endpoints if ep.provider != "NVIDIA NIM" and ep.is_healthy]
        healthy_fallback = [ep for ep in self.endpoints if ep.provider == "NVIDIA NIM" and ep.is_healthy]
        unhealthy = [ep for ep in self.endpoints if not ep.is_healthy]
        base = healthy_primary + healthy_fallback + unhealthy
        if self._prefer_heavy and self.heavy_endpoints:
            healthy_heavy = [ep for ep in self.heavy_endpoints if ep.is_healthy]
            unhealthy_heavy = [ep for ep in self.heavy_endpoints if not ep.is_healthy]
            return healthy_heavy + base + unhealthy_heavy
        return base

    def _call_openai_api(self, client: Any, model: str, system_prompt: str, messages: list[dict[str, Any]]) -> _UnifiedResponse:
        """Execute chat completion against standard OpenAI / NVIDIA NIM / Groq endpoints."""
        oai_messages: list[dict[str, Any]] = [{"role": "system", "content": system_prompt}]
        for m in messages:
            role = m.get("role", "user")
            content = m.get("content", "")
            if isinstance(content, str):
                oai_messages.append({"role": role, "content": content})
            elif isinstance(content, list):
                if role == "user" and any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
                    for b in content:
                        if isinstance(b, dict) and b.get("type") == "tool_result":
                            oai_messages.append({
                                "role": "tool",
                                "tool_call_id": b.get("tool_use_id", "call_1"),
                                "content": str(b.get("content", "")),
                            })
                elif role == "assistant":
                    tool_calls_payload = []
                    text_parts = []
                    for b in content:
                        if isinstance(b, dict):
                            if b.get("type") == "text":
                                text_parts.append(b.get("text", ""))
                            elif b.get("type") == "tool_use":
                                tool_calls_payload.append({
                                    "id": b.get("id", f"call_{len(tool_calls_payload)+1}"),
                                    "type": "function",
                                    "function": {
                                        "name": b.get("name", ""),
                                        "arguments": json.dumps(b.get("input", {})),
                                    },
                                })
                    msg_dict: dict[str, Any] = {"role": "assistant", "content": "".join(text_parts) or None}
                    if tool_calls_payload:
                        msg_dict["tool_calls"] = tool_calls_payload
                    oai_messages.append(msg_dict)
                else:
                    text_parts = [b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"]
                    oai_messages.append({"role": role, "content": "".join(text_parts)})

        tools_param = self.tools.openai_definitions if self.tools else None
        extra_body: dict[str, Any] = {}
        base_url_l = str(getattr(client, "base_url", "")).lower()
        model_l = model.lower()
        if "nvidia.com" in base_url_l or "nemotron" in model_l:
            extra_body["chat_template_kwargs"] = {"enable_thinking": False}
        # gpt-oss / qwen are REASONING models — without this they burn thousands of
        # thinking tokens before answering (turns a 0.5s reply into 40s+). Keep the
        # fast path snappy with minimal reasoning.
        if "gpt-oss" in model_l or "groq" in base_url_l or "qwen" in model_l:
            extra_body["reasoning_effort"] = "low"
        extra_kwargs: dict[str, Any] = {"extra_body": extra_body} if extra_body else {}

        res = client.chat.completions.create(
            model=model,
            messages=oai_messages,
            tools=tools_param or None,
            max_tokens=self.max_tokens,
            temperature=0.68,
            top_p=0.90,
            # Penalise repeated tokens/phrases so weak models can't fall into a
            # runaway loop (the "gaana chal raha hai... chal raha hai..." bug).
            frequency_penalty=0.5,
            presence_penalty=0.3,
            **extra_kwargs,
        )
        choice = res.choices[0]
        blocks: list[_UnifiedBlock] = []
        raw_text = choice.message.content or ""
        if "<think>" in raw_text and "</think>" in raw_text:
            raw_text = re.sub(r"<think>.*?</think>", "", raw_text, flags=re.DOTALL).strip()
        elif re.search(r"We need to .*?(?:Thus respond:|Let's produce:|So:|Produce:)", raw_text, re.DOTALL | re.IGNORECASE):
            quotes = re.findall(r'["“]([^"”]{5,})["”]', raw_text)
            if quotes:
                raw_text = quotes[-1].strip()
            else:
                raw_text = re.sub(r"We need to .*?(?:Thus respond:|Let's produce:|So:|Produce:)\s*", "", raw_text, flags=re.DOTALL | re.IGNORECASE).strip()

        if raw_text:
            blocks.append(_UnifiedBlock(type="text", text=raw_text))
        if choice.message.tool_calls:
            for tc in choice.message.tool_calls:
                call_args = {}
                try:
                    call_args = json.loads(tc.function.arguments) if tc.function.arguments else {}
                except Exception:
                    pass
                # Some models (esp. NVIDIA NIM) leak chat-template markup into the
                # tool name, e.g. "get_datetime\n</tool_call". Keep only the leading
                # identifier so the call still resolves to a real tool.
                raw_name = tc.function.name or ""
                match = re.match(r"[A-Za-z_][A-Za-z0-9_]*", raw_name.strip())
                clean_name = match.group(0) if match else raw_name.strip()
                blocks.append(_UnifiedBlock(
                    type="tool_use",
                    id=tc.id,
                    name=clean_name,
                    input=call_args,
                ))
        return _UnifiedResponse(content=blocks)

    def _create_message(self, system_prompt: str, messages: list[dict[str, Any]]) -> Any:
        """Call model with dynamic health-aware failsafe routing while preserving 100% context."""
        endpoints = self.get_ordered_endpoints()
        last_exception = None

        for endpoint in endpoints:
            for attempt in range(1, 3):
                try:
                    if endpoint.is_openai:
                        res = self._call_openai_api(endpoint.client, endpoint.model_name, system_prompt, messages)
                    else:
                        res = endpoint.client.messages.create(
                            model=endpoint.model_name,
                            max_tokens=self.max_tokens,
                            system=system_prompt,
                            messages=messages,
                            tools=self.tools.definitions,
                        )
                    if endpoint.model_name != self.model:
                        print(f"🔄 Active Route: [{endpoint.provider} -> {endpoint.model_name}]")
                        self.model = endpoint.model_name
                    endpoint.is_healthy = True
                    return res
                except Exception as exc:
                    last_exception = exc
                    endpoint.is_healthy = False
                    err_str = str(exc).lower()
                    status_code = getattr(exc, "status_code", None)

                    # If permanent failure (400 content-blocked, 403, 404, quota), switch immediately to next endpoint
                    if (
                        status_code in {400, 402, 403, 404, 503}
                        or "quota" in err_str
                        or "budget" in err_str
                        or "content-blocked" in err_str
                        or "unsupported" in err_str
                    ):
                        print(f"⚠️ Endpoint [{endpoint.provider}/{endpoint.model_name}] blocked/unavailable ({status_code or 'error'}). Switching...")
                        break

                    # Transient retry for 429 / 500 / timeouts
                    if attempt < 2 and (status_code in {408, 409, 429, 500, 502, 504} or isinstance(exc, (TimeoutError, ConnectionError))):
                        time.sleep(1.0)
                        continue

        if last_exception:
            raise last_exception
        raise RuntimeError("All models and provider endpoints failed.")

    @property
    def has_pending_confirmation(self) -> bool:
        return self.pending is not None and not self._pending_expired()

    def run_task(
        self,
        user_query: str,
        system_prompt: str,
        max_steps: int | None = None,
        conversation_history: list[dict[str, str]] | None = None,
        prefer_heavy: bool | None = None,
    ) -> str:
        """Run a request until the model answers, pauses for approval, or hits a limit.

        prefer_heavy: when True (and a heavy provider is configured), route this turn
        to the strong Bay-of-Assets models first. When None, auto-detect from the query.
        """
        query = user_query.strip()
        if not query:
            return "Kya karna hai, bas bata do."

        if self.heavy_endpoints:
            self._prefer_heavy = self._is_heavy_task(query) if prefer_heavy is None else bool(prefer_heavy)
        else:
            self._prefer_heavy = False

        pending_reply = self._handle_pending_confirmation(query)
        if pending_reply is not None:
            return pending_reply

        # Seed the live thread from text history only on the very first turn of the
        # process; after that the thread carries the real messages (with tool calls
        # and results) forward, so the model retains genuine context between turns.
        if not self.conversation and conversation_history:
            self.conversation = self._history_messages(conversation_history)
        self.conversation.append({"role": "user", "content": query})
        enhanced_prompt = system_prompt.rstrip() + "\n\n" + RUNTIME_INSTRUCTIONS.strip()
        result = self._run_loop(enhanced_prompt, self.conversation, max_steps or self.max_steps)
        # Never trim mid-confirmation: the pending batch holds a reference to this
        # exact list, and trimming rebinds it. Trim only once the turn is resolved.
        if not self.pending:
            self._trim_conversation()
        return result

    # Content the strong model should write, or research/analysis it should reason
    # over. NOTE: bare app names ("word", "excel", "chrome") are deliberately NOT
    # here — opening an app is trivial and must stay on the fast model.
    _HEAVY_HINTS = (
        "essay", "निबंध", "article", "लेख", "report", "रिपोर्ट", "letter", "पत्र",
        "document", "presentation", "spreadsheet", "resume", "cv", "blog",
        "story", "कहानी", "poem", "कविता", "lyrics",
        "code", "program", "algorithm", "debug",
        "summary", "summarize", "सारांश", "translate", "अनुवाद",
        "research", "रिसर्च", "compare", "तुलना", "review", "recommend", "suggest",
        "analyse", "analyze", "explain", "pros and cons", "kaunsa", "kaun sa", "कौन सा",
        "step by step", "workflow", "500 word", "1000 word", "detailed", "विस्तार से",
    )
    # Verbs that mean "produce substantial content" — heavy only when paired with real
    # content, so "kholo/open" (just launching something) never counts.
    _CREATE_VERBS = ("likho", "लिखो", "likh do", "likh de", "banao", "बनाओ", "bana do",
                     "write", "create", "generate", "draft", "compose", "prepare")
    # Simple launch/close/system verbs → always fast, never heavy.
    _SIMPLE_VERBS = ("open", "kholo", "khol", "खोलो", "launch", "start", "chalu", "chala",
                     "band", "close", "switch", "minimize", "maximize", "play", "pause")

    def _is_heavy_task(self, query: str) -> bool:
        q = query.lower()
        # A short "open/launch/close X" style command is always a fast task.
        if any(re.search(rf"\b{v}\b", q) for v in self._SIMPLE_VERBS) and len(q) < 70:
            if not any(h in q for h in self._HEAVY_HINTS):
                return False
        if any(h in q for h in self._HEAVY_HINTS):
            return True
        # A creation verb only makes it heavy if there's real substance to write
        # (not "ek line likho"): treat longer creation requests as heavy.
        if any(v in q for v in self._CREATE_VERBS) and len(q) > 40:
            return True
        return len(q) > 220

    def reset_conversation(self) -> None:
        """Drop the live thread (e.g. when the user explicitly starts over)."""
        self.conversation = []
        self.pending = None

    def _trim_conversation(self) -> None:
        """Bound the live thread without ever splitting a tool_use / tool_result pair.

        We only ever cut at a plain user text message (a real user turn), which is
        always a valid place to restart the thread.
        """
        conv = self.conversation
        if len(conv) <= self.max_thread_messages:
            return
        target = len(conv) - self.max_thread_messages
        for i in range(target, len(conv)):
            m = conv[i]
            if m.get("role") == "user" and isinstance(m.get("content"), str):
                self.conversation = conv[i:]
                return

    def cancel_pending(self) -> bool:
        had_pending = self.pending is not None
        if self.pending is not None:
            # Remove the un-approved tool_use from the live thread so it stays valid.
            del self.conversation[self.pending.rollback_len:]
        self.pending = None
        return had_pending

    def _run_loop(self, system_prompt: str, messages: list[dict[str, Any]], steps: int) -> str:
        last_error = ""
        for step_index in range(max(1, steps)):
            try:
                response = self._create_message(system_prompt, messages)
            except Exception as exc:
                last_error = str(exc)
                print(f"Agent API Error: {type(exc).__name__}: {last_error}")
                return "AI service se abhi connection nahi ban paaya. Internet aur API settings check karke phir try karna."

            assistant_content = [self._serialize_block(block) for block in response.content]
            assistant_content = [block for block in assistant_content if block]
            tool_calls = [
                {
                    "id": block["id"],
                    "name": block["name"],
                    "input": block.get("input") or {},
                }
                for block in assistant_content
                if block.get("type") == "tool_use"
            ]

            if tool_calls:
                if self.on_tool_callback:
                    try:
                        self.on_tool_callback(step_index, tool_calls)
                    except Exception:
                        pass
                # Index of this assistant tool_use message, so an un-approved batch
                # can be rolled back cleanly on cancel/expire.
                pause_index = len(messages)
                messages.append({"role": "assistant", "content": assistant_content})
                warnings = []
                for call in tool_calls:
                    warning = self.tools.confirmation_summary(call["name"], call["input"])
                    if warning:
                        warnings.append(warning)

                if warnings:
                    self.pending = PendingToolBatch(
                        system_prompt=system_prompt,
                        messages=messages,
                        calls=tool_calls,
                        warnings=warnings,
                        remaining_steps=max(1, steps - step_index - 1),
                        created_at=time.monotonic(),
                        rollback_len=pause_index,
                        prefer_heavy=self._prefer_heavy,
                    )
                    action_text = "; ".join(warnings)
                    return random.choice([
                        f"{action_text} — कर दूँ? हाँ या ना बोल दो।",
                        f"ये करने वाली हूँ: {action_text}. आगे बढ़ूँ?",
                        f"{action_text}. बस हाँ बोलो तो कर देती हूँ।",
                        f"तैयार है — {action_text}. करूँ इसे?",
                    ])

                results = self._execute_tool_batch(tool_calls)
                messages.append({"role": "user", "content": results})
                continue

            text_reply = self._extract_text(assistant_content)
            if not text_reply:
                text_reply = "Kaam poora ho gaya."
            # Guard against runaway repetition from a degraded fallback model.
            text_reply = _truncate_runaway(text_reply)
            # Record the assistant's own reply in the thread so the next turn sees a
            # proper alternating conversation (and doesn't stack two user turns in a
            # row after a tool-free chat reply).
            messages.append({"role": "assistant", "content": text_reply})
            return text_reply

        return "Action chal gaya, lekin main poora task finish nahi kar paayi."

    def _handle_pending_confirmation(self, query: str) -> str | None:
        if not self.pending:
            return None
        if self._pending_expired():
            # Roll the un-approved tool_use out of the live thread before dropping it.
            del self.conversation[self.pending.rollback_len:]
            self.pending = None
            return "Pichla confirmation expire ho gaya. Dubara bol do kya karna hai."

        lowered = query.lower()
        # Refusal is checked FIRST so phrases containing both (e.g. "cancel kar do")
        # are treated as a cancel, never an approval.
        if any(re.search(pat, lowered) for pat in NO_PATTERNS):
            del self.conversation[self.pending.rollback_len:]
            self.pending = None
            return "Theek hai, maine action cancel kar diya."

        if any(re.search(pat, lowered) for pat in YES_PATTERNS):
            pending = self.pending
            self.pending = None
            # Keep the strong model for the continuation if the task was heavy.
            self._prefer_heavy = pending.prefer_heavy
            print(f"  Approved pending tool batch ({len(pending.calls)} calls)")
            results = self._execute_tool_batch(pending.calls)
            pending.messages.append({"role": "user", "content": results})
            reply = self._run_loop(pending.system_prompt, pending.messages, pending.remaining_steps)
            self._trim_conversation()
            return reply

        return "Main abhi confirmation ka wait kar rahi hoon. 'haan kar do' ya 'Cancel' bolo."

    def _execute_tool_batch(self, tool_calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if len(tool_calls) == 1:
            call = tool_calls[0]
            call_id = call["id"]
            name = call["name"]
            arguments = call.get("input") or {}
            preview = self._safe_preview(arguments)
            print(f"  [TOOL] {name}({preview})")
            tool_output = self.tools.execute(name, arguments)
            is_error = False
            try:
                parsed = json.loads(tool_output)
                if isinstance(parsed, dict) and parsed.get("ok") is False:
                    is_error = True
            except (json.JSONDecodeError, TypeError):
                pass
            print(f"  [RESULT] {tool_output[:150]}...")
            return [
                {
                    "type": "tool_result",
                    "tool_use_id": call_id,
                    "content": tool_output,
                    "is_error": is_error,
                }
            ]

        # Multi-tool parallel execution using ThreadPoolExecutor for lightning speed
        print(f"  ⚡ Executing {len(tool_calls)} tools in parallel...")

        def _execute_single(call: dict[str, Any]) -> dict[str, Any]:
            call_id = call["id"]
            name = call["name"]
            arguments = call.get("input") or {}
            preview = self._safe_preview(arguments)
            print(f"  [TOOL:PARALLEL] {name}({preview})")
            tool_output = self.tools.execute(name, arguments)
            is_error = False
            try:
                parsed = json.loads(tool_output)
                if isinstance(parsed, dict) and parsed.get("ok") is False:
                    is_error = True
            except (json.JSONDecodeError, TypeError):
                pass
            print(f"  [RESULT:PARALLEL] {name} -> {tool_output[:120]}...")
            return {
                "type": "tool_result",
                "tool_use_id": call_id,
                "content": tool_output,
                "is_error": is_error,
            }

        with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(tool_calls), 8)) as executor:
            futures = [executor.submit(_execute_single, call) for call in tool_calls]
            return [f.result() for f in futures]



    @staticmethod
    def _history_messages(history: list[dict[str, str]]) -> list[dict[str, str]]:
        messages: list[dict[str, str]] = []
        for exchange in history[-5:]:
            user_text = str(exchange.get("user", "")).strip()
            assistant_text = str(exchange.get("assistant", "")).strip()
            if user_text and assistant_text:
                # Deduplicate consecutive identical assistant turns to prevent echo loops
                if messages and messages[-1].get("content") == assistant_text:
                    continue
                messages.append({"role": "user", "content": user_text[:1000]})
                messages.append({"role": "assistant", "content": assistant_text[:1000]})
        return messages

    @staticmethod
    def _serialize_block(block: Any) -> dict[str, Any] | None:
        if hasattr(block, "model_dump"):
            data = block.model_dump(exclude_none=True)
            if isinstance(data, dict):
                return data
        block_type = getattr(block, "type", "")
        if block_type == "text":
            return {"type": "text", "text": getattr(block, "text", "")}
        if block_type == "tool_use":
            return {
                "type": "tool_use",
                "id": getattr(block, "id", ""),
                "name": getattr(block, "name", ""),
                "input": getattr(block, "input", {}) or {},
            }
        if block_type == "thinking":
            return None
        if hasattr(block, "text"):
            return {"type": "text", "text": getattr(block, "text", "")}
        return None

    @staticmethod
    def _extract_text(content: list[Any]) -> str:
        parts: list[str] = []
        for block in content:
            if not block:
                continue
            if isinstance(block, dict) and block.get("type") == "text":
                text_val = block.get("text", "").strip()
                if text_val:
                    parts.append(text_val)
            elif getattr(block, "type", "") == "text":
                text_val = getattr(block, "text", "").strip()
                if text_val:
                    parts.append(text_val)
            elif hasattr(block, "text"):
                text_val = getattr(block, "text", "").strip()
                if text_val:
                    parts.append(text_val)
        raw_text = "\n".join(parts).strip()
        cleaned = re.sub(r"<think>.*?</think>", "", raw_text, flags=re.DOTALL)
        cleaned = re.sub(r"</?think>", "", cleaned)
        cleaned = re.sub(r"[\u4e00-\u9fff\u3040-\u30ff\u3400-\u4dbf]+", "", cleaned)

        # Strip unicode emojis (prevents TTS glitches and unnatural display)
        cleaned = re.sub(
            r"[\U00010000-\U0010ffff]|[\u2600-\u27BF]|[\u2300-\u23FF]|[\u2B50-\u2B55]|[\u200D\uFE0F]",
            "", cleaned
        )

        # Strip all bracketed roleplay/sound text. The LLM is explicitly told
        # never to emit brackets, and the offline cache generator also strips
        # them. Allowing any tags through creates inconsistent voice delivery
        # per ElevenLabs v3 generation — better to drop them all.
        cleaned = re.sub(r"\[(.*?)\]", "", cleaned)
        cleaned = re.sub(r"\s{2,}", " ", cleaned).strip()
        return cleaned

    @staticmethod
    def _safe_preview(arguments: dict[str, Any]) -> str:
        preview = {}
        for key, value in arguments.items():
            if key in {"content", "text", "code"}:
                preview[key] = f"<{len(str(value))} chars>"
            else:
                preview[key] = value
        rendered = repr(preview)
        return rendered[:500] + ("..." if len(rendered) > 500 else "")

    def _pending_expired(self) -> bool:
        return bool(self.pending and time.monotonic() - self.pending.created_at > 300)


__all__ = ["AutonomousAgent", "TOOL_DEFINITIONS", "ToolRegistry", "execute_tool"]
