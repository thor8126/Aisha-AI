"""
notifications.py — Native Windows Toast & Desktop Notification Engine for Aisha AI.
Supports direct PyQt6 System Tray balloon/toast integration and fallback to Windows Shell.
"""

from __future__ import annotations

import os
import sys
import subprocess
from aisha_logger import log


def send_windows_toast(title: str, message: str):
    """Send a native Windows Action Center notification."""
    log.info(f"TOAST: {title} | {message}")

    # 1. Try PyQt6 GUI Tray Signal if active
    try:
        from aisha_gui import SIGNALS
        # If GUI is active, trigger through GUI signal bridge
        if hasattr(SIGNALS, "toast_requested"):
            SIGNALS.toast_requested.emit(title, message)
            return
    except Exception:
        pass

    # 2. Fallback to PowerShell Windows Runtime Toast
    try:
        safe_title = title.replace('"', '`"').replace("'", "''")
        safe_msg = message.replace('"', '`"').replace("'", "''")
        ps_cmd = (
            f"[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null; "
            f"$t = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02); "
            f"$txt = $t.GetElementsByTagName('text'); "
            f"$txt[0].AppendChild($t.CreateTextNode('{safe_title}')) | Out-Null; "
            f"$txt[1].AppendChild($t.CreateTextNode('{safe_msg}')) | Out-Null; "
            f"[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('Aisha AI Companion').Show([Windows.UI.Notifications.ToastNotification]::new($t));"
        )
        subprocess.Popen(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps_cmd],
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
    except Exception as e:
        log.error(f"Failed to display toast notification: {e}")
