"""
aisha_gui.py — Expressive, Alive Chat Overlay for Aisha AI

Visual Design:
- Soft glass card with ambient glow that pulses with Aisha's emotional state
- Animated waveform bars with glow effect and audio reactivity
- Smooth typing indicator with bouncing dots
- Chat bubbles with avatars, timestamps, and slide-in animations
- Ambient particle background for "alive" feeling
- Breathing idle animation when Aisha is waiting
- State-aware color theming throughout

Features:
- Live chat history with smooth scroll
- Typing animation (character-by-character with cursor)
- Emotion-reactive visual pulses
- Audio level visualization
- Collapsible chat panel
- Quick action buttons
- Drag to reposition

Stack: PyQt6, pure Python painting
"""

from __future__ import annotations

import os
import sys
import math
import re
import json
import time
import random
from datetime import datetime
from collections import deque

from PyQt6.QtCore import (
    Qt, QTimer, pyqtSignal, QObject, QPoint, QRectF, QSizeF, QEvent,
    QPropertyAnimation, QEasingCurve, QParallelAnimationGroup,
    QPointF
)
from PyQt6.QtGui import (
    QColor, QFont, QFontDatabase, QPainter, QPen, QBrush,
    QCursor, QIcon, QPixmap, QPainterPath, QRadialGradient,
    QLinearGradient, QGuiApplication
)
from PyQt6.QtWidgets import (
    QApplication, QWidget, QLabel, QFrame, QSystemTrayIcon,
    QMenu, QVBoxLayout, QHBoxLayout,
    QGraphicsOpacityEffect, QSizePolicy, QScrollArea, QPushButton,
    QStackedWidget, QLineEdit
)

# ----------------------------------------------------------------------
# High-DPI crispness — MUST run before any QApplication is created.
# Windows displays are usually at 125%/150% scaling; Qt6 rounds the scale
# factor by default, which renders at a lower resolution and upscales →
# blurry/pixelated. PassThrough keeps true fractional scaling = crisp.
# ----------------------------------------------------------------------
try:
    if QApplication.instance() is None:
        QApplication.setHighDpiScaleFactorRoundingPolicy(
            Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
        )
except Exception:
    pass
# (In Qt6 high-DPI pixmaps are always enabled, so no AA_UseHighDpiPixmaps needed.)

# QtWebEngine (used for the Live2D avatar) requires shared OpenGL contexts and
# must be imported BEFORE any QApplication is created. Do it here at import time
# so the real rigged avatar is available; failure just disables Live2D.
_WEBENGINE_OK = False
try:
    if QApplication.instance() is None:
        QApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts, True)
    import PyQt6.QtWebEngineWidgets  # noqa: F401  (import registers the module early)
    _WEBENGINE_OK = True
except Exception:
    _WEBENGINE_OK = False

# ----------------------------------------------------------------------
# Paths
# ----------------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
MEMORY_FILE = os.path.join(BASE_DIR, "memory.json")

# ----------------------------------------------------------------------
# Fonts — bundled Devanagari + Latin faces (assets/fonts)
# ----------------------------------------------------------------------
# HINDI_FONT is a crisp, modern Devanagari stack for Aisha's spoken replies
# (Noto Sans Devanagari renders हिंदी cleanly at any size). UI_FONT is the Latin
# face for brand/label chrome. Both fall back gracefully if a face is missing.
HINDI_FONT = "'Noto Sans Devanagari', 'Mukta', 'Nirmala UI', 'Segoe UI', sans-serif"
UI_FONT = "'Poppins', 'Segoe UI', sans-serif"


def _load_bundled_fonts():
    """Register the bundled TTFs so their families are available app-wide.

    Safe to call more than once; Qt de-duplicates. Requires a live QApplication.
    """
    import glob
    fonts_dir = os.path.join(BASE_DIR, "assets", "fonts")
    for path in glob.glob(os.path.join(fonts_dir, "*.ttf")):
        try:
            QFontDatabase.addApplicationFont(path)
        except Exception:
            pass

# ----------------------------------------------------------------------
# Color system — warm, alive, emotional
# ----------------------------------------------------------------------
class Palette:
    """Centralized color tokens for consistent theming."""

    # Card & surfaces
    CARD_BG = QColor(20, 20, 28)          # Deep warm dark
    CARD_BG_INNER = QColor(28, 28, 38)    # Slightly lighter
    CARD_BORDER = QColor(255, 255, 255, 8)
    CARD_BORDER_ACTIVE = QColor(244, 114, 182, 40)

    # Text
    TEXT_MAIN = QColor("#f1f5f9")         # Near white
    TEXT_DIM = QColor("#64748b")          # Muted gray
    TEXT_AISHA = QColor("#fce7f3")        # Pink-tinted for Aisha
    TEXT_USER = QColor("#e2e8f0")         # Cool white for user
    TEXT_TIMESTAMP = QColor("#475569")    # Very muted for timestamps

    # Bubbles
    BUBBLE_AISHA = QColor(35, 35, 48)     # Soft dark blue-gray
    BUBBLE_AISHA_BORDER = QColor(244, 114, 182, 15)
    BUBBLE_USER = QColor(60, 45, 70)      # Muted purple tint
    BUBBLE_USER_BORDER = QColor(255, 255, 255, 6)

    # Accent
    ACCENT = QColor("#f472b6")            # Rose pink — primary identity
    ACCENT_SOFT = QColor(244, 114, 182, 60)
    ACCENT_GLOW = QColor(244, 114, 182, 25)

    # State colors — each has foreground + glow variant
    STATES = {
        "standby": {
            "fg": QColor("#94a3b8"),
            "glow": QColor(148, 163, 184, 30),
            "wave": QColor(148, 163, 184),
        },
        "listening": {
            "fg": QColor("#f472b6"),
            "glow": QColor(244, 114, 182, 50),
            "wave": QColor(244, 114, 182),
        },
        "thinking": {
            "fg": QColor("#f59e0b"),
            "glow": QColor(245, 158, 11, 40),
            "wave": QColor(245, 158, 11),
        },
        "speaking": {
            "fg": QColor("#38bdf8"),
            "glow": QColor(56, 189, 248, 45),
            "wave": QColor(56, 189, 248),
        },
        "loading": {
            "fg": QColor("#c084fc"),
            "glow": QColor(192, 132, 252, 35),
            "wave": QColor(192, 132, 252),
        },
        "idle": {
            "fg": QColor("#64748b"),
            "glow": QColor(100, 116, 139, 20),
            "wave": QColor(100, 116, 139),
        },
    }

    # Emotion color pulses for waveform + card border glow
    EMOTIONS = {
        "happy":    QColor("#f472b6"),
        "excited":  QColor("#fb923c"),
        "calm":     QColor("#34d399"),
        "thinking": QColor("#f59e0b"),
        "warm":     QColor("#f472b6"),
        "playful":  QColor("#c084fc"),
    }

    @staticmethod
    def state_fg(state: str) -> QColor:
        return Palette.STATES.get(state, Palette.STATES["standby"])["fg"]

    @staticmethod
    def state_glow(state: str) -> QColor:
        return Palette.STATES.get(state, Palette.STATES["standby"])["glow"]

    @staticmethod
    def state_wave(state: str) -> QColor:
        return Palette.STATES.get(state, Palette.STATES["standby"])["wave"]


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------
def clean_for_ui(text: str) -> str:
    if not text:
        return ""
    cleaned = re.sub(r"\[.*?\]", "", text)
    cleaned = re.sub(r"[一-鿿぀-ヿ㐀-䶿]+", "", cleaned)
    cleaned = re.sub(r"[\*_#`~>\"]", "", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


def _timestamp_now() -> str:
    return datetime.now().strftime("%I:%M %p").lstrip("0")


# ----------------------------------------------------------------------
# Thread-safe signal bridge
# ----------------------------------------------------------------------
class GuiSignals(QObject):
    state_changed = pyqtSignal(str)
    user_speech = pyqtSignal(str)
    aisha_reply = pyqtSignal(str)
    audio_level = pyqtSignal(float)
    emotion_changed = pyqtSignal(str)
    # Live "what she is doing right now" during multi-step tool work.
    # Payload: (step_number, human-readable label). step_number 0 clears the feed.
    activity = pyqtSignal(int, str)
    # A newer release is available. Payload: info dict from updater.check_for_update.
    update_available = pyqtSignal(object)


SIGNALS = GuiSignals()


# ----------------------------------------------------------------------
# Ambient particles — subtle background "alive" feel
# ----------------------------------------------------------------------
class AmbientParticles(QWidget):
    """Tiny floating particles behind the card for ambient life."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.particles = []
        self._init_particles(18)
        self._base_color = QColor(244, 114, 182, 20)
        self._pulse = 0.0

        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(50)

    def _init_particles(self, count: int):
        w, h = 500, 200
        for _ in range(count):
            self.particles.append({
                "x": random.uniform(0, w),
                "y": random.uniform(0, h),
                "vx": random.uniform(-0.15, 0.15),
                "vy": random.uniform(-0.25, -0.05),
                "size": random.uniform(1.2, 3.0),
                "alpha": random.uniform(15, 45),
                "phase": random.uniform(0, math.pi * 2),
            })

    def set_state(self, state: str):
        palette = {
            "standby": QColor(148, 163, 184, 18),
            "listening": QColor(244, 114, 182, 30),
            "thinking": QColor(245, 158, 11, 22),
            "speaking": QColor(56, 189, 248, 25),
            "loading": QColor(192, 132, 252, 20),
            "idle": QColor(100, 116, 139, 12),
        }.get(state, QColor(244, 114, 182, 20))
        self._base_color = palette

    def set_pulse(self, intensity: float):
        self._pulse = min(1.0, max(0.0, intensity))

    def _tick(self):
        w, h = self.width(), self.height()
        if w < 10:
            w, h = 500, 200
        for p in self.particles:
            p["x"] += p["vx"] + math.sin(p["phase"]) * 0.1
            p["y"] += p["vy"]
            p["phase"] += 0.02
            if p["y"] < -5:
                p["y"] = h + 5
                p["x"] = random.uniform(0, w)
            if p["x"] < -5:
                p["x"] = w + 5
            if p["x"] > w + 5:
                p["x"] = -5
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        w, h = self.width(), self.height()

        for particle in self.particles:
            alpha = int(particle["alpha"] + self._pulse * 20)
            c = QColor(
                self._base_color.red(),
                self._base_color.green(),
                self._base_color.blue(),
                min(80, alpha)
            )
            p.setBrush(QBrush(c))
            p.setPen(Qt.PenStyle.NoPen)
            px, py = particle["x"], particle["y"]
            sz = particle["size"]
            # Draw soft circle with radial fade
            p.drawEllipse(QRectF(px - sz, py - sz, sz * 2, sz * 2))


# ----------------------------------------------------------------------
# Animated Waveform — expressive, state-aware, glow effect
# ----------------------------------------------------------------------
class AnimatedWaveform(QWidget):
    """Smooth audio waveform with glow effect and state-aware behavior."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(36)
        self.state = "standby"
        self._base_color = Palette.state_wave("standby")
        self._glow_color = Palette.state_glow("standby")
        self._phase = 0.0
        self._audio_level = 0.0
        self._target_level = 0.0
        self._bar_count = 32
        self._bar_heights = [0.08] * self._bar_count
        self._pulse_intensity = 0.0
        self._glow_radius = 0.0
        self._target_glow = 0.0

        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(16)

    def set_state(self, state: str):
        self.state = state
        self._base_color = Palette.state_wave(state)
        self._glow_color = Palette.state_glow(state)
        targets = {
            "listening": 0.55,
            "speaking": 0.75,
            "thinking": 0.30,
            "loading": 0.20,
        }
        self._target_level = targets.get(state, 0.06)
        self._target_glow = 0.6 if state != "standby" else 0.0
        self.update()

    def set_audio_level(self, rms: float):
        self._audio_level = min(1.0, max(0.0, rms * 6.0))

    def set_emotion(self, emotion: str):
        self._pulse_intensity = 0.7
        color = Palette.EMOTIONS.get(emotion, self._base_color)
        self._base_color = color
        self._glow_color = QColor(color.red(), color.green(), color.blue(), 50)
        self._target_glow = 0.9

    def _tick(self):
        self._audio_level += (self._target_level - self._audio_level) * 0.12
        self._phase += 0.065
        self._pulse_intensity *= 0.965
        self._glow_radius += (self._target_glow - self._glow_radius) * 0.08

        speed = 0.16
        for i in range(self._bar_count):
            wave1 = math.sin(self._phase + i * 0.32) * 0.5 + 0.5
            wave2 = math.sin(self._phase * 0.7 + i * 0.55) * 0.5 + 0.5
            audio = self._audio_level
            pulse = self._pulse_intensity

            if self.state == "listening":
                dist = abs(i - self._bar_count / 2) / (self._bar_count / 2)
                center = 1.0 - dist * 0.35
                target = 0.08 + audio * center * 0.9 + wave1 * 0.22
            elif self.state == "speaking":
                target = 0.12 + wave1 * 0.6 + wave2 * 0.3 + pulse * 0.2
            elif self.state == "thinking":
                target = 0.04 + wave2 * 0.28
            elif self.state == "loading":
                target = 0.04 + wave1 * 0.2
            else:
                # Idle breathing
                target = 0.03 + wave1 * 0.05

            self._bar_heights[i] += (target - self._bar_heights[i]) * speed

        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)

        w, h = self.width(), self.height()
        if w < 10:
            w = 500
        self.setFixedWidth(w)

        bar_w = 3.0
        gap = (w - self._bar_count * bar_w) / max(1, self._bar_count - 1)
        cy = h / 2.0

        # Draw glow layer first (behind bars)
        if self._glow_radius > 0.05:
            glow_alpha = int(self._glow_radius * 35)
            glow_color = QColor(
                self._base_color.red(),
                self._base_color.green(),
                self._base_color.blue(),
                glow_alpha
            )
            glow_pen = QPen(glow_color, 6.0)
            glow_pen.setStyle(Qt.PenStyle.SolidLine)
            glow_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            p.setPen(glow_pen)
            p.setBrush(Qt.BrushStyle.NoBrush)
            for i in range(self._bar_count):
                s = max(0.03, self._bar_heights[i])
                bar_h = max(2.0, s * h * 0.85)
                x = i * (bar_w + gap) + bar_w / 2
                p.drawLine(QPointF(x, cy - bar_h / 2), QPointF(x, cy + bar_h / 2))

        # Draw bars on top
        for i in range(self._bar_count):
            s = max(0.03, self._bar_heights[i])
            bar_h = max(2.0, s * h * 0.85)
            x = i * (bar_w + gap)
            y = cy - bar_h / 2.0

            alpha = int(50 + s * 205)
            c = QColor(
                self._base_color.red(),
                self._base_color.green(),
                self._base_color.blue(),
                min(255, alpha)
            )
            p.setBrush(QBrush(c))
            p.setPen(Qt.PenStyle.NoPen)
            p.drawRoundedRect(QRectF(x, y, bar_w, bar_h), 1.5, 1.5)

        # Center dot indicator
        dot_alpha = int(80 + self._audio_level * 175)
        dot_color = QColor(
            self._base_color.red(),
            self._base_color.green(),
            self._base_color.blue(),
            dot_alpha
        )
        p.setBrush(QBrush(dot_color))
        p.setPen(Qt.PenStyle.NoPen)
        dot_size = 2.5 + self._audio_level * 3.0
        p.drawEllipse(QRectF(w / 2 - dot_size / 2, cy - dot_size / 2, dot_size, dot_size))


