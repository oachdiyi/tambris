"""Terminal UI: colors, menus, prompts, and a flicker-free dashboard.

No external TUI dependencies. Uses ANSI escape sequences with VT100 enabled
on Windows (PowerShell/CMD compatible). The dashboard keeps the previous
frame and rewrites only lines that changed, avoiding flicker and scrolling.
"""

from __future__ import annotations

import os
import shutil
import sys
import time
from typing import Dict, List, Optional

IS_WINDOWS = os.name == "nt"

if IS_WINDOWS:
    import msvcrt


class Ansi:
    RESET = "\x1b[0m"
    BOLD = "\x1b[1m"
    DIM = "\x1b[2m"
    RED = "\x1b[31m"
    GREEN = "\x1b[32m"
    YELLOW = "\x1b[33m"
    BLUE = "\x1b[34m"
    MAGENTA = "\x1b[35m"
    CYAN = "\x1b[36m"
    WHITE = "\x1b[37m"
    BRIGHT_GREEN = "\x1b[92m"
    BRIGHT_RED = "\x1b[91m"
    BRIGHT_YELLOW = "\x1b[93m"
    BRIGHT_CYAN = "\x1b[96m"
    BRIGHT_WHITE = "\x1b[97m"
    BRIGHT_BLUE = "\x1b[94m"
    BRIGHT_MAGENTA = "\x1b[95m"


class Theme:
    def __init__(self, enabled: bool = True):
        self.on = enabled

    def __getattr__(self, name: str) -> str:
        color = getattr(Ansi, name, "")
        return color if self.on else ""


def enable_windows_vt() -> None:
    if IS_WINDOWS:
        os.system("")  # enables ANSI escape processing in PowerShell/CMD


def terminal_size() -> tuple:
    try:
        cols, rows = shutil.get_terminal_size(fallback=(100, 30))
        return cols, rows
    except Exception:
        return 100, 30


def supports_color() -> bool:
    if IS_WINDOWS:
        return True
    return sys.stdout.isatty()


# --------------------------------------------------------------------------- #
# input helpers


def read_key(timeout: float = 0.0) -> Optional[str]:
    """Non-blocking single-key read where available; None when unsupported."""
    if IS_WINDOWS:
        if msvcrt.kbhit():
            ch = msvcrt.getwch()
            return ch.lower()
        return None
    if not sys.stdin.isatty():
        if timeout:
            time.sleep(timeout)
        return None
    import select
    import termios
    import tty

    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        ready, _, _ = select.select([sys.stdin], [], [], timeout)
        if ready:
            ch = sys.stdin.read(1)
            return ch.lower()
        return None
    except (termios.error, OSError, ValueError):
        return None
    finally:
        try:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)
        except Exception:
            pass


def pause_for_key(message: str = "Press any key to continue...") -> None:
    print(f"\n{message}")
    if IS_WINDOWS:
        msvcrt.getwch()
    else:
        import termios, tty, select

        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        try:
            tty.setcbreak(fd)
            select.select([sys.stdin], [], [], None)
            sys.stdin.read(1)
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)


