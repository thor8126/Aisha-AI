"""
hotkey_manager.py — Global Windows Hotkey Listener for Aisha AI.
Uses native Windows Win32 API (RegisterHotKey) for zero-dependency,
ultra-low CPU background hotkey detection across all Windows apps/games.
Default Hotkey: Ctrl + Shift + A
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes
import threading
import time
from typing import Callable

from aisha_logger import log

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

# Win32 Constants
MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000
WM_HOTKEY = 0x0312
PM_REMOVE = 0x0001


class GlobalHotkeyListener:
    """Listens for a global system-wide hotkey to wake Aisha or interrupt speech."""

    def __init__(self, callback: Callable[[], None], hotkey_id: int = 101):
        self.callback = callback
        self.hotkey_id = hotkey_id
        self._thread: threading.Thread | None = None
        self._running = False
        self._thread_id = 0

    def start(self) -> bool:
        """Start the background Win32 message loop."""
        if self._running:
            return True

        self._running = True
        self._thread = threading.Thread(target=self._msg_loop, daemon=True, name="AishaHotkeyListener")
        self._thread.start()
        return True

    def stop(self):
        """Stop listening and unregister the hotkey."""
        self._running = False
        if self._thread_id:
            user32.PostThreadMessageW(self._thread_id, 0x0012, 0, 0)  # WM_QUIT
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=1.0)

    def _msg_loop(self):
        self._thread_id = kernel32.GetCurrentThreadId()

        # Register Ctrl + Shift + A (0x41 = 'A')
        # MOD_NOREPEAT prevents auto-repeat flood when holding the key down
        vk_code = 0x41  # 'A'
        modifiers = MOD_CONTROL | MOD_SHIFT | MOD_NOREPEAT

        success = user32.RegisterHotKey(None, self.hotkey_id, modifiers, vk_code)
        if not success:
            log.warning("Could not register global hotkey Ctrl+Shift+A (may be in use by another app)")
            return

        log.info("Global hotkey registered: Ctrl + Shift + A (Wake / Push-to-Talk / Interrupt)")
        print("⌨️  Global Hotkey active: [Ctrl + Shift + A] (Wake / Interrupt)")

        msg = ctypes.wintypes.MSG()
        try:
            while self._running:
                # Use PeekMessage or GetMessage with timeout
                res = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
                if res == 0 or res == -1:
                    break

                if msg.message == WM_HOTKEY and msg.wParam == self.hotkey_id:
                    log.info("Global hotkey triggered by user")
                    try:
                        if self.callback:
                            self.callback()
                    except Exception as e:
                        log.error(f"Error in hotkey callback: {e}")

                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
        finally:
            user32.UnregisterHotKey(None, self.hotkey_id)
            log.info("Global hotkey unregistered")