# ----------------------------------------------------------------------
# Mic Level Meter — shows live microphone input level
# ----------------------------------------------------------------------
class MicLevelMeter(QWidget):
    """Horizontal mic level bar showing real-time voice input."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(6)
        self._level = 0.0
        self._target_level = 0.0
        self._peak = 0.0
        self._peak_decay = 0.0
        self._state = "standby"
        self._threshold_line = 0.0  # silence threshold position (0-1)

        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(50)

    def set_level(self, rms: float):
        self._target_level = min(1.0, max(0.0, rms * 5.0))
        if self._target_level > self._peak:
            self._peak = self._target_level
        self._target_level = min(1.0, max(0.0, rms * 5.0))

    def set_threshold(self, threshold: float):
        """Set the silence threshold position (0.0–1.0)."""
        self._threshold_line = min(1.0, max(0.0, threshold * 5.0))

    def set_state(self, state: str):
        self._state = state

    def _tick(self):
        # Smooth level interpolation
        self._level += (self._target_level - self._level) * 0.25
        # Peak decay
        self._peak_decay = max(self._level, self._peak) if self._peak > self._level else self._peak
        self._peak *= 0.97  # slow decay
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        w = self.width() if self.width() > 10 else 400
        self.setFixedWidth(w)
        h = self.height()

        # Background track
        p.setBrush(QBrush(QColor(255, 255, 255, 8)))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawRoundedRect(0, 0, w, h, 3, 3)

        # Level fill — color depends on state
        if self._state == "listening":
            level_color = QColor(244, 114, 182, 180)
        elif self._state == "thinking":
            level_color = QColor(245, 158, 11, 140)
        elif self._state == "speaking":
            level_color = QColor(56, 189, 248, 160)
        else:
            level_color = QColor(148, 163, 184, 100)

        fill_w = max(2, int(self._level * w))
        if fill_w > 2:
            p.setBrush(QBrush(level_color))
            p.setPen(Qt.PenStyle.NoPen)
            p.drawRoundedRect(0, 1, fill_w, h - 2, 3, 3)

        # Peak indicator
        peak_x = max(2, int(self._peak * w))
        peak_color = QColor(244, 114, 182, 200) if self._peak > 0.1 else QColor(255, 255, 255, 30)
        p.setBrush(QBrush(peak_color))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawRoundedRect(peak_x - 1, 0, 2, h, 1, 1)

        # Silence threshold marker (thin vertical line)
        if self._threshold_line > 0.05 and self._state == "listening":
            tx = int(self._threshold_line * w)
            p.setPen(QPen(QColor(255, 255, 255, 50), 1, Qt.PenStyle.DashLine))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawLine(tx, 0, tx, h)


# ----------------------------------------------------------------------
# Conversation Starters — suggested prompts when idle
# ----------------------------------------------------------------------
STARTER_PROMPTS = [
    ("मौसम बताओ", "weather Delhi"),
    ("न्यूज़ ढूँढो", "search_web latest news"),
    ("Downloads खोलो", "open my Downloads folder"),
    ("रिमाइंडर लगाओ", "remind me in 10 minutes to take a break"),
    ("पिछली बात", "what was I talking about last time?"),
]


class ConversationStarters(QWidget):
    """Suggested prompts shown when Aisha is idle/standby."""

    def __init__(self, assistant_instance=None, parent=None):
        super().__init__(parent)
        self.assistant = assistant_instance
        self._setup_ui()

    def _setup_ui(self):
        self.setContentsMargins(0, 0, 0, 0)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 2, 8, 2)
        layout.setSpacing(4)

        for label, command in STARTER_PROMPTS:
            btn = QPushButton(label)
            btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
            btn.setFixedHeight(26)
            btn.setToolTip(command)
            btn.setStyleSheet("""
                QPushButton {
                    background: rgba(244, 114, 182, 0.06);
                    color: #cbd5e1;
                    border: 1px solid rgba(244, 114, 182, 0.1);
                    border-radius: 10px;
                    padding: 4px 12px;
                    font-family: """ + HINDI_FONT + """;
                    font-size: 11px;
                    font-weight: 500;
                }
                QPushButton:hover {
                    background: rgba(244, 114, 182, 0.16);
                    color: #f1f5f9;
                    border-color: rgba(244, 114, 182, 0.3);
                }
                QPushButton:pressed {
                    background: rgba(244, 114, 182, 0.25);
                }
            """)
            btn.clicked.connect(lambda checked, cmd=command: self._on_starter(cmd))
            layout.addWidget(btn)

        layout.addStretch()

    def _on_starter(self, command: str):
        if self.assistant and hasattr(self.assistant, 'process_query'):
            import threading
            threading.Thread(
                target=self.assistant.process_query,
                args=(command,),
                daemon=True
            ).start()


# ----------------------------------------------------------------------
# Avatar — fully procedural animated anime woman, 30fps, no static art needed
# ----------------------------------------------------------------------
class AvatarWidget(QWidget):
    """Aisha's animated anime face.

    Fully drawn in code with QPainter — no sprite images needed, though real
    sprites in assets/avatar/ will still be layered on top if they exist.
    Everything animates at ~30fps: blink, breathe, head sway, eye tracking,
    lip-sync to audio, expression changes.
    """

    # State -> base expression
    STATE_EXPR = {
        "standby": "neutral", "idle": "neutral", "listening": "listening",
        "thinking": "thinking", "speaking": "neutral", "loading": "thinking", "muted": "neutral",
    }
    # Emotion -> expression override
    EMOTION_EXPR = {
        "happy": "happy", "excited": "happy", "cheerful": "happy", "laughing": "happy",
        "empathetic": "sad", "sad": "sad", "calming": "neutral", "curious": "thinking",
        "surprised": "surprised",
    }

    def __init__(self, parent=None, size=190):
        super().__init__(parent)
        self._size = size
        self.setFixedSize(size, size)
        self._dir = os.path.join(BASE_DIR, "assets", "avatar")
        self._cache: dict[str, QPixmap | None] = {}
        self.has_sprites = os.path.isfile(os.path.join(self._dir, "neutral.png"))

        # State
        self._state = "standby"
        self._emotion = "calm"
        self._current_expr = "neutral"
        self._target_expr = "neutral"
        self._expr_t = 1.0  # 0=current, 1=target (smooth transition)

        # Audio-driven lip-sync
        self._level = 0.0
        self._target_level = 0.0
        self._mouth_open = 0.0  # smoothed for rendering

        # Blink
        self._blink = 0.0       # 0=open .. 1=closed
        self._blink_state = "open"
        self._next_blink = time.time() + random.uniform(2.0, 5.0)

        # Animation phase
        self._phase = random.uniform(0, 6.28)
        self._mouth_phase = 0.0

        # Eye tracking (mouse position relative to widget)
        self._gaze_x = 0.0
        self._gaze_y = 0.0
        self._target_gaze_x = 0.0
        self._target_gaze_y = 0.0

        self._timer = QTimer(self)
        self._timer.setInterval(33)  # ~30fps
        self._timer.timeout.connect(self._tick)
        self._timer.start()

    # ---- public API ----
    def set_state(self, state: str):
        self._state = state
        self._apply_expr()

    def set_emotion(self, emotion: str):
        self._emotion = emotion
        self._apply_expr()

    def set_level(self, level: float):
        self._target_level = max(0.0, min(1.0, float(level)))

    def _apply_expr(self):
        emo = self.EMOTION_EXPR.get(self._emotion)
        if emo and self._state in ("speaking", "standby", "idle", "listening"):
            self._target_expr = emo
        else:
            self._target_expr = self.STATE_EXPR.get(self._state, "neutral")
        if self._target_expr != self._current_expr:
            self._expr_t = 0.0

    # ---- sprite helpers (unchanged) ----
    def _pix(self, name: str):
        if name not in self._cache:
            path = os.path.join(self._dir, name)
            pm = QPixmap(path) if os.path.isfile(path) else None
            self._cache[name] = pm if (pm and not pm.isNull()) else None
        return self._cache[name]

    # ---- 30fps tick ----
    def _tick(self):
        dt = 0.033
        self._phase += dt
        self._mouth_phase += dt * (8.0 if self._state == "speaking" else 0.3)

        # Smooth expression transition
        if self._expr_t < 1.0:
            self._expr_t = min(1.0, self._expr_t + dt * 4.0)
        if self._target_expr != self._current_expr and self._expr_t >= 1.0:
            self._current_expr = self._target_expr

        # Smooth mouth toward audio level
        self._level += (self._target_level - self._level) * 0.50
        if self._state != "speaking":
            self._target_level *= 0.55
        target_open = self._level if self._state == "speaking" else 0.0
        self._mouth_open += (target_open - self._mouth_open) * 0.45

        # Blink scheduling
        now = time.time()
        if self._blink_state == "open" and now >= self._next_blink:
            self._blink_state = "closing"
        if self._blink_state == "closing":
            self._blink = min(1.0, self._blink + 0.38)
            if self._blink >= 1.0:
                self._blink_state = "opening"
        elif self._blink_state == "opening":
            self._blink = max(0.0, self._blink - 0.28)
            if self._blink <= 0.0:
                self._blink_state = "open"
                self._next_blink = now + random.uniform(2.5, 6.0)

        # Smooth gaze
        self._gaze_x += (self._target_gaze_x - self._gaze_x) * 0.12
        self._gaze_y += (self._target_gaze_y - self._gaze_y) * 0.12

        self.update()

    # ---- main paint ----
    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)

        s = self._size
        cx = s / 2.0
        cy = s / 2.0

        # Layered motion offsets
        breath = math.sin(self._phase * 1.2) * 0.6        # breathing
        head_tilt = math.sin(self._phase * 0.7) * 0.015   # head tilt radians
        sway = math.sin(self._phase * 0.55) * 1.2         # gentle sway
        bob = math.sin(self._phase * 0.9) * 0.8 + breath  # vertical bob

        # If we have sprites, layer them; else draw everything procedurally
        if self.has_sprites:
            self._paint_layered(p, cx + sway, cy + bob, s, head_tilt)
        else:
            self._paint_full_procedural(p, cx + sway, cy + bob, s, head_tilt)

    # ---- Layered sprite mode (if user drops sprites in assets/avatar/) ----
    def _paint_layered(self, p, cx, cy, s, tilt):
        expr = self._target_expr if self._expr_t >= 1.0 else self._current_expr
        # 1. Hair back
        back = self._pix("hair_back.png") or self._pix(expr + ".png")
        if back:
            self._draw_sprite(p, back, cx, cy - 2, s, 1.05)
        # 2. Neck
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(235, 200, 180))
        neck_w = s * 0.10
        neck_h = s * 0.16
        p.drawRoundedRect(int(cx - neck_w), int(cy + s * 0.20), int(neck_w * 2), int(neck_h), 6, 6)
        # 3. Face base
        base = self._pix("neutral.png") or self._pix(expr + ".png")
        if base:
            self._draw_sprite(p, base, cx, cy, s, 1.0)
        # 4. Eyes (blink overlay)
        if self._blink > 0.05:
            blink = self._pix("blink.png")
            if blink:
                self._draw_sprite(p, blink, cx, cy, s, 1.0, alpha=self._blink)
        # 5. Mouth (open overlay for lip-sync)
        if self._mouth_open > 0.05:
            open_m = self._pix(expr + "_open.png") or self._pix("neutral_open.png")
            if open_m:
                self._draw_sprite(p, open_m, cx, cy, s, 1.0, alpha=min(1.0, self._mouth_open * 1.6))
        # 6. Expression overlay
        if expr != "neutral":
            exp = self._pix(expr + ".png")
            if exp:
                self._draw_sprite(p, exp, cx, cy, s, 1.0, alpha=0.85)

    def _draw_sprite(self, p, pm, cx, cy, s, scale=1.0, alpha=1.0):
        w = int(s * scale)
        h = int(s * scale)
        scaled = pm.scaled(w, h, Qt.AspectRatioMode.KeepAspectRatio,
                           Qt.TransformationMode.SmoothTransformation)
        if alpha < 1.0:
            p.setOpacity(alpha)
        p.drawPixmap(int(cx - w / 2), int(cy - h / 2), scaled)
        if alpha < 1.0:
            p.setOpacity(1.0)

    # ---- Full procedural anime woman (no sprites needed) ----
    def _paint_full_procedural(self, p, cx, cy, s, tilt):
        expr = self._lerp_expr()
        expr_t = self._expr_t  # 0..1 transition progress

        p.translate(cx, cy)
        p.rotate(tilt)
        p.translate(-cx, -cy)

        # === LAYER 1: Hair back (long flowing dark hair) ===
        self._draw_hair_back(p, cx, cy, s, expr)

        # === LAYER 2: Neck ===
        p.setPen(Qt.PenStyle.NoPen)
        neck_w = s * 0.10
        neck_h = s * 0.16
        p.setBrush(QColor(235, 200, 180))
        p.drawRoundedRect(int(cx - neck_w), int(cy + s * 0.20), int(neck_w * 2), int(neck_h), 6, 6)
        # Shoulders hint
        p.setBrush(QColor(245, 228, 220))
        p.drawEllipse(QPointF(cx, cy + s * 0.40), s * 0.32, s * 0.10)

        # === LAYER 3: Face shape (oval anime face) ===
        face_w = s * 0.28
        face_h = s * 0.33
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(255, 228, 212))
        p.drawEllipse(QPointF(cx, cy + s * 0.01), face_w, face_h)
        # Jaw line subtle (chin)
        p.setBrush(QColor(255, 222, 206))
        p.drawEllipse(QPointF(cx, cy + s * 0.10), face_w * 0.78, face_h * 0.55)

        # === LAYER 4: Hair front / bangs ===
        self._draw_hair_front(p, cx, cy, s, expr)

        # === LAYER 5: Eyes (detailed anime eyes) ===
        self._draw_eyes(p, cx, cy, s, expr, expr_t)

        # === LAYER 6: Eyebrows ===
        self._draw_eyebrows(p, cx, cy, s, expr, expr_t)

        # === LAYER 7: Nose ===
        self._draw_nose(p, cx, cy, s)

        # === LAYER 8: Mouth (audio-driven) ===
        self._draw_mouth(p, cx, cy, s, expr)

        # === LAYER 9: Blush ===
        self._draw_blush(p, cx, cy, s, expr)

        # === LAYER 10: Accent glow when active ===
        if self._state in ("listening", "thinking", "speaking"):
            glow = QColor(244, 114, 182, 25 + int(15 * math.sin(self._phase * 2)))
            p.setPen(QPen(glow, 2))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawEllipse(QPointF(cx, cy), s * 0.42, s * 0.45)

        p.resetTransform()

    # ---- Hair back: long flowing dark hair behind head ----
    def _draw_hair_back(self, p, cx, cy, s, expr):
        p.setPen(Qt.PenStyle.NoPen)
        # Base hair color: rich dark brown-black
        base = QColor(45, 28, 55)
        highlight = QColor(80, 50, 95)
        p.setBrush(base)
        # Large oval behind head
        p.drawEllipse(QPointF(cx, cy - s * 0.02), s * 0.35, s * 0.40)
        # Long flowing strands (left and right)
        sway = math.sin(self._phase * 0.6) * 3.0
        for side in (-1, 1):
            path = QPainterPath()
            sx = cx + side * s * 0.22
            path.moveTo(sx, cy - s * 0.20)
            ctrl_x = sx + side * (s * 0.08 + sway * 0.5)
            path.quadTo(ctrl_x, cy + s * 0.15, sx + side * s * 0.04, cy + s * 0.42)
            path.quadTo(ctrl_x + side * s * 0.02, cy + s * 0.35, sx, cy + s * 0.38)
            p.setBrush(highlight if side > 0 else base)
            p.drawPath(path)
        # Hair highlight streak
        p.setBrush(QColor(100, 70, 115))
        p.drawEllipse(QPointF(cx - s * 0.08, cy - s * 0.15), s * 0.08, s * 0.22)

    # ---- Hair front: bangs framing the face ----
    def _draw_hair_front(self, p, cx, cy, s, expr):
        p.setPen(Qt.PenStyle.NoPen)
        base = QColor(50, 30, 60)
        highlight = QColor(90, 60, 105)
        # Main bangs shape
        path = QPainterPath()
        fw = s * 0.31
        fh = s * 0.16
        top_y = cy - s * 0.26
        path.moveTo(cx - fw, top_y + fh * 0.3)
        # Left sweep
        path.quadTo(cx - fw * 0.7, top_y - fh * 0.4, cx - fw * 0.15, top_y - fh * 0.2)
        # Center dip (between eyebrows)
        path.quadTo(cx - fw * 0.05, top_y + fh * 0.1, cx, top_y - fh * 0.05)
        path.quadTo(cx + fw * 0.05, top_y + fh * 0.1, cx + fw * 0.15, top_y - fh * 0.2)
        # Right sweep
        path.quadTo(cx + fw * 0.7, top_y - fh * 0.4, cx + fw, top_y + fh * 0.3)
        path.quadTo(cx + fw * 0.5, top_y + fh * 0.7, cx + fw * 0.15, top_y + fh)
        path.lineTo(cx - fw * 0.15, top_y + fh)
        path.quadTo(cx - fw * 0.5, top_y + fh * 0.7, cx - fw, top_y + fh * 0.3)
        p.setBrush(base)
        p.drawPath(path)
        # Strand details
        for i, off in enumerate([-0.18, -0.06, 0.06, 0.18]):
            sx = cx + s * off
            p.setBrush(highlight if i % 2 == 0 else base)
            p.drawEllipse(QPointF(sx, top_y + s * 0.04), s * 0.015, s * 0.06)
        # Side locks
        for side in (-1, 1):
            sx = cx + side * s * 0.24
            p.setBrush(base)
            p.drawEllipse(QPointF(sx, cy + s * 0.02), s * 0.04, s * 0.18)

    # ---- Anime eyes: white, iris, pupil, highlight, cursor tracking ----
    def _draw_eyes(self, p, cx, cy, s, expr, expr_t):
        eye_dx = s * 0.115
        eye_y = cy + s * 0.02
        eye_w = s * 0.075
        # Blink squashes height; surprised opens wide
        blink_squash = 1.0 - self._blink * 0.92
        surprise_factor = 1.0 + (0.35 if expr == "surprised" else 0.0)
        eye_h = s * 0.11 * blink_squash * self._lerp(1.0, surprise_factor, expr_t if expr == "surprised" else 0.0)
        eye_h = max(1.5, eye_h)

        for side in (-1, 1):
            ex = cx + side * eye_dx

            if eye_h < eye_w * 0.35:
                # Nearly closed — just a line
                p.setPen(QPen(QColor(40, 20, 30), 2))
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawArc(int(ex - eye_w), int(eye_y - 1), int(eye_w * 2), 3, 0, 180 * 16)
                continue

            # White of eye
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(255, 255, 255))
            p.drawEllipse(QPointF(ex, eye_y), eye_w, max(1.0, eye_h))

            # Iris (clipped to eye shape — draw a smaller ellipse)
            iris_w = eye_w * 0.68
            iris_h = eye_h * 0.88
            # Shift iris by gaze (clamped so it stays inside the eye)
            gx = self._gaze_x * eye_w * 0.22
            gy = self._gaze_y * eye_h * 0.15
            iris_cx = ex + max(-eye_w * 0.18, min(eye_w * 0.18, gx))
            iris_cy = eye_y + max(-eye_h * 0.10, min(eye_h * 0.10, gy))

            # Iris color — shifts slightly with expression
            iris_base = QColor(140, 95, 165)  # soft purple
            if expr == "happy":
                iris_base = QColor(120, 130, 170)  # lighter blue-purple
            elif expr == "thinking":
                iris_base = QColor(100, 120, 160)
            elif expr == "sad":
                iris_base = QColor(100, 110, 150)
            p.setBrush(iris_base)
            p.drawEllipse(QPointF(iris_cx, iris_cy), iris_w, max(1.0, iris_h))

            # Pupil
            pupil_r = eye_w * 0.30
            p.setBrush(QColor(18, 10, 25))
            p.drawEllipse(QPointF(iris_cx, iris_cy), pupil_r, max(1.0, pupil_r * 1.1))

            # Eyelid shadow (top gradient)
            p.setPen(Qt.PenStyle.NoPen)
            shadow_w = eye_w * 2
            shadow_h = eye_h * 0.35
            p.setBrush(QColor(60, 30, 50, 50))
            p.drawEllipse(QPointF(ex, eye_y - eye_h * 0.35), shadow_w, shadow_h)

            # Top eyelid line (anime style — thick upper lash)
            p.setPen(QPen(QColor(30, 15, 35), 2.5))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawArc(int(ex - eye_w - 2), int(eye_y - eye_h - 2), int((eye_w + 2) * 2), int(eye_h * 2 + 4),
                      185 * 16, 170 * 16)

            # Bottom lash line
            p.setPen(QPen(QColor(60, 35, 55), 1.2))
            p.drawArc(int(ex - eye_w + 2), int(eye_y + eye_h - 2), int((eye_w - 2) * 2), 8, 10 * 16, 160 * 16)

            # Highlights (anime style — two white dots)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(255, 255, 255, 235))
            # Main highlight (follows gaze slightly)
            hl_x = ex + iris_cx * 0.15 - eye_w * 0.22
            hl_y = eye_y - eye_h * 0.25
            p.drawEllipse(QPointF(hl_x, hl_y), eye_w * 0.13, eye_h * 0.13)
            # Secondary small highlight
            p.setBrush(QColor(255, 255, 255, 180))
            p.drawEllipse(QPointF(hl_x + eye_w * 0.20, hl_y + eye_h * 0.18), eye_w * 0.06, eye_h * 0.06)

    # ---- Eyebrows (expression-driven) ----
    def _draw_eyebrows(self, p, cx, cy, s, expr, expr_t):
        eye_dx = s * 0.115
        brow_y = cy - s * 0.105
        bw = s * 0.075
        thickness = 2.5
        p.setPen(QPen(QColor(55, 30, 50), thickness))
        p.setBrush(Qt.BrushStyle.NoBrush)

        for side in (-1, 1):
            bx = cx + side * eye_dx
            if expr == "sad" or expr == "empathetic":
                # Inner corner raised (sympathetic)
                y1 = brow_y + s * 0.015
                y2 = brow_y - s * 0.005
            elif expr == "thinking":
                # One side raised (left = curious)
                y1 = brow_y - (s * 0.012 if side < 0 else 0)
                y2 = brow_y - (s * 0.002 if side > 0 else 0)
            elif expr == "surprised":
                y1 = brow_y - s * 0.025
                y2 = brow_y - s * 0.020
            elif expr == "happy":
                y1 = brow_y - s * 0.008
                y2 = brow_y - s * 0.005
            else:
                y1 = brow_y
                y2 = brow_y
            p.drawLine(int(bx - bw), int(y1), int(bx + bw), int(y2))

    # ---- Nose (subtle anime dot) ----
    def _draw_nose(self, p, cx, cy, s):
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(220, 175, 160, 120))
        p.drawEllipse(QPointF(cx, cy + s * 0.11), s * 0.012, s * 0.010)

    # ---- Mouth: smooth morphing with audio for lip-sync ----
    def _draw_mouth(self, p, cx, cy, s, expr):
        mouth_y = cy + s * 0.175
        p.setPen(Qt.PenStyle.NoPen)

        open_amt = self._mouth_open
        # Add subtle voice-frequency wobble
        wobble = math.sin(self._mouth_phase * 3.7) * 0.12 + math.sin(self._mouth_phase * 5.3) * 0.08
        open_amt = max(0.0, min(1.0, open_amt + wobble * open_amt))

        mw = s * 0.08   # mouth half-width
        mh_base = s * 0.015
        mh_speech = s * 0.09 * open_amt

        # Expression-specific mouth shapes
        if expr == "happy" and open_amt < 0.15:
            # Smile arc
            p.setPen(QPen(QColor(170, 75, 85), 2.5))
            p.setBrush(QColor(200, 90, 100, 40))
            arc_w = mw * 1.2
            arc_h = mh_base + s * 0.04
            p.drawArc(int(cx - arc_w), int(mouth_y - arc_h), int(arc_w * 2), int(arc_h * 2), 210 * 16, 120 * 16)
        elif expr == "sad":
            # Frown
            p.setPen(QPen(QColor(170, 75, 85), 2))
            p.setBrush(QColor(200, 90, 100, 30))
            arc_w = mw * 0.8
            arc_h = mh_base + s * 0.03
            p.drawArc(int(cx - arc_w), int(mouth_y + s * 0.02), int(arc_w * 2), int(arc_h * 2), 30 * 16, 120 * 16)
        elif expr == "surprised":
            # Small o shape
            p.setBrush(QColor(160, 65, 75))
            p.drawEllipse(QPointF(cx, mouth_y), mw * 0.5, max(1.0, s * 0.035 + mh_speech * 0.5))
        else:
            # Normal / speaking — open ellipse
            p.setBrush(QColor(155, 62, 72))
            mh = mh_base + mh_speech
            # Slight width increase when open (realistic)
            mw_open = mw * (1.0 + open_amt * 0.25)
            p.drawEllipse(QPointF(cx, mouth_y + mh * 0.3), max(1.0, mw_open), max(1.0, mh))

    # ---- Blush (emotion-driven, smooth fade) ----
    def _draw_blush(self, p, cx, cy, s, expr):
        if expr in ("happy", "cheerful", "laughing"):
            alpha = 55 + int(20 * math.sin(self._phase * 1.8))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(244, 140, 160, alpha))
            for side in (-1, 1):
                p.drawEllipse(QPointF(cx + side * s * 0.175, cy + s * 0.105), s * 0.05, s * 0.03)

    # ---- Helpers ----
    def _lerp(self, a, b, t):
        return a + (b - a) * min(1.0, max(0.0, t))

    def _lerp_expr(self):
        """Return interpolated expression name based on transition progress."""
        if self._expr_t >= 1.0:
            return self._target_expr
        return self._current_expr

    # ---- Mouse tracking for eye gaze ----
    def mouseMoveEvent(self, event):
        self._update_gaze(event.position())

    def enterEvent(self, event):
        # QEnterEvent exposes position() in PyQt6 (not pos()).
        self._update_gaze(event.position())

    def _update_gaze(self, pos):
        """Convert mouse position (QPointF) to normalized gaze direction (-1..1)."""
        dx = (pos.x() - self._size / 2) / (self._size / 2)
        dy = (pos.y() - self._size / 2) / (self._size / 2)
        self._target_gaze_x = max(-1.0, min(1.0, dx))
        self._target_gaze_y = max(-1.0, min(1.0, dy))

    def leaveEvent(self, event):
        self._target_gaze_x = 0.0
        self._target_gaze_y = 0.0


# ----------------------------------------------------------------------
# Live2D avatar — real rigged VTuber model (Hiyori) rendered via web runtime
# ----------------------------------------------------------------------
_LIVE2D_HTML = os.path.join(BASE_DIR, "assets", "live2d", "web", "avatar.html")
_LIVE2D_MODEL = os.path.join(BASE_DIR, "assets", "live2d", "web", "hiyori",
                             "hiyori_pro_t11.model3.json")


def _live2d_available() -> bool:
    """True when QtWebEngine loaded early and the model files are present."""
    return bool(
        _WEBENGINE_OK
        and os.path.isfile(_LIVE2D_HTML)
        and os.path.isfile(_LIVE2D_MODEL)
    )


class Live2DAvatarWidget(QWidget):
    """A real rigged Live2D model rendered in an embedded web view.

    Exposes the same public API as the procedural AvatarWidget
    (set_state / set_emotion / set_level) so it's a drop-in replacement.
    Blink, breathing, physics and idle motion run inside the model; Python
    only pushes state, emotion and live voice amplitude for lip-sync.
    """

    def __init__(self, parent=None, size=190, height=None):
        super().__init__(parent)
        self._size = size
        self._height = height or size
        self.setFixedSize(size, self._height)

        from PyQt6.QtWebEngineWidgets import QWebEngineView
        from PyQt6.QtWebEngineCore import QWebEngineSettings
        from PyQt6.QtCore import QUrl

        # Make the host widget itself translucent so nothing paints white behind.
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)

        self._view = QWebEngineView(self)
        self._view.setFixedSize(size, self._height)
        self._view.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self._view.setStyleSheet("background: transparent;")
        # Transparent page background so the card shows through.
        try:
            self._view.page().setBackgroundColor(Qt.GlobalColor.transparent)
        except Exception:
            pass
        s = self._view.settings()
        try:
            s.setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessFileUrls, True)
            s.setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls, True)
            s.setAttribute(QWebEngineSettings.WebAttribute.ShowScrollBars, False)
        except Exception:
            pass

        self._ready = False
        self._pending = []  # JS calls queued until the page loads
        self._state = "standby"
        self._emotion = "calm"
        self._view.loadFinished.connect(self._on_loaded)
        self._view.load(QUrl.fromLocalFile(_LIVE2D_HTML))

    def _on_loaded(self, ok: bool):
        self._ready = bool(ok)
        # Flush queued calls (e.g. state set before the page finished loading).
        for js in self._pending:
            self._run(js)
        self._pending.clear()

    def _run(self, js: str):
        if not self._ready:
            self._pending.append(js)
            return
        try:
            self._view.page().runJavaScript(js)
        except Exception:
            pass

    # ---- public API (matches AvatarWidget) ----
    def set_state(self, state: str):
        self._state = state
        self._run(f"window.setState && window.setState({json.dumps(state)});")

    def set_emotion(self, emotion: str):
        self._emotion = emotion
        self._run(f"window.setEmotion && window.setEmotion({json.dumps(emotion)});")

    def set_level(self, level: float):
        lvl = max(0.0, min(1.0, float(level)))
        self._run(f"window.setAudioLevel && window.setAudioLevel({lvl:.3f});")


def make_avatar(parent=None, size=190, height=None):
    """Return the best available avatar widget.

    Prefers the real rigged Live2D model; falls back to the procedural
    QPainter face if QtWebEngine or the model files are unavailable.
    `height` lets the avatar be taller than wide (portrait framing).
    """
    if _live2d_available():
        try:
            return Live2DAvatarWidget(parent, size=size, height=height)
        except Exception:
            pass
    return AvatarWidget(parent, size=size)


# ----------------------------------------------------------------------
# Activity feed — live "what she's doing" steps during long tasks
# ----------------------------------------------------------------------
class ActivityFeed(QWidget):
    """A small stacked list of the steps Aisha is performing right now.

    Each completed step dims with a check; the current step glows in the accent
    colour with an animated spinner and, once a step runs longer than ~2s, a live
    elapsed-time counter — so even a single slow step (e.g. the model writing a
    long essay) visibly stays alive. Fed by SIGNALS.activity(step, label); 0 clears.
    """

    MAX_ROWS = 5
    SPINNER = ("◐", "◓", "◑", "◒")

    def __init__(self, parent=None):
        super().__init__(parent)
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(10, 4, 10, 4)
        self._layout.setSpacing(3)
        self._rows: list[QLabel] = []
        self._spin = 0
        self._started = 0.0
        self._timer = QTimer(self)
        self._timer.setInterval(140)
        self._timer.timeout.connect(self._tick)

    def _style_current(self, lbl: QLabel):
        lbl.setStyleSheet(
            "QLabel { color:#f9a8d4; font-size:12px; font-weight:600;"
            " font-family:" + HINDI_FONT + "; }"
        )

    def _mark_done(self, lbl: QLabel):
        lbl.setText("✓  " + getattr(lbl, "_raw", lbl.text()))
        lbl.setStyleSheet(
            "QLabel { color:#64748b; font-size:11px; font-weight:400;"
            " font-family:" + HINDI_FONT + "; }"
        )

    def _render_current(self):
        if not self._rows:
            return
        row = self._rows[-1]
        elapsed = time.monotonic() - self._started
        suffix = f"   {int(elapsed)}s" if elapsed >= 2 else ""
        row.setText(f"{self.SPINNER[self._spin]}  {getattr(row, '_raw', '')} …{suffix}")

    def _tick(self):
        if not self._rows:
            self._timer.stop()
            return
        self._spin = (self._spin + 1) % len(self.SPINNER)
        self._render_current()

    def push(self, step: int, label: str):
        if step <= 0 or not label:
            self.clear_feed()
            return
        if self._rows:  # previous current step is now finished
            self._mark_done(self._rows[-1])
        row = QLabel("")
        row.setWordWrap(True)
        row._raw = label  # type: ignore[attr-defined]
        self._style_current(row)
        self._layout.addWidget(row)
        self._rows.append(row)
        while len(self._rows) > self.MAX_ROWS:
            old = self._rows.pop(0)
            old.setParent(None)
            old.deleteLater()
        self._started = time.monotonic()
        self._spin = 0
        self._render_current()
        if not self._timer.isActive():
            self._timer.start()
        self.show()

    def clear_feed(self):
        self._timer.stop()
        for r in self._rows:
            r.setParent(None)
            r.deleteLater()
        self._rows = []
        self.hide()


# ----------------------------------------------------------------------
# Typing indicator — bouncing dots (shown while Aisha "thinks")
# ----------------------------------------------------------------------
class TypingIndicator(QWidget):
    """Three bouncing dots shown while Aisha generates a response."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(20)
        self._phase = 0.0
        self._visible = False
        self._opacity = 0.0

        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(50)

    def show_dots(self):
        self._visible = True
        self.show()

    def hide_dots(self):
        self._visible = False

    def _tick(self):
        self._phase += 0.12
        if self._visible and self._opacity < 1.0:
            self._opacity = min(1.0, self._opacity + 0.06)
        elif not self._visible and self._opacity > 0.0:
            self._opacity = max(0.0, self._opacity - 0.08)
            if self._opacity <= 0.01:
                self.hide()
        self.update()

    def paintEvent(self, event):
        if self._opacity < 0.01:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        p.setOpacity(self._opacity)

        w = self.width() if self.width() > 10 else 200
        self.setFixedWidth(w)
        cx, cy = w / 2, self.height() / 2
        dot_r = 4.5
        spacing = 14

        start_x = cx - (spacing * 1.0)
        for i in range(3):
            offset = math.sin(self._phase + i * 1.1) * 5.0
            x = start_x + i * spacing
            y = cy - 3 + offset
            alpha = int(120 + (math.sin(self._phase + i * 1.1) * 0.5 + 0.5) * 135)
            c = QColor(244, 114, 182, min(255, alpha))
            p.setBrush(QBrush(c))
            p.setPen(Qt.PenStyle.NoPen)
            p.drawEllipse(QRectF(x - dot_r, y - dot_r, dot_r * 2, dot_r * 2))


