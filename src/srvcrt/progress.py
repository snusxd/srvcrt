"""Terminal display for mod classification and server installation."""

from __future__ import annotations

import shutil
import sys
import threading
from typing import TextIO


BRAILLE_FRAMES = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")
STATUS_LABELS = {
    "search": "Search",
    "client": "Client-side",
    "server": "Server-side",
    "review": "Needs review",
}


class ConsoleProgress:
    def __init__(self, stream: TextIO | None = None):
        self.stream = stream or sys.stdout
        self.is_tty = self.stream.isatty()
        self._names: list[str] = []
        self._labels: dict[str, str] = {}
        self._statuses: dict[str, str] = {}
        self._lock = threading.Lock()
        self._spinner_stop = threading.Event()
        self._spinner_thread: threading.Thread | None = None
        self._spinner_message = ""

    @staticmethod
    def _safe_name(name: str) -> str:
        return "".join(char if char.isprintable() else "?" for char in name)

    def _mod_line(self, name: str, status: str) -> str:
        label = STATUS_LABELS[status]
        display_name = self._safe_name(self._labels[name])
        if self.is_tty:
            columns = shutil.get_terminal_size(fallback=(100, 24)).columns
            available = max(8, columns - len(label) - 4)
            if len(display_name) > available:
                display_name = display_name[:available - 1] + "…"
        return f"{display_name} [{label}]"

    def mod_status(self, name: str, display_name: str, status: str) -> None:
        if status not in STATUS_LABELS:
            raise ValueError(f"Unknown mod status: {status}")
        with self._lock:
            if name not in self._statuses:
                self._names.append(name)
                self._labels[name] = display_name
                self._statuses[name] = status
                self.stream.write(self._mod_line(name, status) + "\n")
            elif self._statuses[name] != status:
                self._statuses[name] = status
                if self.is_tty:
                    lines_up = len(self._names) - self._names.index(name)
                    self.stream.write(
                        f"\x1b[{lines_up}A\r\x1b[2K{self._mod_line(name, status)}"
                        f"\x1b[{lines_up}B\r"
                    )
                else:
                    self.stream.write(self._mod_line(name, status) + "\n")
            self.stream.flush()

    def message(self, message: str) -> None:
        with self._lock:
            self.stream.write(message + "\n")
            self.stream.flush()

    def _spin(self) -> None:
        index = 1
        while not self._spinner_stop.wait(0.09):
            with self._lock:
                self.stream.write(
                    f"\r\x1b[2K{BRAILLE_FRAMES[index % len(BRAILLE_FRAMES)]} "
                    f"{self._spinner_message}"
                )
                self.stream.flush()
            index += 1

    def start_install(self, message: str) -> None:
        with self._lock:
            self._spinner_message = message
            self._spinner_stop.clear()
            if self.is_tty:
                self.stream.write(f"{BRAILLE_FRAMES[0]} {message}")
                self._spinner_thread = threading.Thread(target=self._spin, daemon=True)
                self._spinner_thread.start()
            else:
                self.stream.write(f"{BRAILLE_FRAMES[0]} {message}\n")
            self.stream.flush()

    def finish_install(self, success: bool) -> None:
        self._spinner_stop.set()
        thread = self._spinner_thread
        if thread:
            thread.join(timeout=1)
            self._spinner_thread = None
        with self._lock:
            if self.is_tty:
                self.stream.write("\r\x1b[2K")
            result = "installed" if success else "failed"
            self.stream.write(f"Minecraft server core {result}.\n")
            self.stream.flush()