def ask(prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    raw = input(f"{prompt}{suffix}: ").strip()
    return raw or default


def ask_int(prompt: str, default: int, lo: int = 1, hi: int = 100000) -> int:
    while True:
        raw = ask(prompt, str(default))
        try:
            value = int(raw)
            if lo <= value <= hi:
                return value
        except ValueError:
            pass
        print(f"  Enter a number between {lo} and {hi}.")


def ask_float(prompt: str, default: float, lo: float = 0.1, hi: float = 600.0) -> float:
    while True:
        raw = ask(prompt, str(default))
        try:
            value = float(raw)
            if lo <= value <= hi:
                return value
        except ValueError:
            pass
        print(f"  Enter a number between {lo} and {hi}.")


def ask_bool(prompt: str, default: bool = True) -> bool:
    hint = "Y/n" if default else "y/N"
    raw = input(f"{prompt} [{hint}]: ").strip().lower()
    if not raw:
        return default
    return raw in ("y", "yes", "1", "true")


def choose(prompt: str, options: List[str], default: int = 0) -> int:
    print()
    for i, opt in enumerate(options, 1):
        marker = "*" if i - 1 == default else " "
        print(f"  {i}){marker} {opt}")
    raw = ask(prompt, str(default + 1))
    try:
        idx = int(raw) - 1
        if 0 <= idx < len(options):
            return idx
    except ValueError:
        pass
    return default


def confirm(prompt: str, default: bool = True) -> bool:
    return ask_bool(prompt, default)


# --------------------------------------------------------------------------- #
# dashboard


def fmt_duration(seconds: float) -> str:
    if seconds != seconds or seconds in (float("inf"), float("-inf")):
        return "--:--:--"
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


class Dashboard:
    """Claims the full screen once, then rewrites only changed lines."""

    def __init__(self, theme: Theme, out=None):
        self.theme = theme
        self.out = out or sys.stdout
        self._last_lines: List[str] = []
        self._last_size = terminal_size()
        self._cursor_row = 0
        self._started = False

    def _plain_len(self, s: str) -> int:
        import re

        return len(re.sub(r"\x1b\[[0-9;]*m", "", s))

    def _move_to(self, row: int) -> None:
        delta = row - self._cursor_row
        if delta < 0:
            self.out.write(f"\x1b[{abs(delta)}A")
        elif delta > 0:
            self.out.write(f"\x1b[{delta}B")
        self.out.write("\r")
        self._cursor_row = row

    def start(self) -> None:
        self.out.write("\x1b[2J\x1b[H")
        self.out.flush()
        self._last_lines = []
        self._cursor_row = 0
        self._started = True

    def _write_frame(self, lines: List[str]) -> None:
        cols, _ = self._last_size
        if lines == self._last_lines:
            return
        for i, line in enumerate(lines):
            if i < len(self._last_lines) and self._last_lines[i] == line:
                continue
            self._move_to(i)
            plain = self._plain_len(line)
            pad = " " * max(0, min(cols - 1, 120) - plain)
            self.out.write(line + pad + "\x1b[K")
        for i in range(len(lines), len(self._last_lines)):
            self._move_to(i)
            self.out.write("\x1b[K")
        self.out.flush()
        self._last_lines = lines[:]
        self._move_to(len(lines))

    def clear(self) -> None:
        self.start()

    def check_resize(self) -> bool:
        size = terminal_size()
        if size != self._last_size:
            self._last_size = size
            self.start()
            return True
        return False

    def finish(self) -> None:
        if self._started:
            self.out.write("\n")
            self.out.flush()
            self._started = False

    def render(self, snap: Dict, theme: Theme) -> None:
        t = theme
        cols, _ = self._last_size
        width = min(cols - 1, 78)
        bar = t.DIM + "─" * width + t.RESET
        title = (
            f"{t.BOLD}{t.BRIGHT_CYAN}  SITECHECK PRO{t.RESET} "
            f"{t.DIM}v{snap.get('version', '?')}  ·  {snap.get('mode', '').upper()} "
            f"mode  ·  {snap.get('preset', '')} preset{t.RESET}"
        )
        state = "PAUSED (no working proxies)" if snap.get("paused") else "RUNNING"
        state_color = t.BRIGHT_YELLOW if snap.get("paused") else t.BRIGHT_GREEN
        processed = snap.get("processed", 0)
        total = snap.get("total", 0)
        pct = (processed * 100.0 / total) if total else 0.0
        cats = snap.get("by_category", {})
        valid = cats.get("VALID", 0) + cats.get("SUCCESS", 0)
        dead = cats.get("DEAD", 0)
        restricted = cats.get("RESTRICTED", 0)
        failed = cats.get("FAILED", 0) + cats.get("ERROR", 0)
        parked = cats.get("PARKED", 0)
        unknown = cats.get("UNKNOWN", 0)
        proxy_fails = snap.get("proxy_failures", 0)

        def row(label: str, value: str) -> str:
            return f"  {t.DIM}{label:<18}{t.RESET}{value}"

        lines = [
            bar,
            title,
            bar,
            row("State", f"{state_color}{state}{t.RESET}"),
            row(
                "Progress",
                f"{processed:,} / {total:,}  ({pct:.1f}%)",
            ),
            row("Remaining", f"{snap.get('remaining', 0):,}"),
            bar,
            row("CPM", f"{t.BRIGHT_WHITE}{snap.get('cpm', 0):.0f}{t.RESET}"),
            row("Avg CPM", f"{snap.get('avg_cpm', 0):.0f}"),
            row("Peak CPM", f"{t.BRIGHT_CYAN}{snap.get('peak_cpm', 0):.0f}{t.RESET}"),
            bar,
            row(
                "Valid / Success",
                f"{t.BRIGHT_GREEN}{valid:,}{t.RESET}",
            ),
            row("Dead", f"{t.BRIGHT_RED}{dead:,}{t.RESET}"),
            row("Restricted", f"{t.BRIGHT_YELLOW}{restricted:,}{t.RESET}"),
            row("Parked", f"{t.BRIGHT_MAGENTA}{parked:,}{t.RESET}"),
            row("Failed / Errors", f"{failed:,}"),
            row("Unknown", f"{t.DIM}{unknown:,}{t.RESET}"),
            row("Proxy failures", f"{t.DIM}{proxy_fails:,}{t.RESET}"),
            bar,
            row("Active workers", f"{snap.get('active_workers', 0)}"),
            row("Elapsed", fmt_duration(snap.get("elapsed", 0))),
            row("ETA", fmt_duration(snap.get("eta", 0) if "eta" in snap else 0)),
            bar,
            f"  {t.DIM}[P] pause  [Q] stop & save{t.RESET}",
        ]
        if snap.get("error"):
            lines.append(
                f"  {t.BRIGHT_RED}note: {snap['error']}{t.RESET}"
            )
        self._write_frame(lines)


def render_eta(snap: Dict) -> float:
    processed = snap.get("processed", 0)
    total = snap.get("total", 0)
    avg = snap.get("avg_cpm", 0.0)
    remaining = max(total - processed, 0)
    if avg <= 0:
        return float("inf")
    return remaining * 60.0 / avg