# ----------------------------------------------------------------------
# Chat Bubble — message with avatar, timestamp, slide-in animation
# ----------------------------------------------------------------------
class ChatBubble(QFrame):
    """A single chat message with role-aware styling, avatar, and animation."""

    def __init__(self, text: str, role: str = "aisha", timestamp: str = "", parent=None):
        super().__init__(parent)
        self.role = role
        self.full_text = text
        self.timestamp = timestamp or _timestamp_now()
        self._opacity = 0.0
        self._slide_x = 30 if role == "aisha" else -30
        self._animating = True

        # Fade-in animation
        self._fade_anim = QPropertyAnimation(self, b"windowOpacity")
        self._fade_anim.setDuration(350)
        self._fade_anim.setStartValue(0.0)
        self._fade_anim.setEndValue(1.0)
        self._fade_anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._fade_anim.start()

        self._build_ui()

    def _build_ui(self):
        is_aisha = self.role == "aisha"

        # Container layout
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 2, 0, 2)
        layout.setSpacing(8)

        # Avatar
        avatar = QLabel()
        avatar.setFixedSize(32, 32)
        avatar.setAlignment(Qt.AlignmentFlag.AlignCenter)
        if is_aisha:
            avatar.setText("🌸")
            avatar.setStyleSheet("""
                QLabel {
                    background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
                        stop:0 #f472b6, stop:1 #c084fc);
                    border-radius: 16px;
                    font-size: 16px;
                }
            """)
        else:
            avatar.setText("👤")
            avatar.setStyleSheet("""
                QLabel {
                    background: #334155;
                    border-radius: 16px;
                    font-size: 15px;
                }
            """)

        # Message bubble
        bubble = QFrame()
        bubble.setObjectName("chatBubble")
        bubble_layout = QVBoxLayout(bubble)
        bubble_layout.setContentsMargins(12, 10, 12, 8)
        bubble_layout.setSpacing(3)

        # Message text
        msg_label = QLabel(self.full_text)
        msg_label.setWordWrap(True)
        msg_label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
        msg_label.setMaximumWidth(300)

        if is_aisha:
            msg_color = Palette.TEXT_AISHA
            bubble_bg = Palette.BUBBLE_AISHA
            border = Palette.BUBBLE_AISHA_BORDER
            font_weight = "400"
        else:
            msg_color = Palette.TEXT_USER
            bubble_bg = Palette.BUBBLE_USER
            border = Palette.BUBBLE_USER_BORDER
            font_weight = "400"

        msg_label.setStyleSheet(f"""
            QLabel {{
                color: {msg_color.name()};
                font-family: 'Segoe UI', sans-serif;
                font-size: 13.5px;
                font-weight: {font_weight};
                line-height: 1.55;
            }}
        """)

        # Timestamp
        ts_label = QLabel(self.timestamp)
        ts_label.setStyleSheet(f"""
            QLabel {{
                color: {Palette.TEXT_TIMESTAMP.name()};
                font-family: 'Segoe UI', sans-serif;
                font-size: 9px;
                font-weight: 400;
            }}
        """)

        bubble_layout.addWidget(msg_label)
        bubble_layout.addWidget(ts_label, 0, Qt.AlignmentFlag.AlignRight if is_aisha else Qt.AlignmentFlag.AlignRight)

        bubble.setStyleSheet(f"""
            QFrame#chatBubble {{
                background: {bubble_bg.name()};
                border: 1px solid {border.name()};
                border-radius: {16 if is_aisha else 16}px;
            }}
        """)

        # Layout direction based on role
        if is_aisha:
            layout.addWidget(avatar)
            layout.addWidget(bubble, 1)
            layout.addStretch()
        else:
            layout.addStretch()
            layout.addWidget(bubble, 1)
            layout.addWidget(avatar)

        # Set overall margins
        self.setContentsMargins(
            4 if is_aisha else 40,
            2,
            40 if is_aisha else 4,
            2
        )


