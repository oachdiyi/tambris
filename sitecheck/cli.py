"""Interactive console application."""

from __future__ import annotations

import asyncio
import os
import time
from typing import List, Optional

from . import __version__
from .config import Settings
from .proxies import ProxyPool
from .results import OutputWriter
from .runner import Runner
from .session import SessionDB
from .ui import (
    Dashboard,
    Theme,
    ask,
    ask_float,
    ask_int,
    choose,
    confirm,
    enable_windows_vt,
    read_key,
    render_eta,
    supports_color,
)

SESSION_DIR = os.path.join(os.getcwd(), "sessions")
SETTINGS_FILE = os.path.join(SESSION_DIR, "settings.json")


class App:
    def __init__(self):
        enable_windows_vt()
        self.theme = Theme(supports_color())
        self.settings = self._load_settings()
        self.running = True

    # ------------------------- settings ------------------------------- #

    def _load_settings(self) -> Settings:
        try:
            return Settings.load(SETTINGS_FILE)
        except Exception:
            return Settings()

    def _save_settings(self) -> None:
        try:
            os.makedirs(SESSION_DIR, exist_ok=True)
            self.settings.save(SETTINGS_FILE)
        except Exception:
            pass

    # --------------------------- main --------------------------------- #

    def run(self) -> int:
        t = self.theme
        while self.running:
            self._banner()
            idx = choose(
                "Main menu",
                [
                    "Start New Session",
                    "Resume Previous Session",
                    "Settings",
                    "Exit",
                ],
            )
            if idx == 0:
                self._new_session()
            elif idx == 1:
                self._resume_session()
            elif idx == 2:
                self._settings_menu()
            else:
                self.running = False
        print(f"\n{t.DIM}Goodbye.{t.RESET}")
        return 0

    def _banner(self) -> None:
        t = self.theme
        inner = 48
        print()
        print(f"{t.BOLD}{t.BRIGHT_CYAN}  ╔{'═' * inner}╗{t.RESET}")
        print(
            f"{t.BOLD}{t.BRIGHT_CYAN}  ║{'SITECHECK PRO'.center(inner)}║{t.RESET}"
        )
        print(
            f"{t.BOLD}{t.BRIGHT_CYAN}  ║{f'high-performance website & proxy checker · v{__version__}'.center(inner)}║{t.RESET}"
        )
        print(f"{t.BOLD}{t.BRIGHT_CYAN}  ╚{'═' * inner}╝{t.RESET}")
        print()

    # ------------------------- sessions ------------------------------- #

    def _session_files(self) -> List[str]:
        if not os.path.isdir(SESSION_DIR):
            return []
        return sorted(
            f for f in os.listdir(SESSION_DIR) if f.endswith(".db")
        )

    def _new_session(self) -> None:
        t = self.theme
        print()
        path = ask("Path to URL list file")
        if not path or not os.path.isfile(path):
            print(f"  {t.BRIGHT_RED}File not found.{t.RESET}")
            return
        mode_idx = choose("Checking mode", ["Simple (max speed)", "Advanced (accurate)"])
        self.settings.mode = "simple" if mode_idx == 0 else "advanced"
        proxy_path = ask("Path to proxy file (empty = direct connection)", "")
        if proxy_path and not os.path.isfile(proxy_path):
            print(f"  {t.BRIGHT_RED}Proxy file not found.{t.RESET}")
            return
        self.settings.input_file = path
        self.settings.proxy_file = proxy_path or None
        preset_idx = choose(
            "Performance preset",
            ["Low", "Balanced", "High", "Extreme", "Custom"],
            default=1,
        )
        self._apply_preset_choice(preset_idx)
        self._run_session(path, proxy_path or None, resume=False)

    def _apply_preset_choice(self, preset_idx: int) -> None:
        names = ["low", "balanced", "high", "extreme"]
        if preset_idx < 4:
            self.settings.apply_preset(names[preset_idx])
        else:
            self.settings.preset = "custom"
            self.settings.concurrency = ask_int("Concurrency (workers)", self.settings.concurrency, 1, 100000)
            self.settings.per_proxy_concurrency = ask_int(
                "Max concurrent requests per proxy", self.settings.per_proxy_concurrency, 1, 1000
            )
            self.settings.connect_timeout = ask_float("Connect timeout (s)", self.settings.connect_timeout)
            self.settings.request_timeout = ask_float("Request timeout (s)", self.settings.request_timeout)
        self._save_settings()

    def _resume_session(self) -> None:
        t = self.theme
        files = self._session_files()
        if not files:
            print(f"\n  {t.BRIGHT_YELLOW}No previous sessions found.{t.RESET}")
            return
        print()
        for i, f in enumerate(files, 1):
            size = os.path.getsize(os.path.join(SESSION_DIR, f))
            print(f"  {i}) {f}  ({size // 1024} KB)")
        idx = choose("Resume which session", files and [str(i) for i in range(1, len(files) + 1)])
        db_path = os.path.join(SESSION_DIR, files[idx])
        db = SessionDB(db_path)
        offset = db.get_int("byte_offset", 0)
        saved = db.get_meta("settings")
        input_file = None
        if saved:
            try:
                saved_settings = Settings.from_json(saved)
                input_file = saved_settings.input_file
                self.settings.mode = saved_settings.mode
                self.settings.preset = saved_settings.preset
            except Exception:
                pass
        db.close()
        if not input_file or not os.path.isfile(input_file):
            input_file = ask("Path to original URL list file")
            if not os.path.isfile(input_file):
                print(f"  {t.BRIGHT_RED}File not found.{t.RESET}")
                return
        processed_note = f" (checkpoint at byte {offset:,})" if offset else ""
        print(f"\n  {t.DIM}Resuming from byte offset {offset:,}{processed_note}{t.RESET}")
        if not confirm("Reuse the previous proxy file?"
                       + (f" [{self.settings.proxy_file}]" if self.settings.proxy_file else "")):
            proxy_path = ask("Path to proxy file (empty = direct)", self.settings.proxy_file or "")
            self.settings.proxy_file = proxy_path or None
        preset_idx = choose(
            "Performance preset for this run",
            ["Keep previous", "Low", "Balanced", "High", "Extreme", "Custom"],
        )
        if preset_idx > 0:
            self._apply_preset_choice(preset_idx - 1)
        self._run_session(input_file, self.settings.proxy_file, resume=True, db_path=db_path)

    # -------------------------- engine run ----------------------------- #

    def _run_session(
        self,
        input_path: str,
        proxy_path: Optional[str],
        resume: bool = False,
        db_path: Optional[str] = None,
    ) -> None:
        t = self.theme
        if db_path is None:
            stamp = time.strftime("%Y%m%d-%H%M%S")
            db_path = os.path.join(SESSION_DIR, f"session-{stamp}.db")
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        output_dir = os.path.join(os.getcwd(), self.settings.output_dir)

        db = SessionDB(db_path)
        pool: Optional[ProxyPool] = None
        if proxy_path:
            try:
                pool = ProxyPool.from_file(
                    proxy_path,
                    health_threshold=self.settings.proxy_health_threshold,
                    quarantine_seconds=self.settings.proxy_quarantine_seconds,
                )
            except OSError as exc:
                print(f"  {t.BRIGHT_RED}Cannot read proxy file: {exc}{t.RESET}")
                db.close()
                return
            print(f"  {t.DIM}Loaded {len(pool.display_list())} proxies; detecting protocols...{t.RESET}")

        async def main() -> None:
            if pool is not None:
                await pool.resolve_schemes()
                pool.set_per_proxy_limit(self.settings.per_proxy_concurrency)
                snap = pool.snapshot()
                alive = snap.healthy + snap.quarantined
                print(
                    f"  {t.DIM}Proxies usable: {snap.healthy}, "
                    f"unreachable: {snap.dead}{t.RESET}"
                )
                if alive == 0:
                    print(f"  {t.BRIGHT_RED}No working proxies found. Aborting.{t.RESET}")
                    return
            writer = OutputWriter(output_dir)
            runner = Runner(self.settings, db, pool, writer, input_path)
            await self._run_with_dashboard(runner)

        try:
            asyncio.run(main())
        except KeyboardInterrupt:
            print(f"\n  {t.BRIGHT_YELLOW}Interrupted; session checkpointed.{t.RESET}")

    async def _run_with_dashboard(self, runner: Runner) -> None:
        t = self.theme
        dash = Dashboard(self.theme)
        dash.start()
        stop = asyncio.Event()

        async def ui_loop() -> None:
            fps = max(self.settings.dashboard_fps, 1.0)
            last_render = 0.0
            while not stop.is_set():
                now = time.monotonic()
                if now - last_render >= 1.0 / fps:
                    dash.check_resize()
                    snap = runner.snapshot()
                    snap["eta"] = render_eta(snap)
                    dash.render(snap, self.theme)
                    last_render = now
                key = await asyncio.get_running_loop().run_in_executor(
                    None, read_key, 0.05
                )
                if key == "q":
                    runner.stop()
                    stop.set()
                elif key == "p":
                    runner.paused = not runner.paused
                await asyncio.sleep(0.01)

        ui_task = asyncio.create_task(ui_loop())
        run_task = asyncio.create_task(runner.run())
        done, _ = await asyncio.wait(
            {run_task}, return_when=asyncio.FIRST_COMPLETED
        )
        stop.set()
        ui_task.cancel()
        try:
            await ui_task
        except asyncio.CancelledError:
            pass
        if run_task.cancelled():
            pass
        else:
            exc = run_task.exception()
            if exc:
                print(f"\n  {t.BRIGHT_RED}Run failed: {exc}{t.RESET}")
        dash.finish()
        counts = runner.writer.counts()
        print(f"\n  {t.BOLD}Session finished.{t.RESET}")
        for cat, n in sorted(counts.items()):
            print(f"    {cat:<16} {n:,}")
        print(f"  {t.DIM}Results in: {runner.writer.dir}{t.RESET}")
        runner.writer.close()

    # --------------------------- settings ------------------------------ #

    def _settings_menu(self) -> None:
        t = self.theme
        s = self.settings
        while True:
            print()
            print(f"{t.BOLD}  Settings{t.RESET}")
            print(f"  {t.DIM}1{t.RESET}) Checking mode          : {s.mode}")
            print(f"  {t.DIM}2{t.RESET}) Performance preset     : {s.preset}")
            print(f"  {t.DIM}3{t.RESET}) Concurrency            : {s.concurrency}")
            print(f"  {t.DIM}4{t.RESET}) Per-proxy concurrency  : {s.per_proxy_concurrency}")
            print(f"  {t.DIM}5{t.RESET}) Max redirects          : {s.max_redirects}")
            print(f"  {t.DIM}6{t.RESET}) Connect timeout        : {s.connect_timeout}s")
            print(f"  {t.DIM}7{t.RESET}) Request timeout        : {s.request_timeout}s")
            print(f"  {t.DIM}8{t.RESET}) HEAD-first strategy    : {s.head_first}")
            print(f"  {t.DIM}9{t.RESET}) TLS verification       : {s.verify_tls}")
            print(f"  {t.DIM}10{t.RESET}) HTTPS->HTTP fallback   : {s.https_fallback_http}")
            print(f"  {t.DIM}11{t.RESET}) Deduplication          : {s.dedup}")
            print(f"  {t.DIM}12{t.RESET}) Proxy quarantine secs  : {s.proxy_quarantine_seconds}")
            print(f"  {t.DIM}13{t.RESET}) Output directory       : {s.output_dir}")
            print(f"  {t.DIM}0{t.RESET}) Back")
            idx = choose("Toggle/edit", [str(i) for i in range(0, 14)])
            if idx == 0:
                self._save_settings()
                return
            elif idx == 1:
                s.mode = "advanced" if s.mode == "simple" else "simple"
            elif idx == 2:
                pidx = choose(
                    "Preset",
                    ["Low", "Balanced", "High", "Extreme", "Custom"],
                    default=1,
                )
                self._apply_preset_choice(pidx)
            elif idx == 3:
                s.concurrency = ask_int("Concurrency", s.concurrency, 1, 100000)
            elif idx == 4:
                s.per_proxy_concurrency = ask_int("Per-proxy concurrency", s.per_proxy_concurrency, 1, 1000)
            elif idx == 5:
                s.max_redirects = ask_int("Max redirects", s.max_redirects, 1, 30)
            elif idx == 6:
                s.connect_timeout = ask_float("Connect timeout (s)", s.connect_timeout)
            elif idx == 7:
                s.request_timeout = ask_float("Request timeout (s)", s.request_timeout)
            elif idx == 8:
                s.head_first = not s.head_first
            elif idx == 9:
                s.verify_tls = not s.verify_tls
            elif idx == 10:
                s.https_fallback_http = not s.https_fallback_http
            elif idx == 11:
                s.dedup = not s.dedup
            elif idx == 12:
                s.proxy_quarantine_seconds = ask_float(
                    "Proxy quarantine seconds", s.proxy_quarantine_seconds, 5.0, 3600.0
                )
            elif idx == 13:
                s.output_dir = ask("Output directory", s.output_dir)
            self._save_settings()


def main() -> int:
    try:
        return App().run()
    except KeyboardInterrupt:
        print("\nInterrupted.")
        return 130
    except EOFError:
        print()
        return 0