# ----------------------------------------------------------------------
# Chat History Panel — scrollable conversation
# ----------------------------------------------------------------------
class ChatHistoryPanel(QWidget):
    """Scrollable conversation history with smooth auto-scroll."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.bubbles: list[ChatBubble] = []
        self._setup_ui()

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Header with clear button
        header = QHBoxLayout()
        header.setContentsMargins(12, 8, 12, 4)
        header.setSpacing(8)

        title = QLabel("Chat")
        title.setStyleSheet("""
            QLabel {
                color: #94a3b8;
                font-family: 'Segoe UI', sans-serif;
                font-size: 10px;
                font-weight: 700;
                letter-spacing: 1.5px;
                text-transform: uppercase;
            }
        """)
        header.addWidget(title)
        header.addStretch()

        clear_btn = QPushButton("Clear")
        clear_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        clear_btn.setStyleSheet("""
            QPushButton {
                background: transparent;
                color: #64748b;
                font-size: 9px;
                border: none;
                padding: 2px 8px;
                border-radius: 4px;
            }
            QPushButton:hover { color: #f472b6; background: rgba(244,114,182,0.08); }
        """)
        clear_btn.clicked.connect(self.clear_history)
        header.addWidget(clear_btn)
        layout.addLayout(header)

        # Divider
        divider = QFrame()
        divider.setFrameShape(QFrame.Shape.HLine)
        divider.setStyleSheet("background: rgba(255,255,255,0.04); max-height: 1px;")
        layout.addWidget(divider)

        # Scroll area
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        scroll.setMinimumHeight(60)
        scroll.setMaximumHeight(380)
        scroll.setStyleSheet("""
            QScrollArea {
                background: transparent;
                border: none;
            }
            QScrollBar:vertical {
                background: rgba(255,255,255,0.02);
                width: 3px;
                border-radius: 2px;
            }
            QScrollBar::handle:vertical {
                background: rgba(244, 114, 182, 0.25);
                border-radius: 2px;
                min-height: 20px;
            }
            QScrollBar::handle:vertical:hover {
                background: rgba(244, 114, 182, 0.45);
            }
            QScrollBar::add-line, QScrollBar::sub-line {
                height: 0;
            }
        """)

        container = QWidget()
        self.bubbles_layout = QVBoxLayout(container)
        self.bubbles_layout.setContentsMargins(8, 6, 8, 6)
        self.bubbles_layout.setSpacing(6)
        self.bubbles_layout.addStretch()

        scroll.setWidget(container)
        layout.addWidget(scroll)

    def add_message(self, text: str, role: str = "aisha", timestamp: str = ""):
        bubble = ChatBubble(text, role, timestamp or _timestamp_now())
        self.bubbles.append(bubble)
        self.bubbles_layout.insertWidget(self.bubbles_layout.count() - 1, bubble)

        # Auto-scroll to bottom
        QTimer.singleShot(50, self._scroll_to_bottom)

    def _scroll_to_bottom(self):
        # Find the scroll area parent and scroll down
        parent = self.parent()
        while parent:
            if isinstance(parent, QScrollArea):
                parent.verticalScrollBar().setValue(
                    parent.verticalScrollBar().maximum()
                )
                break
            parent = parent.parent()

    def clear_history(self):
        for bubble in self.bubbles:
            bubble.deleteLater()
        self.bubbles.clear()


# ----------------------------------------------------------------------
# Quick Actions Bar — one-tap shortcuts
# ----------------------------------------------------------------------
class QuickActions(QWidget):
    """Row of quick-action buttons for common tasks."""

    def __init__(self, assistant_instance=None, parent=None):
        super().__init__(parent)
        self.assistant = assistant_instance
        self._setup_ui()

    def _setup_ui(self):
        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 4, 8, 4)
        layout.setSpacing(4)

        actions = [
            ("📷 Camera", "take_photo"),
            ("🌤 Weather", "weather Delhi"),
            ("⏰ Timer", "set a timer for 5 minutes"),
            ("🔍 Search", "search_web latest news"),
            ("📁 Files", "open my Downloads folder"),
        ]

        for label, command in actions:
            btn = QPushButton(label)
            btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
            btn.setFixedHeight(28)
            btn.setStyleSheet("""
                QPushButton {
                    background: rgba(255, 255, 255, 0.03);
                    color: #94a3b8;
                    border: 1px solid rgba(255, 255, 255, 0.05);
                    border-radius: 8px;
                    padding: 4px 10px;
                    font-family: 'Segoe UI', sans-serif;
                    font-size: 10px;
                    font-weight: 500;
                }
                QPushButton:hover {
                    background: rgba(244, 114, 182, 0.1);
                    color: #f472b6;
                    border-color: rgba(244, 114, 182, 0.2);
                }
                QPushButton:pressed {
                    background: rgba(244, 114, 182, 0.18);
                }
            """)
            btn.clicked.connect(lambda checked, cmd=command: self._on_action(cmd))
            layout.addWidget(btn)

    def _on_action(self, command: str):
        if self.assistant and hasattr(self.assistant, 'process_query'):
            import threading
            threading.Thread(
                target=self.assistant.process_query,
                args=(command,),
                daemon=True
            ).start()


# ----------------------------------------------------------------------
# Main Card — alive, expressive, full-featured
# ----------------------------------------------------------------------
class AishaCard(QWidget):
    """Main overlay card — expressive, alive, with full chat experience."""

    def __init__(self, assistant_instance=None):
        super().__init__()
        self.assistant = assistant_instance
        self._drag_pos = None
        self._current_state = "standby"
        self._current_emotion = "calm"
        self._history_visible = False
        self._muted = False
        self._idle_breath_phase = 0.0
        self._card_glow = 0.0
        self._target_card_glow = 0.0

        self._setup_window()
        self._build_ui()
        self._connect_signals()
        self._animate_in()
        self._start_idle_animation()

    def _setup_window(self):
        """Card IS the window — no full-screen parent = no layered window errors."""
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint |
            Qt.WindowType.WindowStaysOnTopHint |
            Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)

    def _build_ui(self):
        # ---- Window-level layout — this sizes and positions the card ----
        win_layout = QVBoxLayout(self)
        win_layout.setContentsMargins(0, 0, 0, 0)
        win_layout.setSpacing(0)

        # ---- Inner card frame (the visible rounded rectangle) ----
        self.card = QFrame(self)
        self.card.setObjectName("aishaCard")
        self.card.setMinimumWidth(460)

        # A bounded child layer keeps ambient painting inside the card instead
        # of creating an unmanaged widget at the window's top-left corner.
        self.particles = AmbientParticles(self.card)
        self.particles.lower()

        inner = QVBoxLayout(self.card)
        inner.setContentsMargins(16, 10, 16, 12)
        inner.setSpacing(4)

        # ---- Header: identity/status on the left, window controls on the right ----
        self.header = QFrame(self.card)
        self.header.setObjectName("aishaHeader")
        top_row = QHBoxLayout(self.header)
        top_row.setContentsMargins(2, 2, 2, 8)
        top_row.setSpacing(8)

        identity = QVBoxLayout()
        identity.setContentsMargins(0, 0, 0, 0)
        identity.setSpacing(1)

        self.brand_label = QLabel("AISHA")
        self.brand_label.setStyleSheet("""
            QLabel {
                color: #fce7f3;
                font-size: 13px;
                font-weight: 700;
                letter-spacing: 2px;
                font-family: """ + UI_FONT + """;
            }
        """)
        identity.addWidget(self.brand_label)

        self.status_label = QLabel("Ready")
        self.status_label.setStyleSheet("""
            QLabel {
                color: #64748b;
                font-size: 10px;
                font-weight: 500;
                font-family: 'Segoe UI', sans-serif;
            }
        """)
        identity.addWidget(self.status_label)

        self.emotion_label = QLabel("")
        self.emotion_label.setStyleSheet("""
            QLabel {
                color: #c084fc;
                font-size: 9px;
                font-weight: 500;
                font-family: 'Segoe UI', sans-serif;
            }
        """)
        identity.addWidget(self.emotion_label)
        top_row.addLayout(identity)
        top_row.addStretch()

        self.mute_btn = QPushButton("Mic")
        self.mute_btn.setFixedSize(52, 24)
        self.mute_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.mute_btn.setCheckable(True)
        self.mute_btn.clicked.connect(self._toggle_mute)
        self.mute_btn.setStyleSheet("""
            QPushButton {
                background: rgba(255,255,255,0.06);
                color: #94a3b8;
                border: none;
                border-radius: 6px;
                font-size: 10px;
                font-weight: 600;
                font-family: 'Segoe UI', sans-serif;
            }
            QPushButton:hover { background: rgba(244,114,182,0.15); color: #f472b6; }
            QPushButton:checked { background: rgba(239,68,68,0.2); color: #ef4444; }
        """)
        top_row.addWidget(self.mute_btn)

        self.hide_btn = QPushButton("Hide")
        self.hide_btn.setFixedSize(36, 24)
        self.hide_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.hide_btn.clicked.connect(self.hide)
        self.hide_btn.setStyleSheet("""
            QPushButton {
                background: rgba(255,255,255,0.06);
                color: #94a3b8;
                border: none;
                border-radius: 6px;
                font-size: 10px;
                font-weight: 600;
            }
            QPushButton:hover { background: rgba(244,114,182,0.15); color: #f472b6; }
        """)
        top_row.addWidget(self.hide_btn)

        close_btn = QPushButton("X")
        close_btn.setFixedSize(24, 24)
        close_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        close_btn.clicked.connect(self._close_overlay)
        close_btn.setStyleSheet("""
            QPushButton {
                background: rgba(255,255,255,0.06);
                color: #64748b;
                border: none;
                border-radius: 6px;
                font-size: 11px;
                font-weight: 700;
            }
            QPushButton:hover { background: rgba(239,68,68,0.25); color: #ef4444; }
        """)
        top_row.addWidget(close_btn)

        inner.addWidget(self.header)

        header_divider = QFrame(self.card)
        header_divider.setFrameShape(QFrame.Shape.HLine)
        header_divider.setFixedHeight(1)
        header_divider.setStyleSheet("background: rgba(255,255,255,0.07); border: none;")
        inner.addWidget(header_divider)

        self._drag_surfaces = (self.header, self.brand_label, self.status_label)
        for surface in self._drag_surfaces:
            surface.installEventFilter(self)

        # ---- Avatar (real Live2D model if available, else procedural face) ----
        self.avatar = make_avatar(size=300, height=360)
        avatar_row = QHBoxLayout()
        avatar_row.setContentsMargins(0, 2, 0, 2)
        avatar_row.addStretch()
        avatar_row.addWidget(self.avatar)
        avatar_row.addStretch()
        inner.addLayout(avatar_row)

        # ---- Waveform ----
        self.waveform = AnimatedWaveform()
        self.waveform.set_state("standby")

        wave_row = QHBoxLayout()
        wave_row.setSpacing(0)
        wave_row.addStretch()
        wave_row.addWidget(self.waveform)
        wave_row.addStretch()
        inner.addLayout(wave_row)

        # ---- Mic level meter ----
        self.mic_meter = MicLevelMeter()
        self.mic_meter.setFixedHeight(4)
        self.mic_meter.set_state("standby")
        inner.addWidget(self.mic_meter)

        # ---- Conversation starters (shown when idle) ----
        self.starters = ConversationStarters(assistant_instance=self.assistant)
        self.starters.hide()
        inner.addWidget(self.starters)

        # ---- Typing indicator ----
        self.typing_indicator = TypingIndicator()
        self.typing_indicator.hide()
        inner.addWidget(self.typing_indicator)

        # ---- Live activity feed (what she's doing during long/multi-step tasks) ----
        self.activity_feed = ActivityFeed()
        self.activity_feed.hide()
        inner.addWidget(self.activity_feed)

        # ---- Reply display with a dedicated label/action hierarchy ----
        self.reply_container = QFrame(self.card)
        reply_layout = QVBoxLayout(self.reply_container)
        reply_layout.setContentsMargins(0, 2, 0, 0)
        reply_layout.setSpacing(5)

        reply_header = QHBoxLayout()
        reply_header.setContentsMargins(4, 0, 4, 0)
        self.reply_role_label = QLabel("AISHA")
        self.reply_role_label.setStyleSheet("""
            QLabel {
                color: #94a3b8;
                font-size: 9px;
                font-weight: 700;
                letter-spacing: 1px;
            }
        """)
        reply_header.addWidget(self.reply_role_label)
        reply_header.addStretch()

        self.copy_btn = QPushButton("Copy")
        self.copy_btn.setFixedSize(46, 24)
        self.copy_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.copy_btn.setToolTip("Copy Aisha's reply")
        self.copy_btn.clicked.connect(self._copy_reply)
        self.copy_btn.setStyleSheet("""
            QPushButton {
                background: rgba(244,114,182,0.16);
                color: #fce7f3;
                border: 1px solid rgba(244,114,182,0.35);
                border-radius: 7px;
                font-size: 10px;
                font-weight: 700;
                font-family: 'Segoe UI', sans-serif;
            }
            QPushButton:hover {
                background: rgba(244,114,182,0.28);
                color: #ffffff;
                border-color: rgba(244,114,182,0.55);
            }
            QPushButton:pressed { background: rgba(244,114,182,0.38); }
        """)
        self.copy_btn.hide()
        reply_header.addWidget(self.copy_btn)
        reply_layout.addLayout(reply_header)

        self.reply_label = QLabel("")
        self.reply_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.reply_label.setWordWrap(True)
        self.reply_label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
        self.reply_label.setMaximumWidth(428)
        self._apply_reply_style(Palette.BUBBLE_AISHA, Palette.TEXT_AISHA, Palette.BUBBLE_AISHA_BORDER)
        reply_layout.addWidget(self.reply_label)

        self.reply_container.hide()
        inner.addWidget(self.reply_container)

        # ---- Quick actions ----
        self.quick_actions = QuickActions(assistant_instance=self.assistant)
        self.quick_actions.hide()
        inner.addWidget(self.quick_actions)

        # ---- Text input bar (type or speak) ----
        input_row = QHBoxLayout()
        input_row.setContentsMargins(6, 0, 6, 0)
        input_row.setSpacing(6)

        self.text_input = QLineEdit()
        self.text_input.setPlaceholderText("Type or speak...")
        self.text_input.setFixedHeight(32)
        self.text_input.setClearButtonEnabled(True)
        self.text_input.returnPressed.connect(self._on_text_submit)
        self.text_input.setStyleSheet("""
            QLineEdit {
                background: rgba(255, 255, 255, 0.04);
                color: #e2e8f0;
                border: 1px solid rgba(255, 255, 255, 0.06);
                border-radius: 10px;
                padding: 0 12px;
                font-family: """ + HINDI_FONT + """;
                font-size: 13.5px;
            }
            QLineEdit:focus {
                border-color: rgba(244, 114, 182, 0.4);
                background: rgba(255, 255, 255, 0.06);
            }
            QLineEdit::placeholder {
                color: #475569;
            }
        """)

        send_btn = QPushButton("➤")
        send_btn.setFixedSize(32, 32)
        send_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        send_btn.setToolTip("Send message")
        send_btn.clicked.connect(self._on_text_submit)
        send_btn.setStyleSheet("""
            QPushButton {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
                    stop:0 #f472b6, stop:1 #c084fc);
                color: #fff;
                border: none;
                border-radius: 10px;
                font-size: 14px;
                font-weight: 700;
            }
            QPushButton:hover {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
                    stop:0 #f472b6, stop:1 #c084fc);
                opacity: 0.9;
            }
            QPushButton:pressed {
                opacity: 0.7;
            }
        """)

        input_row.addWidget(self.text_input, 1)
        input_row.addWidget(send_btn, 0, Qt.AlignmentFlag.AlignVCenter)

        self.input_container = QWidget()
        self.input_container.setLayout(input_row)
        self.input_container.hide()
        inner.addWidget(self.input_container)

        # ---- Toggle row ----
        toggle_row = QHBoxLayout()
        toggle_row.setContentsMargins(0, 2, 0, 0)
        toggle_row.setSpacing(6)

        self.actions_btn = QPushButton("Quick")
        self.actions_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.actions_btn.clicked.connect(self._toggle_actions)

        self._apply_toggle_style()

        toggle_row.addStretch()
        toggle_row.addWidget(self.actions_btn)
        inner.addLayout(toggle_row)

        # ---- Card style — deep warm dark with border-based depth (shadow removed for Windows compat) ----
        self.card.setStyleSheet("""
            QFrame#aishaCard {
                background-color: #14141c;
                border: 1px solid rgba(255, 255, 255, 0.08);
                border-radius: 18px;
            }
        """)

        # The visible card must belong to the window layout before geometry is
        # measured. Without this, the top-level layout has no size hint.
        win_layout.addWidget(self.card)
        inner.activate()
        win_layout.activate()
        self.adjustSize()

    def _close_overlay(self):
        """Minimize to tray — don't quit the app."""
        self.hide()
        try:
            SIGNALS.state_changed.emit("idle")
        except Exception:
            pass

    def _apply_status_style(self, color: QColor):
        self.status_label.setStyleSheet(f"""
            QLabel {{
                color: {color.name()};
                font-family: 'Segoe UI', sans-serif;
                font-size: 10px;
                font-weight: 700;
                letter-spacing: 1.0px;
                text-transform: uppercase;
            }}
        """)

    def _apply_reply_style(self, bg: QColor, text_color: QColor, border: QColor):
        # Aisha's replies are Devanagari — render them in the crisp bundled Hindi
        # font, slightly larger with generous line-height for readability.
        self.reply_label.setStyleSheet(f"""
            QLabel {{
                color: {text_color.name()};
                font-family: {HINDI_FONT};
                font-size: 16px;
                font-weight: 500;
                line-height: 1.7;
                padding: 12px 18px;
                background: {bg.name()};
                border-radius: 14px;
                border: 1px solid {border.name()};
            }}
        """)

    def _apply_toggle_style(self):
        for btn in [self.actions_btn]:
            btn.setStyleSheet("""
                QPushButton {
                    background: transparent;
                    color: #64748b;
                    font-family: 'Segoe UI', sans-serif;
                    font-size: 9px;
                    font-weight: 600;
                    border: none;
                    padding: 4px 10px;
                    border-radius: 6px;
                }
                QPushButton:hover {
                    color: #f472b6;
                    background: rgba(244, 114, 182, 0.07);
                }
            """)

    def _position(self, force_recenter=False):
        """Fit the card and keep its current position unless explicitly recentered."""
        if self.layout():
            self.layout().activate()
        if self.card.layout():
            self.card.layout().activate()
        self.card.adjustSize()
        self.adjustSize()
        size = self.sizeHint().expandedTo(self.minimumSizeHint())
        w = max(self.card.minimumWidth(), size.width())
        h = max(1, size.height())

        screen = QGuiApplication.screenAt(self.frameGeometry().center()) or QApplication.primaryScreen()
        available = screen.availableGeometry()

        if force_recenter or not hasattr(self, "_user_x"):
            x = available.left() + max(0, (available.width() - w) // 2)
            y = available.top() + max(0, available.height() - h - 36)
        else:
            x, y = self.x(), self.y()

        max_x = max(available.left(), available.right() - w + 1)
        max_y = max(available.top(), available.bottom() - h + 1)
        x = min(max(x, available.left()), max_x)
        y = min(max(y, available.top()), max_y)
        self.setGeometry(x, y, w, h)
        self._user_x, self._user_y = x, y
        self._sync_particles_geometry()

    def _animate_in(self):
        # Show first so Qt computes layout sizes
        self.show()
        QApplication.processEvents()
        # Now position based on actual computed size
        self._position(force_recenter=True)
        # Layout/particles can still settle a beat after show(); re-clamp so the
        # docked bar can't end up hanging off the bottom of the screen.
        QTimer.singleShot(120, self._clamp_to_screen)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        QTimer.singleShot(0, self._sync_particles_geometry)
        # The card grows as content/animations appear after the initial position was
        # computed. Without re-clamping, that extra height pushes the bottom edge off
        # the screen. Pull the window fully back into the visible area after any resize.
        QTimer.singleShot(0, self._clamp_to_screen)

    def _clamp_to_screen(self):
        """Keep the whole window inside the current screen's available area.

        Only ever pulls the window back on-screen; if it already fits, this is a
        no-op, so it never fights a user drag that stays within bounds.
        """
        screen = QGuiApplication.screenAt(self.frameGeometry().center()) or QApplication.primaryScreen()
        if not screen:
            return
        available = screen.availableGeometry()
        w, h = self.width(), self.height()
        x, y = self.x(), self.y()
        max_x = max(available.left(), available.right() - w + 1)
        max_y = max(available.top(), available.bottom() - h + 1)
        nx = min(max(x, available.left()), max_x)
        ny = min(max(y, available.top()), max_y)
        if (nx, ny) != (x, y):
            self.move(nx, ny)
            self._user_x, self._user_y = nx, ny

    def _sync_particles_geometry(self):
        if hasattr(self, "particles") and hasattr(self, "card"):
            self.particles.setGeometry(self.card.rect())
            self.particles.lower()

    def _start_idle_animation(self):
        """Breathing idle animation — subtle card glow pulsing."""
        self.idle_timer = QTimer(self)
        self.idle_timer.timeout.connect(self._idle_tick)
        self.idle_timer.start(80)

    def _idle_tick(self):
        if self._current_state in ("standby", "idle"):
            self._idle_breath_phase += 0.03
            breath = math.sin(self._idle_breath_phase) * 0.5 + 0.5
            self._card_glow += (breath * 0.3 - self._card_glow) * 0.05
            self._update_card_glow()

    def _update_card_glow(self):
        """Pulse the card border color based on idle breath."""
        glow_alpha = int(self._card_glow * 120)
        glow_color = QColor(244, 114, 182, glow_alpha)
        if self._card_glow > 0.02:
            border_alpha = min(255, 8 + glow_alpha)
            border_color = QColor(244, 114, 182, border_alpha)
            self.card.setStyleSheet(f"""
                QFrame#aishaCard {{
                    background-color: #14141c;
                    border: 1px solid {border_color.name()};
                    border-radius: 18px;
                }}
            """)
        else:
            self.card.setStyleSheet("""
                QFrame#aishaCard {
                    background-color: #14141c;
                    border: 1px solid rgba(255, 255, 255, 0.08);
                    border-radius: 18px;
                }
            """)

    # ---- Drag ----
    def eventFilter(self, watched, event):
        if watched in getattr(self, "_drag_surfaces", ()):
            if event.type() == QEvent.Type.MouseButtonPress and event.button() == Qt.MouseButton.LeftButton:
                self._begin_drag(event.globalPosition().toPoint())
                return True
            if event.type() == QEvent.Type.MouseMove and event.buttons() & Qt.MouseButton.LeftButton:
                self._drag_window(event.globalPosition().toPoint())
                return True
            if event.type() == QEvent.Type.MouseButtonRelease:
                self._finish_drag()
                return True
        return super().eventFilter(watched, event)

    def _begin_drag(self, global_position: QPoint):
        self._drag_pos = global_position - self.frameGeometry().topLeft()
        self.setCursor(QCursor(Qt.CursorShape.ClosedHandCursor))

    def _drag_window(self, global_position: QPoint):
        if self._drag_pos is None:
            return
        target = global_position - self._drag_pos
        screen = QGuiApplication.screenAt(global_position) or QApplication.primaryScreen()
        available = screen.availableGeometry()
        max_x = max(available.left(), available.right() - self.width() + 1)
        max_y = max(available.top(), available.bottom() - self.height() + 1)
        x = min(max(target.x(), available.left()), max_x)
        y = min(max(target.y(), available.top()), max_y)
        self.move(x, y)
        self._user_x, self._user_y = x, y

    def _finish_drag(self):
        self._drag_pos = None
        self.unsetCursor()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._begin_drag(event.globalPosition().toPoint())
            event.accept()

    def mouseMoveEvent(self, event):
        if event.buttons() == Qt.MouseButton.LeftButton and self._drag_pos is not None:
            self._drag_window(event.globalPosition().toPoint())
            event.accept()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._finish_drag()
            event.accept()

    # ---- Signals ----
    def _connect_signals(self):
        SIGNALS.state_changed.connect(self.on_state_changed)
        SIGNALS.user_speech.connect(self.on_user_speech)
        SIGNALS.aisha_reply.connect(self.on_aisha_reply)
        SIGNALS.audio_level.connect(self._on_audio_level)
        SIGNALS.emotion_changed.connect(self.on_emotion_changed)
        SIGNALS.activity.connect(self._on_activity)

    def _on_activity(self, step: int, label: str):
        """Live step feed from the agent's tool loop."""
        self.activity_feed.push(step, label)
        # Growing the feed can change height; keep the window fully on-screen.
        QTimer.singleShot(0, self._clamp_to_screen)

    def on_state_changed(self, state: str):
        self._current_state = state
        self.waveform.set_state(state)
        self.particles.set_state(state)
        self.avatar.set_state(state)

        labels = {
            "loading": "Loading",
            "listening": "Listening",
            "thinking": "Thinking",
            "speaking": "Speaking",
            "muted": "Muted",
            "standby": "Aisha ready",
            "idle": "Sleeping",
        }
        text = labels.get(state, "...")
        color = Palette.state_fg(state)
        self.status_label.setText(text)
        self._apply_status_style(color)

        # Show typing indicator during thinking/speaking
        if state == "thinking":
            self.typing_indicator.show_dots()
            self.typing_indicator.show()
        else:
            self.typing_indicator.hide_dots()

        # Glow intensity based on state
        glow_map = {
            "listening": 0.5,
            "speaking": 0.4,
            "thinking": 0.3,
            "loading": 0.2,
        }
        self._target_card_glow = glow_map.get(state, 0.0)

        # Clear the step feed once she goes back to rest/listening.
        if state in ("standby", "idle", "listening"):
            self.activity_feed.clear_feed()

        # Show conversation starters in standby, hide during active states
        if state in ("standby", "idle") and not self._history_visible:
            self.starters.show()
        else:
            self.starters.hide()

        # Show text input when active, hide when idle/standby
        if state in ("listening", "thinking", "speaking") or self._history_visible:
            self.input_container.show()
        else:
            self.input_container.hide()

        self.mic_meter.set_state(state)
        # DON'T call _position() here — it re-centers the window and undoes user drag

    def on_emotion_changed(self, emotion: str):
        self._current_emotion = emotion
        self.waveform.set_emotion(emotion)
        self.particles.set_pulse(0.6)
        self.avatar.set_emotion(emotion)

        emotion_icons = {
            "happy": "~ blushing",
            "excited": "~ excited",
            "calm": "~ calm",
            "thinking": "~ hmm...",
            "warm": "~ warm",
            "playful": "~ playful",
        }
        self.emotion_label.setText(emotion_icons.get(emotion, ""))
        self._target_card_glow = max(self._target_card_glow, 0.4)
        # Don't reposition — user may have dragged the window

    def on_user_speech(self, text: str):
        cleaned = clean_for_ui(text)
        if not cleaned:
            return

        # Show in reply area
        self.reply_container.show()
        self.reply_role_label.setText("YOU")
        self._apply_reply_style(Palette.BUBBLE_USER, Palette.TEXT_USER, Palette.BUBBLE_USER_BORDER)
        self.reply_label.setText(f"you: {cleaned}")
        self.copy_btn.hide()
        # Don't reposition — user may have dragged the window

        # Hide conversation starters during active chat
        self.starters.hide()

    def on_aisha_reply(self, text: str):
        cleaned = clean_for_ui(text)
        if not cleaned:
            return

        # Hide typing indicator
        self.typing_indicator.hide_dots()

        # Show reply with typing animation
        self.reply_container.show()
        self.reply_role_label.setText("AISHA")
        self._apply_reply_style(Palette.BUBBLE_AISHA, Palette.TEXT_AISHA, Palette.BUBBLE_AISHA_BORDER)
        self.copy_btn.show()

        # Character-by-character typing effect
        self._typing_full_text = cleaned
        self._typing_index = 0
        self._typing_timer = QTimer(self)
        self._typing_timer.timeout.connect(self._typing_step)
        self._typing_timer.start(22)
        self.reply_label.setText("")

        # Detect emotion
        emotion = self._detect_emotion(cleaned)
        self.on_emotion_changed(emotion)
        # Don't reposition — user may have dragged the window

    def _typing_step(self):
        if self._typing_index < len(self._typing_full_text):
            self._typing_index += 1
            display = self._typing_full_text[:self._typing_index] + " ▌"
            self.reply_label.setText(display)
        else:
            self._typing_timer.stop()
            self.reply_label.setText(self._typing_full_text)

    def _detect_emotion(self, text: str) -> str:
        text_lower = text.lower()
        if any(w in text_lower for w in ["वाह", "अरे वाह", "बिल्कुल mast", "क्या बात है", "जबरदस्त", "वाह बोल्ड"]):
            return "happy"
        if any(w in text_lower for w in ["रुको", "सोच रही", "देखती हूँ", "लो", "बस हो"]):
            return "thinking"
        if any(w in text_lower for w in ["सुन रही", "क्या बोलो", "बताओ", "हाँ", "अच्छा"]):
            return "calm"
        return "warm"

    def _on_text_submit(self):
        """Send typed text to Aisha."""
        text = self.text_input.text().strip()
        if not text:
            return
        self.text_input.clear()
        self.text_input.clearFocus()
        if self.assistant and hasattr(self.assistant, 'process_query'):
            import threading
            threading.Thread(
                target=self.assistant.process_query,
                args=(text,),
                daemon=True
            ).start()

    def _on_audio_level(self, level: float):
        self.waveform.set_audio_level(level)
        self.mic_meter.set_level(level)
        self.avatar.set_level(level)  # lip-sync

    def _copy_reply(self):
        """Copy Aisha's last reply to clipboard."""
        text = self.reply_label.text().replace(" ▌", "")
        if text:
            clipboard = QApplication.clipboard()
            clipboard.setText(text)
            # Flash feedback
            self.copy_btn.setText("✓")
            self.copy_btn.setStyleSheet("""
                QPushButton {
                    background: rgba(52, 211, 153, 0.15);
                    color: #34d399;
                    border: 1px solid rgba(52, 211, 153, 0.25);
                    border-radius: 6px;
                    font-size: 9px;
                    font-weight: 600;
                    font-family: 'Segoe UI', sans-serif;
                }
            """)
            QTimer.singleShot(1200, self._reset_copy_btn)

    def _reset_copy_btn(self):
        self.copy_btn.setText("Copy")
        self.copy_btn.setStyleSheet("""
            QPushButton {
                background: rgba(244,114,182,0.16);
                color: #fce7f3;
                border: 1px solid rgba(244,114,182,0.35);
                border-radius: 7px;
                font-size: 10px;
                font-weight: 700;
                font-family: 'Segoe UI', sans-serif;
            }
            QPushButton:hover {
                background: rgba(244,114,182,0.28);
                color: #ffffff;
                border-color: rgba(244,114,182,0.55);
            }
            QPushButton:pressed { background: rgba(244,114,182,0.38); }
        """)

    def _toggle_mute(self):
        """Toggle listening mute — pause/resume voice input."""
        self._muted = not self._muted
        # Keep the label as plain text (not an emoji) so it stays consistent with
        # the other header controls; the checked/red state conveys "muted".
        self.mute_btn.setChecked(self._muted)
        if self._muted:
            self.mute_btn.setText("Muted")
            self.mute_btn.setToolTip("Unmute — resume listening")
            self.mic_meter.set_state("idle")
            # Notify assistant to pause listener
            if self.assistant and hasattr(self.assistant, 'listener_pause'):
                self.assistant.listener_pause.set()
        else:
            self.mute_btn.setText("Mic")
            self.mute_btn.setToolTip("Mute — pause listening")
            self.mic_meter.set_state(self._current_state)
            if self.assistant and hasattr(self.assistant, 'listener_pause'):
                self.assistant.listener_pause.clear()

    def _toggle_actions(self):
        if self.quick_actions.isVisible():
            self.quick_actions.hide()
            self.actions_btn.setText("Quick")
        else:
            self.quick_actions.show()
            self.actions_btn.setText("Hide")
        QTimer.singleShot(0, self._position)


# ----------------------------------------------------------------------
# System Tray
# ----------------------------------------------------------------------
class AishaTray(QSystemTrayIcon):
    def __init__(self, card: AishaCard, app: QApplication):
        super().__init__()
        self.card = card
        self.app = app
        self._update_info = None
        self._create_icon()
        self._build_menu()
        self.show()
        try:
            SIGNALS.update_available.connect(self._on_update_available)
        except Exception:
            pass

    def _on_update_available(self, info):
        """A newer release exists — notify the user via a tray balloon."""
        self._update_info = info
        try:
            ver = info.get("version", "?")
            self.showMessage(
                "Aisha update available",
                f"Version {ver} is ready. Right-click the tray icon → 'Update Aisha' to install.",
                QSystemTrayIcon.MessageIcon.Information,
                8000,
            )
            self._build_menu()  # rebuild so the "Update Aisha" item appears
        except Exception:
            pass

    def _do_update(self):
        info = self._update_info
        if not info:
            return
        try:
            import updater
            self.showMessage("Aisha", "Downloading update… she'll restart when ready.",
                             QSystemTrayIcon.MessageIcon.Information, 5000)
            if updater.apply_update(info):
                # Swap scheduled — exit so the batch can replace files in place.
                self.app.quit()
        except Exception:
            pass

    def _create_icon(self):
        pix = QPixmap(32, 32)
        pix.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pix)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        # Soft gradient background
        bg = QRadialGradient(16, 16, 14)
        bg.setColorAt(0, QColor(50, 40, 60))
        bg.setColorAt(1, QColor(20, 20, 28))
        painter.setBrush(QBrush(bg))
        painter.setPen(QPen(QColor(244, 114, 182, 100), 1))
        painter.drawEllipse(1, 1, 30, 30)

        # Inner accent ring
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(QColor(244, 114, 182), 1.5))
        painter.drawEllipse(5, 5, 22, 22)

        # Center accent dot
        painter.setBrush(QBrush(QColor("#f472b6")))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawEllipse(13, 13, 6, 6)

        painter.end()
        self.setIcon(QIcon(pix))
        self.setToolTip("🌸 Aisha AI")

    def _build_menu(self):
        menu = QMenu()
        menu.setStyleSheet("""
            QMenu {
                background-color: #14141c;
                color: #f1f5f9;
                border: 1px solid rgba(255, 255, 255, 0.08);
                border-radius: 12px;
                padding: 6px;
            }
            QMenu::item {
                padding: 8px 20px;
                border-radius: 6px;
                font-size: 11px;
                font-family: 'Segoe UI', sans-serif;
            }
            QMenu::item:selected {
                background-color: rgba(244, 114, 182, 0.15);
                color: #f472b6;
            }
            QMenu::separator {
                height: 1px;
                background: rgba(255, 255, 255, 0.05);
                margin: 4px 8px;
            }
        """)

        actions = [
            ("🎤 Mute / Unmute", self._tray_toggle_mute),
            ("⏸ Pause Listening", self._tray_toggle_pause),
            ("📋 Copy Last Reply", self._tray_copy_reply),
            ("🔄 Recalibrate Mic", self._recalibrate),
            ("🗑 Reset Memory", self._reset_memory),
            ("🔧 Run Diagnostics", self._run_diag),
            ("📂 Open App Folder", self._open_folder),
        ]
        for label, handler in actions:
            a = menu.addAction(label)
            a.triggered.connect(handler)

        # Show an "Update" item only when a newer release has been detected.
        if getattr(self, "_update_info", None):
            menu.addSeparator()
            ver = self._update_info.get("version", "")
            up = menu.addAction(f"⬆ Update Aisha (v{ver})")
            up.triggered.connect(self._do_update)

        menu.addSeparator()
        quit_a = menu.addAction("Exit Aisha")
        quit_a.triggered.connect(self.app.quit)

        self.setContextMenu(menu)
        self.activated.connect(self._on_click)

    def _on_click(self, reason):
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self.card.setVisible(not self.card.isVisible())

    def _tray_toggle_mute(self):
        """Toggle mute from tray."""
        self.card._toggle_mute()

    def _tray_toggle_pause(self):
        """Toggle listening pause from tray (opposite of mute)."""
        self.card._toggle_mute()  # same action, just reversed tooltip

    def _tray_copy_reply(self):
        """Copy last reply from tray."""
        self.card._copy_reply()

    def _recalibrate(self):
        if self.card.assistant and hasattr(self.card.assistant, 'silence_threshold'):
            # Just reset the threshold to default — actual recalibration happens live
            self.card.assistant.silence_threshold = 0.038
            print("🎤 Mic threshold reset to default (recalibration happens live during recording)")

    def _reset_memory(self):
        try:
            with open(MEMORY_FILE, "w", encoding="utf-8") as f:
                json.dump({
                    "user_name": "Boss",
                    "preferences": {},
                    "facts": [],
                    "conversation_history": [],
                    "conversation_summaries": [],
                    "daily_episodes": [],
                    "last_interaction": None,
                }, f, indent=2)
        except Exception:
            pass

    def _run_diag(self):
        import subprocess
        subprocess.Popen(
            [sys.executable, os.path.join(BASE_DIR, "doctor.py")],
            creationflags=subprocess.CREATE_NEW_CONSOLE
        )

    def _open_folder(self):
        import subprocess
        subprocess.Popen(["explorer.exe", BASE_DIR])


# ----------------------------------------------------------------------
# Launcher
# ----------------------------------------------------------------------
def launch_aisha_gui(assistant=None):
    app = QApplication.instance() or QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    # A single consistent base font + a neutral style engine so every control
    # renders with the same typography (and avoids the missing-OpenType warning
    # from the old "Segoe UI Variable Display" face).
    try:
        _load_bundled_fonts()
        app.setStyle("Fusion")
        base_font = QFont("Segoe UI", 10)
        base_font.setStyleStrategy(QFont.StyleStrategy.PreferAntialias)
        app.setFont(base_font)
    except Exception:
        pass

    card = AishaCard(assistant_instance=assistant)
    tray = AishaTray(card, app)

    return card, tray
