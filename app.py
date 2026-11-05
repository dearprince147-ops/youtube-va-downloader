#!/usr/bin/env python3
"""
══════════════════════════════════════════════════════════════
▶  YouTube Downloader — Termux / Desktop  (Production v4.0)
══════════════════════════════════════════════════════════════
A production-ready terminal UI for downloading videos and audio
from YouTube.

What's new in v4.0
  • Instant start-up — yt-dlp loads in the background while the menu
    is already on screen; the update check no longer blocks launch.
  • One download engine for everything: single video, playlist, or a
    batch of many links — all run in parallel with a live dashboard
    (overall progress, per-item bars, speed, ETA, recent results).
  • Paste several links at once and they're queued automatically.
  • Faster downloads: metadata fetched for the preview is reused for
    the download (no second extraction) and chunked HTTP is used
    everywhere.
  • Clean Ctrl+C: stops all workers immediately; partial files resume.

Kept from v3.x: automatic dependency management, resume support,
download history, cookie-based authentication, H.264/AAC safety
net, skip-already-downloaded for playlists, retry prompts, Windows
ffmpeg auto-install, Termux storage setup.

Fully cross-platform:  Termux (Android), Linux, macOS, Windows 10/11.

Requirements:  pip install yt-dlp rich
ffmpeg on PATH  (pkg install ffmpeg | apt install ffmpeg |
                 winget install Gyan.FFmpeg  — or let the app do it)
"""
from __future__ import annotations

import concurrent.futures
import contextlib
import copy
import importlib.util
import json
import logging
import logging.handlers
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
from collections import deque
from dataclasses import dataclass, field
from functools import lru_cache
from importlib.metadata import version as _pkg_version, PackageNotFoundError
from pathlib import Path


# ═══════════════════════════════════════════════════════════════
#  BOOTSTRAP — ensure required Python packages exist before anything
# ═══════════════════════════════════════════════════════════════
def _pip_install(*args: str, user: bool = False) -> tuple[bool, str]:
    """Run ``pip install`` quietly.  Returns ``(success, stderr_text)``.

    ``user=True`` installs into the user site-packages, which is needed
    on machines where the main Python tree is read-only (common on
    locked-down Windows installs)."""
    cmd = [sys.executable, "-m", "pip", "install", "--quiet"]
    if user:
        cmd.append("--user")
    cmd.extend(args)
    try:
        result = subprocess.run(cmd, capture_output=True, text=True)
        return (result.returncode == 0, (result.stderr or "").strip())
    except OSError as e:
        return (False, str(e))


def _install_and_restart(package: str) -> None:
    """Install a pip package then restart the script cleanly.

    Uses subprocess + sys.exit on Windows (os.execv is emulated there and
    doesn't truly replace the process) and os.execv on POSIX (Termux /
    Linux / macOS) where it works correctly."""
    print(f"→ Installing {package}…")
    ok, err = _pip_install(package)
    if not ok:
        ok, err2 = _pip_install(package, user=True)
        if not ok:
            print(f"✗ Failed to install {package}.")
            detail = err2 or err
            if detail:
                print(f"  {detail[:300]}")
            print(f"  Run manually:  python -m pip install {package}")
            sys.exit(1)

    os.environ["YTD_NO_UPDATE"] = "1"
    script = str(Path(__file__).resolve())
    args = [sys.executable, script] + sys.argv[1:]
    if sys.platform == "win32":
        try:
            subprocess.Popen(args)
        except OSError:
            print(f"✓ Installed {package}.  Please re-run the script.")
        sys.exit(0)
    else:
        try:
            os.execv(sys.executable, args)
        except OSError:
            print(f"✓ Installed {package}.  Please re-run the script.")
        sys.exit(0)


try:
    from rich import box
    from rich.align import Align
    from rich.console import Console, Group
    from rich.live import Live
    from rich.markup import escape as esc
    from rich.panel import Panel
    from rich.progress import (
        Progress, BarColumn, TextColumn, DownloadColumn,
        TransferSpeedColumn, TimeRemainingColumn, SpinnerColumn,
    )
    from rich.progress_bar import ProgressBar
    from rich.prompt import Prompt, Confirm
    from rich.rule import Rule
    from rich.table import Table
    from rich.text import Text
except ImportError:
    _install_and_restart("rich")

console = Console()

# yt-dlp is imported lazily (see ytdlp()) so the menu appears instantly.
# Here we only check that it is installed, which is nearly free.
if importlib.util.find_spec("yt_dlp") is None:
    _install_and_restart("yt-dlp")

_YTDLP = None
_YTDLP_LOCK = threading.Lock()


def ytdlp():
    """Return the ``yt_dlp`` module, importing it on first use.  A background
    thread calls this at start-up, so by the time a download starts the
    (slow) import has normally finished."""
    global _YTDLP
    if _YTDLP is None:
        with _YTDLP_LOCK:
            if _YTDLP is None:
                try:
                    import yt_dlp
                    import yt_dlp.utils  # noqa: F401
                except ImportError:
                    _install_and_restart("yt-dlp")
                _YTDLP = yt_dlp
    return _YTDLP


# ═══════════════════════════════════════════════════════════════
#  CONFIGURATION
# ═══════════════════════════════════════════════════════════════
APP_VERSION = "4.0.0"
IS_TERMUX = "com.termux" in os.environ.get("PREFIX", "")

if IS_TERMUX:
    DOWNLOAD_DIR = Path.home() / "storage" / "downloads" / "YouTube"
else:
    DOWNLOAD_DIR = Path.home() / "Downloads" / "YouTube"

SCRIPT_DIR = Path(__file__).resolve().parent
HISTORY_MD_FILE = SCRIPT_DIR / "history.md"
COOKIES_FILE = SCRIPT_DIR / "cookies.txt"
SETTINGS_FILE = SCRIPT_DIR / "settings.json"
DEBUG_LOG_FILE = SCRIPT_DIR / "debug.log"

# Fallback quality list used for playlists (where per-video format
# info isn't available) or when metadata extraction fails.
_FALLBACK_QUALITIES = [
    {"height": 360,  "fps": 30},
    {"height": 480,  "fps": 30},
    {"height": 720,  "fps": 30},
    {"height": 720,  "fps": 60},
    {"height": 1080, "fps": 30},
    {"height": 1080, "fps": 60},
    {"height": 1440, "fps": 60},
    {"height": 2160, "fps": 60},
]

# Height caps offered in batch mode (videos differ, so "up to" semantics).
_BATCH_HEIGHTS = [360, 480, 720, 1080, 1440, 2160]

AUDIO_CHOICES = {
    "1": ("WAV — Lossless / Editing",              {"codec": "wav", "quality": "0"}),
    "2": ("MP3 128 kbps — Compact",                {"codec": "mp3", "quality": "128"}),
    "3": ("MP3 320 kbps + Cover Art — Premium",    {"codec": "mp3", "quality": "320", "art": True}),
}

TRACKED_PACKAGES = ["yt-dlp", "rich"]
_SKIP_UPDATE = os.environ.get("YTD_NO_UPDATE") == "1"

MAX_RETRIES = 3          # interactive "retry failed?" rounds
AUTO_ATTEMPTS = 3        # silent per-item attempts before an item is failed
INFO_TTL = 20 * 60       # seconds a preview's extracted info stays reusable
START_STAGGER = 0.3      # seconds between worker start-ups (gentle on YouTube)


# ═══════════════════════════════════════════════════════════════
#  LOCAL BIN / FFMPEG DISCOVERY
#  On Windows the auto-installer places ffmpeg.exe in <script>/bin.
#  Prepending that folder to PATH makes it discoverable on every
#  launch without ever touching the system-wide PATH.
# ═══════════════════════════════════════════════════════════════
def _add_local_bin_to_path() -> None:
    bin_dir = SCRIPT_DIR / "bin"
    if bin_dir.is_dir():
        bin_str = str(bin_dir)
        current = os.environ.get("PATH", "")
        if bin_str not in current.split(os.pathsep):
            os.environ["PATH"] = bin_str + os.pathsep + current


def _ffmpeg_location() -> str | None:
    """Absolute path to a locally-installed ffmpeg binary, if present."""
    name = "ffmpeg.exe" if sys.platform == "win32" else "ffmpeg"
    local = SCRIPT_DIR / "bin" / name
    return str(local) if local.exists() else None


def _ffmpeg_bin() -> str:
    """ffmpeg executable to invoke — local install first, else PATH."""
    return _ffmpeg_location() or "ffmpeg"


_add_local_bin_to_path()


# ═══════════════════════════════════════════════════════════════
#  ERROR LOGGING — persists tracebacks so problems are debuggable
#  after the fact, without spraying raw stack traces at the user.
# ═══════════════════════════════════════════════════════════════
def _setup_logger() -> logging.Logger:
    logger = logging.getLogger("ytd")
    logger.setLevel(logging.DEBUG)
    try:
        handler = logging.handlers.RotatingFileHandler(
            DEBUG_LOG_FILE, maxBytes=512_000, backupCount=1, encoding="utf-8",
        )
        handler.setFormatter(logging.Formatter(
            "%(asctime)s [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S",
        ))
        logger.addHandler(handler)
    except Exception:
        logger.addHandler(logging.NullHandler())
    return logger


log = _setup_logger()


# ═══════════════════════════════════════════════════════════════
#  SETTINGS — tiny JSON file next to the script
# ═══════════════════════════════════════════════════════════════
_DEFAULT_SETTINGS = {"parallel": 4, "cookies": "ask"}   # cookies: ask | on | off


def _load_settings() -> dict:
    data = dict(_DEFAULT_SETTINGS)
    try:
        raw = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            p = raw.get("parallel")
            if isinstance(p, int) and 1 <= p <= 16:
                data["parallel"] = p
            if raw.get("cookies") in ("ask", "on", "off"):
                data["cookies"] = raw["cookies"]
    except (OSError, ValueError):
        pass
    return data


def _save_settings() -> None:
    try:
        tmp = SETTINGS_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(SETTINGS, indent=2), encoding="utf-8")
        os.replace(tmp, SETTINGS_FILE)
    except OSError as e:
        log.warning("Could not save settings: %s", e)


SETTINGS = _load_settings()


# ═══════════════════════════════════════════════════════════════
#  CROSS-PLATFORM RESTART HELPER
# ═══════════════════════════════════════════════════════════════
_RESTARTING = False  # set right before a self-relaunch so the Windows
                     # exit-pause knows not to hold the old window open


def _restart_script() -> None:
    """Restart the current script using a platform-appropriate method.

    On Windows, ``os.execv()`` is emulated and doesn't truly replace the
    running process, so we use ``subprocess.Popen`` + ``sys.exit``.  On
    POSIX systems ``os.execv()`` atomically replaces the process image."""
    global _RESTARTING
    _RESTARTING = True
    os.environ["YTD_NO_UPDATE"] = "1"
    script = str(Path(__file__).resolve())
    args = [sys.executable, script] + sys.argv[1:]
    if sys.platform == "win32":
        try:
            subprocess.Popen(args)
        except OSError:
            console.print("[yellow]Please restart the script manually.[/]")
        sys.exit(0)
    else:
        try:
            os.execv(sys.executable, args)
        except OSError:
            console.print("[yellow]Please restart the script manually.[/]")
        sys.exit(0)


# ═══════════════════════════════════════════════════════════════
#  UPDATE CHECKER — runs on every launch, but in the background
#  so it never delays start-up.  Results appear in the menu.
# ═══════════════════════════════════════════════════════════════
def _installed_version(pkg: str) -> str | None:
    try:
        return _pkg_version(pkg)
    except PackageNotFoundError:
        return None


def _pypi_version(pkg: str, timeout: float = 6.0) -> str | None:
    """Fetch the latest version string from PyPI's JSON API."""
    try:
        url = f"https://pypi.org/pypi/{pkg}/json"
        req = urllib.request.Request(
            url, headers={"User-Agent": f"ytd-tui/{APP_VERSION}"}
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return data["info"]["version"]
    except Exception:
        return None


class UpdateWatcher:
    """Checks PyPI on a daemon thread.  ``updates`` stays ``None`` until the
    check finishes, then holds a (possibly empty) list of
    ``(package, installed, latest)`` tuples."""

    def __init__(self) -> None:
        self.updates: list[tuple[str, str | None, str]] | None = None

    def start(self) -> None:
        if _SKIP_UPDATE:
            return
        threading.Thread(target=self._run, name="update-check", daemon=True).start()

    def _run(self) -> None:
        try:
            with concurrent.futures.ThreadPoolExecutor(
                max_workers=len(TRACKED_PACKAGES)
            ) as pool:
                latest = dict(zip(TRACKED_PACKAGES,
                                  pool.map(_pypi_version, TRACKED_PACKAGES)))
            found = []
            for pkg in TRACKED_PACKAGES:
                installed, new = _installed_version(pkg), latest.get(pkg)
                if new and installed != new:
                    found.append((pkg, installed, new))
            self.updates = found
        except Exception as e:
            log.debug("Update check failed: %s", e)
            self.updates = []


UPDATES = UpdateWatcher()


def apply_updates() -> None:
    """Install the updates found by the background check, then restart."""
    updates = UPDATES.updates or []
    if not updates:
        pause("Everything is up to date.")
        return

    console.print()
    console.print("[yellow]Updates available:[/]")
    for pkg, old, new in updates:
        if old:
            console.print(f"  • {pkg}:  [red]{old}[/] → [green]{new}[/]")
        else:
            console.print(f"  • {pkg}:  [green]{new}[/]  (not installed)")
    console.print()

    if not Confirm.ask("[bold]Install updates now?[/]", default=True):
        return

    pkgs = [u[0] for u in updates]
    with console.status(f"[cyan]Installing {', '.join(pkgs)}…[/]", spinner="dots"):
        ok, err = _pip_install("-U", *pkgs)
        if not ok:
            ok, err2 = _pip_install("-U", *pkgs, user=True)
            err = err2 or err
    if not ok:
        console.print("[red]✗ Update installation failed.[/]")
        if err:
            console.print(f"[dim]{esc(err[:400])}[/]")
        console.print(
            "[dim]Try manually:  python -m pip install -U "
            f"{' '.join(pkgs)}[/]"
        )
        pause()
        return

    console.print("[green]✓ Updates installed![/]\n")
    console.print("[cyan]Restarting to apply changes…[/]")
    time.sleep(1.0)
    _restart_script()


# ═══════════════════════════════════════════════════════════════
#  COOKIE SUPPORT — authenticate with YouTube to bypass bot checks
# ═══════════════════════════════════════════════════════════════
def _check_cookies() -> tuple[str, str]:
    """Validate the cookies.txt file next to this script."""
    if not COOKIES_FILE.exists():
        return ("missing", "No cookies.txt found in the script directory")

    try:
        content = COOKIES_FILE.read_text(encoding="utf-8-sig", errors="replace")
    except Exception as e:
        return ("invalid", f"Cannot read cookies file: {e}")

    data_lines: list[str] = []
    for line in content.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            data_lines.append(stripped)

    if not data_lines:
        return ("invalid", "Cookies file is empty or contains only comments")

    yt_lines = [ln for ln in data_lines
                if ".youtube.com" in ln or "youtube.com" in ln]
    if not yt_lines:
        return ("invalid", "No YouTube cookies found in the file")

    now = time.time()
    has_expiry = 0
    expired = 0
    for line in yt_lines:
        fields = line.split("\t")
        if len(fields) >= 7:
            try:
                exp = int(fields[4])
                if exp > 0:
                    has_expiry += 1
                    if exp < now:
                        expired += 1
            except (ValueError, IndexError):
                pass

    if has_expiry > 0 and expired == has_expiry:
        return ("expired",
                "All YouTube cookies have expired — please update cookies.txt")

    return ("valid", f"Cookies OK — {len(yt_lines)} YouTube entries loaded")


_cookie_state: dict = {"resolved": False, "path": None}


def get_cookiefile() -> str | None:
    """Resolve cookie usage once per session (mode set in Settings:
    ask / on / off).  Returns the cookies.txt path or ``None``."""
    if _cookie_state["resolved"]:
        return _cookie_state["path"]

    mode = SETTINGS["cookies"]
    if mode == "off":
        use = False
    elif mode == "on":
        use = True
    else:
        console.print()
        use = Confirm.ask(
            "[bold]Use cookies support?[/]  "
            "[dim](helps avoid bot detection — asked once per session)[/]",
            default=True,
        )

    if not use:
        if mode == "ask":
            console.print("[dim]Proceeding without cookies.[/]")
        _cookie_state.update(resolved=True, path=None)
        return None

    status, message = _check_cookies()
    if status == "valid":
        console.print(f"[green]✓ {message}[/]")
        _cookie_state.update(resolved=True, path=str(COOKIES_FILE))
        return str(COOKIES_FILE)
    if status == "expired":
        console.print(f"[yellow]⚠ {message}[/]")
        _cookie_state.update(resolved=True, path=str(COOKIES_FILE))
        return str(COOKIES_FILE)
    if status == "missing":
        console.print(f"[yellow]⚠ {message}[/]")
        console.print(f"[dim]Place your cookies.txt in:[/]  {SCRIPT_DIR}")
        console.print("[dim]Continuing without cookies.[/]")
    else:
        console.print(f"[red]✗ {message}[/]")
        console.print("[dim]Continuing without cookies.[/]")
    return None   # not cached — you can drop in a cookies.txt and try again


# ═══════════════════════════════════════════════════════════════
#  UI HELPERS
# ═══════════════════════════════════════════════════════════════
def _status_line() -> Text:
    """One compact line of environment status shown under the banner."""
    line = Text(no_wrap=False)
    yt_v = _installed_version("yt-dlp") or "?"
    line.append("yt-dlp ", style="dim")
    line.append(yt_v, style="cyan")
    line.append("  │  ", style="dim")
    if shutil.which("ffmpeg"):
        line.append("ffmpeg ✓", style="green")
    else:
        line.append("ffmpeg ✗", style="red")
    line.append("  │  ", style="dim")
    cookie_mode = SETTINGS["cookies"]
    if cookie_mode == "off":
        line.append("cookies off", style="dim")
    elif COOKIES_FILE.exists():
        line.append("cookies ✓", style="green")
    else:
        line.append("no cookies.txt", style="dim")
    line.append("  │  ", style="dim")
    line.append(f"{SETTINGS['parallel']}× parallel", style="cyan")
    return line


def banner() -> None:
    console.clear()
    title = Text()
    title.append("▶ ", style="bold red")
    title.append("YouTube Downloader", style="bold white")
    title.append(f"  v{APP_VERSION}", style="dim")
    console.print(Panel(Align.center(title), border_style="red", padding=(0, 2)))
    console.print(Align.center(_status_line()))
    console.print(f"[dim]📁 {esc(str(DOWNLOAD_DIR))}[/]", justify="center")
    ups = UPDATES.updates
    if ups:
        names = ", ".join(f"{p} {n}" for p, _, n in ups)
        console.print(Align.center(Text.from_markup(
            f"[yellow]⬆ Updates available:[/] [dim]{esc(names)}[/]  "
            "[yellow]— press U below to install[/]"
        )))
    console.print()


def menu(title: str, options: dict[str, str], back: bool = False) -> str:
    """Render a numbered menu inside a panel and return the chosen key.
    ``back=True`` appends a ``0. ← Back`` entry."""
    table = Table.grid(padding=(0, 1))
    table.add_column(style="bold green", justify="right", width=3)
    table.add_column(overflow="fold")
    for key, label in options.items():
        table.add_row(f"{key}.", label)
    if back:
        table.add_row("0.", "[dim]← Back[/]")
    console.print(Panel(
        table, title=f"[bold]{title}[/]", title_align="left",
        border_style="cyan", padding=(0, 1),
    ))
    choices = list(options.keys()) + (["0"] if back else [])
    many = len(choices) > 9
    return Prompt.ask(
        "[bold]Enter choice[/]" + (f" [dim](0-{len(options)})[/]" if many and back
                                   else f" [dim](1-{len(options)})[/]" if many else ""),
        choices=choices, show_choices=not many, case_sensitive=False,
    )


def pause(msg: str = "") -> None:
    if msg:
        console.print(f"\n[bold yellow]{msg}[/]")
    Prompt.ask("[dim]Press ENTER to continue[/]", default="", show_default=False)


def _fmt_bytes(n: float) -> str:
    n = float(n or 0)
    if n < 1024:
        return f"{n:.0f} B"
    for unit in ("KB", "MB", "GB", "TB"):
        n /= 1024
        if n < 1024 or unit == "TB":
            return f"{n:.1f} {unit}"
    return f"{n:.1f} TB"


def _fmt_dur(sec: float | None) -> str:
    if sec is None or sec < 0:
        return "--:--"
    sec = int(sec)
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def _format_speed(bps: float) -> str:
    """Format bandwidth speed in bytes/sec to a human-readable string."""
    if bps <= 0:
        return "0 B/s"
    if bps < 1024:
        return f"{bps:.0f} B/s"
    if bps < 1024 * 1024:
        return f"{bps / 1024:.1f} KB/s"
    if bps < 1024 * 1024 * 1024:
        return f"{bps / (1024 * 1024):.1f} MB/s"
    return f"{bps / (1024 * 1024 * 1024):.2f} GB/s"



# ═══════════════════════════════════════════════════════════════
#  WINDOWS FFMPEG AUTO-INSTALLER
# ═══════════════════════════════════════════════════════════════
_FFMPEG_WINDOWS_URLS = [
    "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip",
    "https://github.com/BtbN/FFmpeg-Builds/releases/latest/download/ffmpeg-master-latest-win64-gpl.zip",
]


def _download_file_with_progress(url: str, dest: Path) -> bool:
    """Download ``url`` to ``dest`` with a Rich progress bar."""
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": f"ytd-tui/{APP_VERSION}"}
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            total = int(resp.headers.get("Content-Length") or 0)
            with open(dest, "wb") as fh:
                with Progress(
                    TextColumn("[bold cyan]{task.description}[/]"),
                    BarColumn(),
                    DownloadColumn(),
                    TransferSpeedColumn(),
                    TimeRemainingColumn(),
                    console=console,
                    transient=True,
                ) as prog:
                    tid = prog.add_task("ffmpeg", total=total or None)
                    while True:
                        chunk = resp.read(256 * 1024)
                        if not chunk:
                            break
                        fh.write(chunk)
                        prog.update(tid, advance=len(chunk))
        return True
    except Exception as e:
        log.warning("ffmpeg download failed (%s): %s", url, e)
        if dest.exists():
            try:
                dest.unlink()
            except OSError:
                pass
        return False


def _install_ffmpeg_windows() -> bool:
    """Download a static ffmpeg build, place ffmpeg.exe/ffprobe.exe into
    ``<script_dir>/bin`` and put that folder on PATH for this process."""
    import zipfile

    bin_dir = SCRIPT_DIR / "bin"
    extract_dir = bin_dir / "_ffmpeg_tmp"
    try:
        bin_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        console.print(f"[red]✗ Can't create {bin_dir}: {e}[/]")
        return False

    zip_path = bin_dir / "ffmpeg.zip"
    downloaded = False
    for url in _FFMPEG_WINDOWS_URLS:
        console.print(f"[cyan]Downloading ffmpeg from[/]\n  [dim]{url}[/] …")
        if _download_file_with_progress(url, zip_path):
            downloaded = True
            break
        console.print("[yellow]That mirror didn't work, trying the next…[/]\n")

    if not downloaded:
        console.print("[red]✗ Couldn't download ffmpeg from any mirror.[/]")
        return False

    try:
        console.print("[cyan]Extracting archive…[/]")
        shutil.rmtree(extract_dir, ignore_errors=True)
        with zipfile.ZipFile(zip_path) as zf:
            zf.extractall(extract_dir)

        ffmpeg_exe = next(iter(extract_dir.rglob("ffmpeg.exe")), None)
        if not ffmpeg_exe:
            console.print("[red]✗ ffmpeg.exe not found inside the archive.[/]")
            return False

        shutil.copy2(ffmpeg_exe, bin_dir / "ffmpeg.exe")
        ffprobe_exe = next(iter(extract_dir.rglob("ffprobe.exe")), None)
        if ffprobe_exe:
            shutil.copy2(ffprobe_exe, bin_dir / "ffprobe.exe")
    except Exception as e:
        console.print(f"[red]✗ Failed to extract ffmpeg: {e}[/]")
        return False
    finally:
        shutil.rmtree(extract_dir, ignore_errors=True)
        if zip_path.exists():
            try:
                zip_path.unlink()
            except OSError:
                pass

    _add_local_bin_to_path()
    if shutil.which("ffmpeg"):
        console.print("[green]✓ ffmpeg installed and ready![/]")
        return True

    console.print("[yellow]⚠ ffmpeg files were placed but not detected on PATH.[/]")
    return False


def _handle_missing_ffmpeg_windows() -> bool:
    """Explain the missing-ffmpeg situation on Windows; offer auto-install.
    Returns True once ffmpeg is usable, False otherwise."""
    banner()
    console.print(Panel(
        "[bold red]✗ ffmpeg was not found on this PC.[/]\n\n"
        "[dim]ffmpeg is required for merging video+audio tracks and\n"
        "converting audio to MP3/WAV.[/]",
        border_style="red",
    ))
    console.print()
    console.print("[bold]You can install it in any of these ways:[/]")
    console.print("  [green]•[/] Let this app download it automatically  [dim](recommended, ~90 MB)[/]")
    console.print("  [green]•[/] [bold]winget install Gyan.FFmpeg[/]")
    console.print("  [green]•[/] [bold]choco install ffmpeg[/]")
    console.print("  [green]•[/] [bold]scoop install ffmpeg[/]")
    console.print()

    if Confirm.ask("[bold]Auto-download & install ffmpeg now?[/]", default=True):
        if _install_ffmpeg_windows():
            console.print()
            return True

    console.print()
    console.print(
        "[yellow]Install ffmpeg with one of the commands above, "
        "then run this app again.[/]"
    )
    console.print()
    return False



# ═══════════════════════════════════════════════════════════════
#  STARTUP CHECKS
# ═══════════════════════════════════════════════════════════════
def startup_checks() -> None:
    """Verify ffmpeg exists and Termux storage is configured."""
    _add_local_bin_to_path()

    if not shutil.which("ffmpeg"):
        if sys.platform == "win32":
            if not _handle_missing_ffmpeg_windows():
                sys.exit(1)
        else:
            banner()
            console.print(Panel(
                "[bold red]✗ ffmpeg is not installed.[/]\n\n"
                "[dim]ffmpeg is required for merging video+audio tracks and\n"
                "converting audio to MP3/WAV.[/]",
                border_style="red",
            ))
            console.print()
            if IS_TERMUX:
                console.print("Install with:  [bold green]pkg install ffmpeg[/]")
            else:
                managers = [
                    ("apt",    "sudo apt install ffmpeg"),
                    ("dnf",    "sudo dnf install ffmpeg"),
                    ("pacman", "sudo pacman -S ffmpeg"),
                    ("brew",   "brew install ffmpeg"),
                ]
                for cmd, inst in managers:
                    if shutil.which(cmd):
                        console.print(f"Install with:  [bold green]{inst}[/]")
                        break
                else:
                    console.print("Install ffmpeg via your system's package manager.")
            console.print()
            sys.exit(1)

    if IS_TERMUX and not (Path.home() / "storage" / "downloads").is_dir():
        banner()
        console.print(Panel(
            "[bold yellow]⚠ Shared storage is not set up.[/]\n\n"
            "[dim]Termux needs permission to save files to your phone's\n"
            "Downloads folder.[/]",
            border_style="yellow",
        ))
        console.print("\nRunning [bold green]termux-setup-storage[/]…")
        subprocess.run(["termux-setup-storage"])
        console.print(
            "\n[yellow]Please tap 'Allow' on the permission dialog, "
            "then rerun this script.[/]"
        )
        sys.exit(0)

    try:
        DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        log.error("Could not create download directory %s: %s", DOWNLOAD_DIR, e)
        banner()
        console.print(Panel(
            f"[bold red]✗ Can't create the download folder.[/]\n\n"
            f"[dim]{DOWNLOAD_DIR}\n{e}[/]",
            border_style="red",
        ))
        console.print()
        sys.exit(1)

    ensure_history_md_file()




# ═══════════════════════════════════════════════════════════════
#  URL VALIDATION, PARSING & METADATA PREVIEW
# ═══════════════════════════════════════════════════════════════
_YT_PATTERNS = [
    re.compile(p, re.IGNORECASE) for p in [
        r"youtube\.com/watch",
        r"youtu\.be/",
        r"youtube\.com/playlist",
        r"youtube\.com/shorts/",
        r"youtube\.com/show/",
        r"youtube\.com/channel/",
        r"youtube\.com/c/",
        r"youtube\.com/@",
        r"m\.youtube\.com/",
    ]
]

_PLAYLISTISH = re.compile(
    r"youtube\.com/(playlist|show/|channel/|c/|@)", re.IGNORECASE
)
_URL_RE = re.compile(r"https?://[^\s,;<>\"'\]\[]+", re.IGNORECASE)


def is_youtube_url(url: str) -> bool:
    return any(p.search(url) for p in _YT_PATTERNS)


def _looks_like_playlist(url: str) -> bool:
    """Pure playlist/channel links expand to many videos; a watch link that
    merely carries a ``list=`` param is treated as the single video."""
    return bool(_PLAYLISTISH.search(url))


def normalize_youtube_url(url: str) -> str:
    """Normalize YouTube URLs (e.g. show URLs or VL-prefixed playlist IDs)."""
    url = url.strip()
    m = re.search(r"youtube\.com/show/(?:VL)?(PL[a-zA-Z0-9_-]+)", url, re.IGNORECASE)
    if m:
        return f"https://www.youtube.com/playlist?list={m.group(1)}"
    m = re.search(r"list=VL(PL[a-zA-Z0-9_-]+)", url, re.IGNORECASE)
    if m:
        return re.sub(r"list=VLPL[a-zA-Z0-9_-]+", f"list={m.group(1)}", url, flags=re.IGNORECASE)
    return url


def parse_urls(text: str) -> list[str]:
    """Extract every URL from free text (space/comma/newline separated).
    If no scheme-prefixed URL is found the raw text is returned as one
    candidate so ``youtube.com/watch?v=…`` still works."""
    found = _URL_RE.findall(text or "")
    if not found and (text or "").strip():
        found = [text.strip()]
    return found


def _dedupe(urls: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for u in urls:
        u = normalize_youtube_url(u)
        if u and u not in seen:
            seen.add(u)
            out.append(u)
    return out


@lru_cache(maxsize=1)
def _get_js_runtimes() -> dict:
    """Detect available JS runtimes (Node, Deno, QuickJS, Bun) for challenge
    solving.  Cached — the PATH lookups only need to happen once."""
    runtimes = {}
    for rt in ["node", "deno", "quickjs", "bun"]:
        if shutil.which(rt):
            runtimes[rt] = {}
    return runtimes


def _get_ytdlp_base_opts(cookiefile: str | None = None) -> dict:
    """Return base options for YoutubeDL including JS runtimes, the EJS
    challenge solver, and a local ffmpeg binary when one is installed."""
    opts: dict = {
        "remote_components": {"ejs:github": {}},
        "extractor_args": {"youtubetab": {"skip": ["authcheck"]}},
    }
    runtimes = _get_js_runtimes()
    if runtimes:
        opts["js_runtimes"] = dict(runtimes)
    if cookiefile:
        opts["cookiefile"] = cookiefile
    ff = _ffmpeg_location()
    if ff:
        opts["ffmpeg_location"] = ff
    return opts


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _clean_ytdlp_message(msg: str) -> str:
    msg = _ANSI_RE.sub("", str(msg))
    return re.sub(r"^(ERROR|WARNING):\s*", "", msg).strip()


class _ErrorCollector:
    """Stands in for yt-dlp's logger, capturing error text so it can be
    shown properly instead of being swallowed by the live display."""

    def __init__(self) -> None:
        self.errors: list[str] = []

    def debug(self, msg: str) -> None:
        pass

    def warning(self, msg: str) -> None:
        pass

    def error(self, msg: str) -> None:
        self.errors.append(_clean_ytdlp_message(msg))


def _fetch_info_core(
    url: str, is_playlist: bool, cookiefile: str | None = None,
) -> tuple[dict | None, str]:
    """Extract metadata without downloading.  Thread-safe and silent, so it
    can run many-at-once.  Returns ``(info, error_reason)``."""
    yt = ytdlp()
    collector = _ErrorCollector()
    common = {
        "quiet": True, "no_warnings": True, "no_color": True,
        "skip_download": True,
        "noplaylist": not is_playlist,
        "extract_flat": is_playlist,
    }
    try:
        opts = {**_get_ytdlp_base_opts(cookiefile), **common, "logger": collector}
        with yt.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
        if info:
            return info, ""
    except Exception as e:
        log.debug("Primary preview fetch failed for %s: %s", url, e)
        collector.errors.append(_clean_ytdlp_message(str(e)))

    try:
        fallback = {
            **_get_ytdlp_base_opts(None), **common,
            "extractor_args": {"youtube": {"player_client": ["android", "web"]}},
        }
        with yt.YoutubeDL(fallback) as ydl:
            info = ydl.extract_info(url, download=False)
        if info:
            return info, ""
    except Exception as e:
        log.debug("Fallback preview fetch failed for %s: %s", url, e)

    return None, (collector.errors[0] if collector.errors else "unknown reason")


def fetch_info(
    url: str,
    is_playlist: bool,
    cookiefile: str | None = None,
) -> dict | None:
    """Extract metadata with a spinner (for the interactive preview)."""
    with console.status("[cyan]Fetching info…[/]", spinner="dots"):
        info, reason = _fetch_info_core(url, is_playlist, cookiefile)
    if info:
        return info
    console.print(f"[dim]⚠ Couldn't load a preview ({esc(reason)}) — continuing anyway.[/]\n")
    return None


def _info_grid() -> Table:
    grid = Table.grid(padding=(0, 2))
    grid.add_column(style="bold", no_wrap=True)
    grid.add_column(overflow="fold")
    return grid


def show_video_preview(info: dict) -> None:
    title = info.get("title", "Unknown")
    uploader = info.get("uploader") or info.get("channel") or "Unknown"
    duration = info.get("duration") or 0
    views = info.get("view_count")
    up = str(info.get("upload_date") or "")
    grid = _info_grid()
    grid.add_row("Title", Text(str(title)))
    grid.add_row("Channel", Text(str(uploader)))
    if duration:
        grid.add_row("Duration", _fmt_dur(duration))
    if views is not None:
        grid.add_row("Views", f"{views:,}")
    if len(up) == 8 and up.isdigit():
        grid.add_row("Uploaded", f"{up[:4]}-{up[4:6]}-{up[6:]}")
    console.print(Panel(grid, title="[cyan]Video Info[/]", title_align="left",
                        border_style="cyan", padding=(0, 1)))
    console.print()


def _playlist_entries(info: dict | None) -> list[tuple[int, str, str]]:
    """Flat-playlist info → ``[(index, url, title), …]``."""
    entries = []
    for idx, entry in enumerate((info or {}).get("entries") or [], 1):
        if not entry:
            continue
        e_url = entry.get("url") or entry.get("webpage_url")
        v_id = entry.get("id")
        if not e_url and v_id:
            e_url = f"https://www.youtube.com/watch?v={v_id}"
        if e_url:
            title = (entry.get("title") or f"Video {idx}").replace("\n", " ")
            entries.append((idx, e_url, title))
    return entries


def show_playlist_preview(info: dict) -> int:
    title = info.get("title", "Unknown Playlist")
    channel = info.get("uploader") or info.get("channel") or "Unknown"
    count = len([e for e in (info.get("entries") or []) if e])
    grid = _info_grid()
    grid.add_row("Title", Text(str(title)))
    grid.add_row("Channel", Text(str(channel)))
    grid.add_row("Videos", str(count))
    console.print(Panel(grid, title="[cyan]Playlist Info[/]", title_align="left",
                        border_style="cyan", padding=(0, 1)))
    console.print()
    return count



# ═══════════════════════════════════════════════════════════════
#  POSTPROCESSOR DISPLAY NAMES
# ═══════════════════════════════════════════════════════════════
_PP_FRIENDLY_NAMES: dict[str, str] = {
    "Merger":                    "Merging audio + video",
    "MoveFiles":                 "Moving to destination",
    "FFmpegMetadata":            "Embedding metadata",
    "FFmpegExtractAudio":        "Extracting audio",
    "EmbedThumbnail":            "Embedding thumbnail",
    "FFmpegThumbnailsConvertor": "Converting thumbnail",
    "EnsurePlayablePP":          "Checking compatibility",
}


class _CancelledDownload(Exception):
    """Fallback used if this yt-dlp has no DownloadCancelled."""


def _cancel_exc():
    return getattr(ytdlp().utils, "DownloadCancelled", _CancelledDownload)


# ═══════════════════════════════════════════════════════════════
#  LIVE DASHBOARD
#  Hooks only mutate plain state under a lock; rendering happens on
#  Rich's refresh thread (8 Hz).  That decouples the (very chatty)
#  yt-dlp progress callbacks from terminal drawing — a big win on
#  slow terminals such as Termux.
# ═══════════════════════════════════════════════════════════════
_SPIN = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


@dataclass
class _Item:
    key: str
    title: str
    idx: int = 0
    stage: str = "Fetching info…"
    frac: float = 0.0
    cur_done: float = 0.0
    cur_total: float = 0.0
    speed: float = 0.0
    eta: float | None = None
    expected: int = 1                       # 2 when video+audio are separate
    streams: dict = field(default_factory=dict)
    processing: bool = False
    audio_stage: bool = False
    started: float = field(default_factory=time.monotonic)
    path: str = ""


class Dashboard:
    """Thread-safe live view for one or many concurrent downloads."""

    def __init__(self, title: str, total: int, already_done: int = 0) -> None:
        self.lock = threading.RLock()
        self.title = title
        self.total = max(1, total)
        self.done = already_done
        self.existing = already_done
        self.failed = 0
        self.items: dict[str, _Item] = {}
        self.recent: deque = deque(maxlen=8)
        self.failed_keys: set[str] = set()
        self.bytes_done = 0
        self.t0 = time.monotonic()
        self.cancel = threading.Event()
        self.cancelling = False

    # ── state transitions ───────────────────────────────────────
    def _add(self, key: str, title: str, idx: int = 0) -> _Item:
        it = _Item(key=key, title=str(title).replace("\n", " "), idx=idx)
        self.items[key] = it
        return it

    def start(self, key: str, title: str, idx: int = 0) -> None:
        with self.lock:
            if key in self.failed_keys:          # retry pass
                self.failed_keys.discard(key)
                self.failed = max(0, self.failed - 1)
            self._add(key, title, idx)

    def reset(self, key: str, stage: str) -> None:
        """Forget stream progress (new attempt) but keep the row."""
        with self.lock:
            it = self.items.get(key)
            if it:
                it.streams.clear()
                it.frac = it.cur_done = it.cur_total = it.speed = 0.0
                it.eta = None
                it.expected = 1
                it.processing = False
                it.stage = stage

    def finish(self, key: str, ok: bool, note: str = "") -> tuple[str, int]:
        """Mark an item finished.  Returns ``(final_path, size_bytes)``."""
        with self.lock:
            return self._finish_locked(key, ok, note)

    def _finish_locked(self, key: str, ok: bool, note: str = "") -> tuple[str, int]:
        it = self.items.pop(key, None)
        if it is None:
            return "", 0
        size = 0
        if it.path:
            try:
                size = os.path.getsize(it.path)
            except OSError:
                pass
        if ok:
            self.done += 1
            self.bytes_done += size
        else:
            self.failed += 1
            self.failed_keys.add(key)
        if ok:
            note = " · ".join(x for x in (
                _fmt_bytes(size) if size else "",
                _fmt_dur(time.monotonic() - it.started)) if x)
        self.recent.append((ok, it.title, note))
        return it.path, size

    def finish_all(self, ok: bool) -> None:
        """Close any items still open (whole-playlist fallback mode)."""
        with self.lock:
            for key in list(self.items):
                self._finish_locked(key, ok)

    # ── yt-dlp callbacks ────────────────────────────────────────
    def hook(self, key: str, d: dict, dynamic: bool = False) -> None:
        if self.cancel.is_set():
            raise _cancel_exc()("Cancelled by user")
        status = d.get("status")
        fn = d.get("filename") or d.get("tmpfilename") or "?"
        info = d.get("info_dict") or {}
        with self.lock:
            if dynamic:
                key = str(info.get("id") or fn)
                if key not in self.items:
                    self._add(key, info.get("title") or Path(fn).stem,
                              int(info.get("playlist_index") or 0))
            it = self.items.get(key)
            if it is None:
                return

            vc = info.get("vcodec") or "none"
            ac = info.get("acodec") or "none"
            st = it.streams.get(fn)
            if st is None:
                if not it.streams:
                    # First stream seen: video-only means audio follows.
                    it.expected = 2 if (vc != "none" and ac == "none") else 1
                st = it.streams[fn] = {
                    "done": 0, "total": 0, "fin": False,
                    "n": len(it.streams) + 1,
                    "audio": (vc == "none" and ac != "none"),
                }

            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            done = d.get("downloaded_bytes") or 0
            if status == "downloading":
                st["done"] = done
                if total:
                    st["total"] = total
                it.speed = float(d.get("speed") or 0.0)
                it.eta = d.get("eta")
                it.cur_done, it.cur_total = done, st["total"]
            elif status == "finished":
                st["fin"] = True
                st["done"] = st["total"] = total or done or st["total"]
                it.cur_done = it.cur_total = st["total"]
                it.speed = 0.0
                it.eta = 0

            n_fin = sum(1 for s in it.streams.values() if s["fin"])
            if st["fin"]:
                frac = n_fin / it.expected
            else:
                cur = (st["done"] / st["total"]) if st["total"] else 0.0
                frac = (n_fin + min(cur, 1.0)) / it.expected
            it.frac = max(it.frac, min(frac, 1.0))

            it.audio_stage = st["audio"]
            if not it.processing:
                kind = "audio" if st["audio"] else "video"
                it.stage = f"{kind} {st['n']}/{it.expected}" if it.expected > 1 else kind

    def pp_hook(self, key: str, d: dict, dynamic: bool = False) -> None:
        info = d.get("info_dict") or {}
        with self.lock:
            if dynamic:
                key = str(info.get("id") or key)
            it = self.items.get(key)
            if it is None:
                return
            status = d.get("status")
            raw = d.get("postprocessor", "Processing")
            name = _PP_FRIENDLY_NAMES.get(raw, raw)
            if status == "started":
                it.processing = True
                it.stage = f"⚙ {name}"
                it.speed, it.eta, it.frac = 0.0, None, 1.0
            elif status == "finished":
                fp = info.get("filepath") or info.get("_filename")
                if fp:
                    it.path = str(fp)
                if dynamic and raw == "MoveFiles":
                    self._finish_locked(key, True)

    # ── rendering ───────────────────────────────────────────────
    def __rich_console__(self, rc, options):
        width = options.max_width
        height = options.height or rc.size.height or 25
        with self.lock:
            yield self._build(width, height)

    def _build(self, width: int, height: int):
        now = time.monotonic()
        tick = int(now * 10)
        active = sorted(self.items.values(), key=lambda i: (i.idx, i.started))
        speed = sum(i.speed for i in active)
        frac = (self.done + self.failed + sum(i.frac for i in active)) / self.total
        frac = min(1.0, frac)
        elapsed = now - self.t0
        queued = max(0, self.total - self.done - self.failed - len(active))
        compact = self.total <= 1

        # header ---------------------------------------------------------
        rows: list = []
        if self.cancelling:
            rows.append(Text("⏹  Cancelling — stopping workers…", style="bold red"))
        narrow = width < 70
        sep = "  " if narrow else "  │  "
        if not compact:
            s = Text()
            if narrow:
                s.append(f"📊 {self.done}/{self.total}", style="bold green")
                s.append(f"  🔄 {len(active)}", style="cyan")
                s.append(f"  ⏳ {queued}", style="white")
                if self.failed:
                    s.append(f"  ⚠ {self.failed}", style="yellow")
            else:
                s.append(f"📊 {self.done}/{self.total} done", style="bold green")
                if self.existing:
                    s.append(f" ({self.existing} existing)", style="dim")
                s.append(sep, style="dim")
                s.append(f"🔄 {len(active)} active", style="cyan")
                s.append(sep, style="dim")
                s.append(f"⏳ {queued} queued", style="white")
                if self.failed:
                    s.append(sep, style="dim")
                    s.append(f"⚠ {self.failed} skipped", style="yellow")
            rows.append(s)
            # ProgressBar has no trailing newline — give it its own grid row.
            bar_row = Table.grid(expand=True)
            bar_row.add_column(ratio=1)
            bar_row.add_row(ProgressBar(total=100, completed=frac * 100, width=None,
                                        complete_style="cyan", finished_style="green"))
            rows.append(bar_row)
        t = Text()
        t.append(f"⚡ {_format_speed(speed)}", style="bold yellow")
        t.append(sep, style="dim")
        t.append(f"⏱ {_fmt_dur(elapsed)}", style="white")
        if not compact and frac > 0.01 and elapsed > 3 and frac < 1:
            t.append(sep, style="dim")
            t.append(f"ETA ~{_fmt_dur(elapsed * (1 - frac) / frac)}", style="white")
        if self.bytes_done and not narrow:
            t.append(sep, style="dim")
            t.append(f"✓ {_fmt_bytes(self.bytes_done)}", style="green")
        rows.append(t)
        header = Panel(Group(*rows), title=Text(self.title, style="bold cyan"),
                       title_align="left", border_style="cyan", padding=(0, 1))

        # recent results -------------------------------------------------
        recent = list(self.recent)[-3:] if not compact else []
        parts: list = [header]

        # active rows (as many as fit) ----------------------------------
        header_lines = 5 + (1 if self.cancelling else 0) + (2 if not compact else 0)
        avail = height - header_lines - (len(recent) + 1 if recent else 0) - 1
        per = 3 if len(active) * 3 <= avail else 2
        n_show = max(1, avail // per)
        for it in active[:n_show]:
            parts.append(self._render_item(it, width, tick))
            if per == 3:
                parts.append(Text(""))
        if len(active) > n_show:
            parts.append(Text(f"   … +{len(active) - n_show} more active", style="dim"))
        if not active and self.done + self.failed < self.total:
            parts.append(Text(f" {_SPIN[tick % len(_SPIN)]} Preparing…", style="cyan"))

        for ok, title, note in recent:
            g = Table.grid(expand=True, padding=(0, 1))
            g.add_column(width=1, no_wrap=True)
            g.add_column(ratio=1, no_wrap=True, overflow="ellipsis")
            g.add_column(no_wrap=True, justify="right", max_width=max(10, width // 2))
            g.add_row(Text("✓" if ok else "✗", style="green" if ok else "red"),
                      Text(title, style="dim"),
                      Text(note[:60], style="dim" if ok else "red", overflow="ellipsis",
                           no_wrap=True))
            parts.append(g)
        return Group(*parts)

    def _render_item(self, it: _Item, width: int, tick: int):
        processing = it.processing
        color = "yellow" if processing else ("magenta" if it.audio_stage else "green")

        head = Table.grid(expand=True, padding=(0, 1))
        head.add_column(width=1, no_wrap=True)
        head.add_column(ratio=1, no_wrap=True, overflow="ellipsis")
        head.add_column(no_wrap=True, justify="right")
        head.add_row(
            Text(_SPIN[tick % len(_SPIN)], style="yellow" if processing else "cyan"),
            Text(it.title, style="bold", no_wrap=True, overflow="ellipsis"),
            Text(it.stage, style=color),
        )

        show_size = width >= 58
        show_eta = width >= 78
        stats = Table.grid(expand=True, padding=(0, 1))
        stats.add_column(width=1)
        stats.add_column(ratio=1, min_width=8)
        stats.add_column(width=6, justify="right", no_wrap=True)
        if show_size:
            stats.add_column(width=17, justify="right", no_wrap=True)
        stats.add_column(width=10, justify="right", no_wrap=True)
        if show_eta:
            stats.add_column(width=7, justify="right", no_wrap=True)

        pulse = (not processing) and it.cur_total <= 0 and it.frac <= 0
        bar = ProgressBar(total=100, completed=min(100.0, it.frac * 100), width=None,
                          pulse=pulse, complete_style=color, finished_style=color)
        cells = [Text(""), bar, Text(f"{it.frac * 100:4.0f}%", style="bold")]
        if show_size:
            size = (f"{_fmt_bytes(it.cur_done)}/{_fmt_bytes(it.cur_total)}"
                    if it.cur_total else (_fmt_bytes(it.cur_done) if it.cur_done else ""))
            cells.append(Text(size.replace(" ", ""), style="dim"))
        cells.append(Text(_format_speed(it.speed) if it.speed else "", style="yellow"))
        if show_eta:
            cells.append(Text(f"~{_fmt_dur(it.eta)}" if it.eta else "", style="dim"))
        stats.add_row(*cells)
        return Group(head, stats)


def _use_alt_screen() -> bool:
    """Use the terminal's alternate screen buffer where it is reliable,
    otherwise draw inline.

    The alternate screen is not guaranteed on the legacy Windows console
    (cmd.exe / older PowerShell), so there we only enable it inside
    Windows Terminal (which sets ``WT_SESSION``).

    Override with ``YTD_NO_ALT_SCREEN=1`` (force off) or
    ``YTD_FORCE_ALT_SCREEN=1`` (force on)."""
    if os.environ.get("YTD_NO_ALT_SCREEN") == "1":
        return False
    if os.environ.get("YTD_FORCE_ALT_SCREEN") == "1":
        return True
    if sys.platform == "win32" and not os.environ.get("WT_SESSION"):
        return False
    return True


@contextlib.contextmanager
def _live(renderable):
    alt = _use_alt_screen()
    with Live(renderable, console=console, screen=alt, transient=not alt,
              refresh_per_second=8, vertical_overflow="crop"):
        yield


# ═══════════════════════════════════════════════════════════════
#  DOWNLOAD ENGINE — one engine for single / playlist / batch
# ═══════════════════════════════════════════════════════════════
@dataclass
class DownloadResult:
    """Outcome of a download run."""
    ok: bool
    total: int = 1
    failed: int = 0
    errors: list[str] = field(default_factory=list)
    cancelled: bool = False

    @property
    def succeeded(self) -> int:
        return max(0, self.total - self.failed)

    @property
    def partial(self) -> bool:
        return self.failed > 0 and self.succeeded > 0


@dataclass
class Plan:
    """What the user chose: video/audio, yt-dlp options and a label."""
    kind: str                       # "video" | "audio"
    opts: dict
    label: str


@dataclass
class Job:
    """One downloadable item."""
    key: str
    url: str
    title: str
    folder: Path
    idx: int = 0
    src: int = 0                    # index of the Source it came from
    info: dict | None = None        # pre-extracted metadata (reused!)
    info_time: float = 0.0
    info_stale: bool = False
    dynamic: bool = False           # whole-playlist single-call fallback
    hint: int = 1                   # how many videos this job stands for
    ok: bool = False
    error: str = ""
    path: str = ""
    size: int = 0

    def reset(self) -> None:
        self.ok, self.error = False, ""


@dataclass
class Source:
    """One URL the user gave us (video or playlist)."""
    url: str
    is_playlist: bool
    info: dict | None
    title: str
    count: int = 1
    error: str = ""
    info_time: float = field(default_factory=time.time)
    jobs: list[Job] = field(default_factory=list)
    skipped: int = 0


_PERMANENT_HINTS = (
    "private video", "video unavailable", "this video is not available",
    "has been removed", "account associated", "terminated", "members-only",
    "members only", "join this channel", "copyright", "not made this video available",
    "who has blocked", "age-restricted", "confirm your age", "sign in to confirm",
    "premieres in", "live event will begin", "is not a valid url", "unsupported url",
)


def _is_permanent_error(msg: str) -> bool:
    low = (msg or "").lower()
    return any(h in low for h in _PERMANENT_HINTS)


def _short(msg: str, n: int = 160) -> str:
    msg = (msg or "").strip().splitlines()[0] if (msg or "").strip() else ""
    return msg if len(msg) <= n else msg[: n - 1] + "…"


_COMPAT_PP_CLS = None


def _compat_pp_class():
    """Build the H.264/AAC safety-net postprocessor (needs yt-dlp imported,
    so it is created lazily)."""
    global _COMPAT_PP_CLS
    if _COMPAT_PP_CLS is not None:
        return _COMPAT_PP_CLS

    ytdlp()
    from yt_dlp.postprocessor.common import PostProcessor

    class EnsurePlayablePP(PostProcessor):
        """After download/merge, re-encode to H.264/AAC only if needed."""

        def run(self, info):
            filepath = info.get("filepath")
            if not filepath or not os.path.exists(filepath) or info.get("ext") != "mp4":
                return [], info

            vcodec = (info.get("vcodec") or "").lower()
            acodec = (info.get("acodec") or "").lower()
            video_ok = vcodec in ("", "none") or vcodec.startswith(("avc1", "h264"))
            audio_ok = acodec in ("", "none") or acodec.startswith(("mp4a", "aac"))

            if video_ok and audio_ok:
                return [], info

            self.to_screen(
                "Source has no H.264/AAC stream at this quality — "
                "re-encoding for phone compatibility (may take a while)…"
            )
            tmp_path = filepath + ".compat.mp4"
            cmd = [
                _ffmpeg_bin(), "-y", "-i", filepath,
                "-c:v", ("copy" if video_ok else "libx264"),
                *([] if video_ok else ["-preset", "fast", "-crf", "20"]),
                "-c:a", ("copy" if audio_ok else "aac"),
                *([] if audio_ok else ["-b:a", "192k"]),
                "-movflags", "+faststart",
                tmp_path,
            ]
            try:
                subprocess.run(cmd, check=True, capture_output=True)
            except (subprocess.CalledProcessError, OSError, FileNotFoundError) as e:
                self.report_warning(f"Compatibility re-encode failed, keeping original file ({e})")
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
                return [], info

            os.replace(tmp_path, filepath)
            if not video_ok:
                info["vcodec"] = "avc1"
            if not audio_ok:
                info["acodec"] = "mp4a"
            return [], info

    _COMPAT_PP_CLS = EnsurePlayablePP
    return _COMPAT_PP_CLS


def _register_compat_pp(ydl, plan_opts: dict) -> None:
    """Attach the H.264/AAC safety net for video downloads."""
    if plan_opts.get("merge_output_format") == "mp4":
        ydl.add_post_processor(_compat_pp_class()(), when="after_move")


class _Stagger:
    """Spaces worker start-ups ``gap`` seconds apart (without holding a
    lock while sleeping)."""

    def __init__(self, gap: float) -> None:
        self.gap, self.lock, self.next = gap, threading.Lock(), 0.0

    def wait(self, cancel: threading.Event) -> None:
        with self.lock:
            now = time.monotonic()
            at = max(now, self.next)
            self.next = at + self.gap
        if at > now:
            cancel.wait(at - now)


@dataclass
class _Ctx:
    plan_opts: dict
    cookiefile: str | None
    dash: Dashboard
    cancel: threading.Event
    frags: int
    skip_existing: bool


def _job_opts(job: Job, ctx: _Ctx, collector: _ErrorCollector) -> dict:
    """Assemble the yt-dlp options for one job."""
    if job.dynamic:
        outtmpl = str(
            DOWNLOAD_DIR / "%(playlist_title)s" /
            "%(playlist_index)02d - %(title).80s.%(ext)s"
        )
    else:
        outtmpl = str(job.folder / "%(title).80s.%(ext)s")

    pps = list(ctx.plan_opts.get("postprocessors", [])) + [{
        "key": "FFmpegMetadata", "add_chapters": True, "add_metadata": True,
    }]
    dash, dyn, key = ctx.dash, job.dynamic, job.key
    opts: dict = {
        **_get_ytdlp_base_opts(ctx.cookiefile),
        "outtmpl": outtmpl,
        "noplaylist": not job.dynamic,
        "restrictfilenames": True,
        "updatetime": False,
        "continuedl": True,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "no_color": True,
        "logger": collector,
        "progress_hooks": [lambda d: dash.hook(key, d, dyn)],
        "postprocessor_hooks": [lambda d: dash.pp_hook(key, d, dyn)],
        "retries": 15,
        "fragment_retries": 15,
        "extractor_retries": 3,
        "socket_timeout": 60,
        "ignoreerrors": True,
        "concurrent_fragment_downloads": ctx.frags,
        "buffersize": 1024 * 64,
        "http_chunk_size": 10485760,
        **{k: v for k, v in ctx.plan_opts.items()
           if k not in ("cookiefile", "postprocessors")},
        "postprocessors": pps,
    }
    if ctx.skip_existing:
        opts["overwrites"] = False
    return opts


def _download_once(job: Job, ctx: _Ctx, collector: _ErrorCollector) -> int:
    """One download attempt.  Returns 0 on success, -1 if cancelled,
    anything else on failure.

    Fast path: reuse the metadata already extracted for the preview
    (skips a full second YouTube extraction).  If that fails for any
    reason — e.g. stale stream URLs — fall back to a normal fresh run."""
    yt = ytdlp()
    opts = _job_opts(job, ctx, collector)

    def attempt(use_info: bool) -> int:
        with yt.YoutubeDL(opts) as ydl:
            _register_compat_pp(ydl, ctx.plan_opts)
            if use_info:
                try:
                    info = copy.deepcopy(job.info)
                except Exception:
                    info = job.info
                ydl.process_ie_result(info, download=True)
                return getattr(ydl, "_download_retcode",
                               1 if collector.errors else 0)
            return ydl.download([job.url])

    use_cached = (
        bool(job.info) and not job.dynamic and not job.info_stale
        and (time.time() - job.info_time) < INFO_TTL
    )
    if use_cached:
        try:
            ret = attempt(True)
        except Exception as e:
            ret = 1
            if not ctx.cancel.is_set():
                log.debug("Reused-info download failed for %s (%s); retrying fresh",
                          job.url, e)
        if ctx.cancel.is_set():
            return -1
        if ret == 0:
            return 0
        job.info_stale = True
        collector.errors.clear()
        ctx.dash.reset(job.key, "Re-fetching info…")

    try:
        ret = attempt(False)
    except Exception as e:
        if ctx.cancel.is_set():
            return -1
        if not isinstance(e, yt.utils.DownloadError):
            log.exception("Unexpected error downloading %s", job.url)
        collector.errors.append(_clean_ytdlp_message(str(e)))
        ret = 1
    return -1 if ctx.cancel.is_set() else ret


def run_jobs(
    jobs: list[Job],
    plan: Plan,
    cookiefile: str | None,
    concurrency: int,
    title: str,
    already_done: int = 0,
    skip_existing: bool = False,
) -> bool:
    """Run ``jobs`` in parallel behind a live dashboard.  Fills in each
    job's ``ok`` / ``error`` / ``path``.  Returns True if cancelled."""
    dash = Dashboard(title, total=sum(j.hint for j in jobs) + already_done,
                     already_done=already_done)
    concurrency = max(1, min(concurrency, len(jobs)))
    ctx = _Ctx(
        plan_opts=plan.opts, cookiefile=cookiefile, dash=dash, cancel=dash.cancel,
        frags=max(2, min(4, 16 // concurrency)), skip_existing=skip_existing,
    )
    stagger = _Stagger(START_STAGGER)

    def work(job: Job, retry_pass: bool = False) -> None:
        if ctx.cancel.is_set():
            return
        if not retry_pass:
            stagger.wait(ctx.cancel)
            if ctx.cancel.is_set():
                return
        if not job.dynamic:
            dash.start(job.key, job.title, job.idx)
        ok = False
        try:
            for attempt in range(1, AUTO_ATTEMPTS + 1):
                collector = _ErrorCollector()
                ret = _download_once(job, ctx, collector)
                if ctx.cancel.is_set():
                    break
                if ret == 0:
                    ok = True
                    break
                job.error = collector.errors[-1] if collector.errors else "Download failed"
                if _is_permanent_error(job.error):
                    break
                if attempt < AUTO_ATTEMPTS:
                    if not job.dynamic:
                        dash.reset(job.key, f"retrying ({attempt + 1}/{AUTO_ATTEMPTS})…")
                    ctx.cancel.wait(attempt)          # 1s, 2s back-off
        except Exception as e:
            log.exception("Worker crashed on %s", job.url)
            job.error = f"Unexpected error: {e}"
        finally:
            job.ok = ok
            if ok:
                job.error = ""
            if job.dynamic:
                dash.finish_all(ok)
            else:
                job.path, job.size = dash.finish(
                    job.key, ok, "" if ok else _short(job.error, 60))

    cancelled = False
    with _live(dash):
        pool = concurrent.futures.ThreadPoolExecutor(max_workers=concurrency)
        pending = {pool.submit(work, j) for j in jobs}
        try:
            while pending:
                _, pending = concurrent.futures.wait(
                    pending, timeout=0.25,
                    return_when=concurrent.futures.FIRST_COMPLETED)

            # One gentle sequential pass over items that failed under load.
            if len(jobs) > 1:
                for job in jobs:
                    if not job.ok and job.error and not _is_permanent_error(job.error):
                        work(job, retry_pass=True)
        except KeyboardInterrupt:
            cancelled = True
            dash.cancelling = True
            ctx.cancel.set()
            for f in pending:
                f.cancel()
            concurrent.futures.wait(pending, timeout=10)
        finally:
            pool.shutdown(wait=not cancelled, cancel_futures=True)

    if cancelled or ctx.cancel.is_set():
        console.print("\n[yellow]⏹  Cancelled by user.[/]")
        return True
    return False


# ═══════════════════════════════════════════════════════════════
#  ALREADY-DOWNLOADED DETECTION (playlist resume)
# ═══════════════════════════════════════════════════════════════
_VIDEO_EXTS = {"mp4", "mkv", "webm", "mov", "avi", "m4v", "flv", "ts"}
_AUDIO_EXTS = {"mp3", "wav", "m4a", "opus", "aac", "flac", "ogg", "wma"}


def _expected_media_exts(plan_opts: dict) -> set[str]:
    is_audio_job = any(
        pp.get("key") == "FFmpegExtractAudio"
        for pp in plan_opts.get("postprocessors", [])
    )
    return _AUDIO_EXTS if is_audio_job else _VIDEO_EXTS


def _expected_stems(title: str) -> set[str]:
    sanitize_filename = ytdlp().utils.sanitize_filename
    return {
        sanitize_filename(title, restricted=r)[:80].lower()
        for r in (True, False)
    }


def _scan_completed_stems(folders: list[Path | None], allowed_exts: set[str]) -> set[str]:
    stems: set[str] = set()
    for folder in folders:
        if not folder or not folder.is_dir():
            continue
        try:
            for f in folder.iterdir():
                if not f.is_file():
                    continue
                if f.suffix.lower().lstrip(".") not in allowed_exts:
                    continue
                if f.stat().st_size < 100_000:
                    continue
                stems.add(f.stem.lower())
        except Exception:
            pass
    return stems


def _safe_folder(name: str) -> str:
    """Make a playlist title safe as a folder name without changing names
    that were already valid (so earlier downloads are still recognised)."""
    name = name.replace("/", "-").replace("\x00", "")
    if sys.platform == "win32":
        name = re.sub(r'[<>:"\\|?*]', "_", name)
    return name.strip(" .") or "Playlist"


def build_jobs(sources: list[Source], plan: Plan) -> tuple[list[Job], int]:
    """Turn sources into jobs.  Playlists are expanded and entries that
    are already on disk are skipped.  Returns ``(jobs, skipped_total)``."""
    jobs: list[Job] = []
    skipped_total = 0
    allowed_exts = _expected_media_exts(plan.opts)

    for si, src in enumerate(sources):
        src.jobs, src.skipped = [], 0
        if not src.is_playlist:
            j = Job(key=f"{si}:{src.url}", url=src.url, title=src.title,
                    folder=DOWNLOAD_DIR, src=si, info=src.info,
                    info_time=src.info_time)
            src.jobs.append(j)
            jobs.append(j)
            continue

        entries = _playlist_entries(src.info)
        if not entries:
            # No flat listing — let yt-dlp walk the whole playlist itself.
            j = Job(key=f"{si}:{src.url}", url=src.url, title=src.title,
                    folder=DOWNLOAD_DIR, src=si, dynamic=True,
                    hint=max(1, src.count))
            src.jobs.append(j)
            jobs.append(j)
            continue

        pl_title = _safe_folder((src.info or {}).get("title") or "Playlist")
        base_folder = DOWNLOAD_DIR / pl_title
        dest_folder = (DOWNLOAD_DIR / f"{pl_title} [{plan.label}]"
                       if plan.label else base_folder)
        done_stems = _scan_completed_stems([dest_folder, base_folder], allowed_exts)
        for idx, e_url, title in entries:
            if _expected_stems(title) & done_stems:
                src.skipped += 1
                continue
            j = Job(key=f"{si}_{idx}_{e_url}", url=e_url, title=title,
                    folder=dest_folder, idx=idx, src=si)
            src.jobs.append(j)
            jobs.append(j)
        if src.skipped:
            console.print(
                f"  [green]✓ {src.skipped}/{len(entries)} videos already downloaded "
                f"— downloading {len(src.jobs)} remaining[/]\n"
            )
        skipped_total += src.skipped
    return jobs, skipped_total


def print_run_summary(jobs: list[Job]) -> None:
    """Printed after the live view closes (the alternate screen would
    otherwise take all results and errors with it)."""
    if not jobs:
        return
    good = [j for j in jobs if j.ok]
    bad = [j for j in jobs if not j.ok]
    if len(jobs) > 1:
        total_size = sum(j.size for j in good)
        console.print(Rule("[cyan]Summary[/]", style="dim"))
        if good:
            extra = f"  [dim]({_fmt_bytes(total_size)})[/]" if total_size else ""
            console.print(f"  [green]✓ {len(good)} downloaded[/]{extra}")
        for j in bad[:15]:
            console.print(f"  [red]✗ {esc(j.title[:60])}[/] [dim]— {esc(_short(j.error, 90) or 'failed')}[/]")
        if len(bad) > 15:
            console.print(f"  [dim]… and {len(bad) - 15} more[/]")
        console.print()
    else:
        for j in bad:
            if j.error:
                console.print(f"[red]✗ {esc(j.error)}[/]")


def execute(
    sources: list[Source],
    plan: Plan,
    cookiefile: str | None,
    concurrency: int,
    title: str,
) -> DownloadResult:
    """Build jobs for ``sources``, run them, and offer interactive retries."""
    jobs, skipped = build_jobs(sources, plan)
    if not jobs:
        console.print("  [bold green]✓ All videos already downloaded![/]\n")
        return DownloadResult(ok=True, total=max(1, skipped), failed=0)

    total = len(jobs) + skipped
    console.print(Rule(f"[cyan]{esc(title)}[/]", style="dim"))
    attempt = 1
    todo = jobs
    cancelled = run_jobs(todo, plan, cookiefile, concurrency, title,
                         already_done=skipped)
    print_run_summary(todo)

    while True:
        failed_jobs = [j for j in jobs if not j.ok]
        if cancelled or not failed_jobs or attempt > MAX_RETRIES:
            break
        succeeded = total - len(failed_jobs)
        if succeeded > 0:
            prompt = (
                f"[yellow]{succeeded}/{total} downloaded — "
                f"retry the rest? (attempt {attempt}/{MAX_RETRIES})[/]"
            )
        else:
            prompt = (
                f"[yellow]Download didn't complete. Retry? "
                f"(attempt {attempt}/{MAX_RETRIES})[/]"
            )
        if not Confirm.ask(prompt, default=True):
            break
        console.print("[cyan]Retrying — resuming where it left off…[/]\n")
        attempt += 1
        for j in failed_jobs:
            j.reset()
            j.info_stale = True          # fresh stream URLs on retry
        todo = failed_jobs
        cancelled = run_jobs(todo, plan, cookiefile, concurrency, title,
                             already_done=total - len(todo), skip_existing=True)
        print_run_summary(todo)

    failed_jobs = [j for j in jobs if not j.ok]
    multi = len(jobs) > 1
    errors = [(f"{j.title}: {_short(j.error)}" if multi else j.error)
              for j in failed_jobs if j.error][:5]
    return DownloadResult(
        ok=not cancelled and not failed_jobs,
        total=total,
        failed=len(failed_jobs),
        errors=errors,
        cancelled=cancelled,
    )


#  QUALITY DETECTION
# ═══════════════════════════════════════════════════════════════
def _format_filesize(size: int) -> str:
    if size <= 0:
        return ""
    if size < 1024 * 1024:
        return f"{size / 1024:.0f} KB"
    if size < 1024 * 1024 * 1024:
        return f"{size / (1024 * 1024):.0f} MB"
    return f"{size / (1024 * 1024 * 1024):.1f} GB"


def get_available_qualities(info: dict | None) -> list[dict]:
    """Extract unique (height, fps) pairs from the video's format list."""
    if not info or not info.get("formats"):
        return []

    duration = info.get("duration") or 0

    best_audio_size = 0
    for fmt in info["formats"]:
        acodec = fmt.get("acodec", "")
        if not acodec or acodec == "none":
            continue
        size = fmt.get("filesize") or fmt.get("filesize_approx") or 0
        tbr = fmt.get("tbr") or fmt.get("abr") or 0
        if not size and duration and tbr:
            size = int(tbr * 1024 / 8 * duration)
        if size > best_audio_size:
            best_audio_size = size

    seen: set[tuple[int, int]] = set()
    qualities: list[dict] = []
    for fmt in info["formats"]:
        vcodec = fmt.get("vcodec", "")
        if not vcodec or vcodec in ("none", "images"):
            continue
        height = fmt.get("height") or 0
        fps = fmt.get("fps") or 0
        if height <= 0:
            continue
        key = (height, fps)
        if key in seen:
            continue
        seen.add(key)

        vsize = fmt.get("filesize") or fmt.get("filesize_approx") or 0
        vtbr = fmt.get("vbr") or fmt.get("tbr") or 0
        if not vsize and duration and vtbr:
            vsize = int(vtbr * 1024 / 8 * duration)

        total_size = vsize + best_audio_size
        qualities.append({
            "height": height,
            "fps": fps,
            "filesize": total_size,
        })

    qualities.sort(key=lambda q: (q["height"], q["fps"]))
    return qualities


def _quality_label(q: dict) -> str:
    height = q["height"]
    fps = q.get("fps", 0)
    label = f"{height}p"
    if fps > 0:
        fps_val = int(round(fps)) if round(fps, 1) == int(round(fps)) else fps
        label += f" @ {fps_val}fps"
    size_str = _format_filesize(q.get("filesize", 0))
    if size_str:
        label += f"  [dim]~{size_str}[/]"
    return label

# ═══════════════════════════════════════════════════════════════
#  DOWNLOAD HISTORY
# ═══════════════════════════════════════════════════════════════
_HISTORY_MD_HEADER = (
    "# 📥 YouTube Downloader — History\n\n"
    "| Date | Status | Type | Title | URL |\n"
    "|------|--------|------|-------|-----|\n"
)
_HISTORY_ROW_RE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")


def ensure_history_md_file() -> None:
    if HISTORY_MD_FILE.exists():
        return
    try:
        HISTORY_MD_FILE.write_text(_HISTORY_MD_HEADER, encoding="utf-8")
    except Exception as e:
        log.warning("Could not create %s: %s", HISTORY_MD_FILE, e)


def log_history(url: str, info: dict | None, kind: str, success: bool) -> None:
    try:
        title = (info or {}).get("title", "Unknown")
        title = title.replace("|", "\\|").replace("\n", " ")
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        status = "✅ OK" if success else "❌ FAIL"
        ensure_history_md_file()
        with open(HISTORY_MD_FILE, "a", encoding="utf-8") as f:
            f.write(f"| {ts} | {status} | {kind} | {title} | {url} |\n")
    except Exception as e:
        log.warning("Could not write history entry: %s", e)





# ═══════════════════════════════════════════════════════════════
#  FORMAT SELECTION
# ═══════════════════════════════════════════════════════════════
def _concurrent_downloads_prompt() -> int:
    default = SETTINGS["parallel"]
    console.print()
    ans = Prompt.ask(
        f"[bold]Select active concurrent downloads [1-16] (default: {default})[/]",
        default=str(default),
    ).strip()
    try:
        val = int(ans)
        if 1 <= val <= 16:
            return val
    except ValueError:
        pass
    return default


def _video_format(height: int, fps: float, is_playlist: bool) -> str:
    """yt-dlp format string: prefer H.264/AAC at the chosen height/fps and
    degrade gracefully."""
    fps_val = int(round(fps)) if fps > 0 and round(fps, 1) == int(round(fps)) else fps
    h_exact = f"[height={height}]"
    h_max = f"[height<={height}]"
    if fps > 0 and not is_playlist:
        f_exact = f"[fps={fps_val}]"
    elif fps > 0:
        f_exact = f"[fps<={fps_val}]"
    else:
        f_exact = ""

    avc = "[vcodec^=avc1]"
    aac = "[acodec^=mp4a]"
    return "/".join([
        f"bestvideo{h_exact}{f_exact}{avc}+bestaudio{aac}",
        f"bestvideo{h_exact}{avc}+bestaudio{aac}",
        f"bestvideo{h_max}{f_exact}{avc}+bestaudio{aac}",
        f"bestvideo{h_max}{avc}+bestaudio{aac}",
        f"bestvideo{h_exact}{f_exact}+bestaudio",
        f"bestvideo{h_exact}+bestaudio",
        f"bestvideo{h_max}{f_exact}+bestaudio",
        f"bestvideo{h_max}+bestaudio",
        "bestvideo+bestaudio",
        f"best{h_max}",
        "best",
    ])


_BEST_FORMAT = "/".join([
    "bestvideo[vcodec^=avc1]+bestaudio[acodec^=mp4a]",
    "bestvideo[vcodec^=avc1]+bestaudio",
    "best[vcodec^=avc1][acodec^=mp4a]",
    "bestvideo+bestaudio",
    "best",
])


def choose_video(info: dict | None, is_playlist: bool) -> Plan | None:
    """Quality menu from the video's real formats (or sensible defaults)."""
    if info and not is_playlist:
        qualities = get_available_qualities(info)
        if not qualities:
            qualities = _FALLBACK_QUALITIES
            console.print("[dim]Couldn't detect available qualities — showing defaults.[/]\n")
    else:
        qualities = _FALLBACK_QUALITIES

    options: dict[str, str] = {}
    for i, q in enumerate(qualities, 1):
        options[str(i)] = _quality_label(q)
    best_key = str(len(qualities) + 1)
    options[best_key] = "Best available"

    choice = menu("Select Video Quality:", options, back=True)
    if choice == "0":
        return None
    if choice == best_key:
        return Plan("video", {"format": _BEST_FORMAT, "merge_output_format": "mp4"},
                    "Best available")

    q = qualities[int(choice) - 1]
    height, fps = q["height"], q.get("fps", 0)
    fps_val = int(round(fps)) if fps > 0 and round(fps, 1) == int(round(fps)) else fps
    label = f"{height}p" + (f" @ {fps_val}fps" if fps > 0 else "")
    return Plan("video", {"format": _video_format(height, fps, is_playlist),
                          "merge_output_format": "mp4"}, label)


def choose_video_cap() -> Plan | None:
    """Batch mode: videos differ, so pick a maximum height."""
    options = {str(i): f"Up to {h}p" for i, h in enumerate(_BATCH_HEIGHTS, 1)}
    best_key = str(len(_BATCH_HEIGHTS) + 1)
    options[best_key] = "Best available"
    choice = menu("Select Video Quality (applies to every item):", options, back=True)
    if choice == "0":
        return None
    if choice == best_key:
        return Plan("video", {"format": _BEST_FORMAT, "merge_output_format": "mp4"},
                    "Best available")
    h = _BATCH_HEIGHTS[int(choice) - 1]
    return Plan("video", {"format": _video_format(h, 0, True),
                          "merge_output_format": "mp4"}, f"up to {h}p")


def choose_audio() -> Plan | None:
    a = menu("Select Audio Format:", {k: v[0] for k, v in AUDIO_CHOICES.items()}, back=True)
    if a == "0":
        return None
    cfg = AUDIO_CHOICES[a][1]
    postprocessors = [{
        "key": "FFmpegExtractAudio",
        "preferredcodec": cfg["codec"],
        "preferredquality": cfg["quality"],
    }]
    extra: dict = {"format": "bestaudio/best"}
    if cfg.get("art"):
        extra["writethumbnail"] = True
        postprocessors += [
            {"key": "FFmpegThumbnailsConvertor", "format": "jpg"},
            {"key": "EmbedThumbnail"},
        ]
    extra["postprocessors"] = postprocessors
    return Plan("audio", extra, AUDIO_CHOICES[a][0].split("—")[0].strip())


def _run_title(plan: Plan, sources: list[Source], concurrency: int) -> str:
    video = plan.kind == "video"
    if len(sources) == 1 and not sources[0].is_playlist:
        return (f"▶ Downloading video — {plan.label}" if video
                else f"▶ Downloading {plan.label} audio")
    if len(sources) == 1:
        return (f"▶ Downloading playlist videos ({concurrency} parallel) — {plan.label}"
                if video else
                f"▶ Downloading playlist {plan.label} audio ({concurrency} parallel)")
    return (f"▶ Downloading {len(sources)} links ({concurrency} parallel) — {plan.label}"
            if video else
            f"▶ Downloading {len(sources)} links as {plan.label} ({concurrency} parallel)")


def _make_source(url: str, is_playlist: bool, info: dict | None) -> Source:
    title = (info or {}).get("title") or url
    count = 1
    if is_playlist and info:
        count = len([e for e in (info.get("entries") or []) if e]) or 1
    return Source(url=url, is_playlist=is_playlist, info=info,
                  title=str(title).replace("\n", " "), count=count)


def _log_sources(sources: list[Source], result: DownloadResult,
                 base_label: str, batch: bool = False) -> None:
    for src in sources:
        label = base_label + (" (Playlist)" if src.is_playlist
                              else " (Batch)" if batch else "")
        ok = (not result.cancelled) and all(j.ok for j in src.jobs)
        log_history(src.url, {"title": src.title}, label, ok)


# ═══════════════════════════════════════════════════════════════
#  DOWNLOAD FLOWS
# ═══════════════════════════════════════════════════════════════
def simple_flow(url: str, is_playlist: bool, cookiefile: str | None) -> DownloadResult | None:
    """Single video or entire playlist."""
    url = normalize_youtube_url(url)
    if not is_youtube_url(url):
        if not Confirm.ask(
            "[yellow]This doesn't look like a YouTube URL. Try anyway?[/]",
            default=False,
        ):
            return None

    info = fetch_info(url, is_playlist, cookiefile=cookiefile)
    if is_playlist:
        if info:
            count = show_playlist_preview(info)
            if count > 30:
                if not Confirm.ask(
                    f"[yellow]That's {count} videos! Download all of them?[/]",
                    default=False,
                ):
                    return None
    elif info:
        show_video_preview(info)

    console.print()
    kind = menu("Select Content Type:", {"1": "Video", "2": "Audio"}, back=True)
    if kind == "0":
        return None
    console.print()
    plan = choose_video(info, is_playlist) if kind == "1" else choose_audio()
    if plan is None:
        return None

    concurrency = _concurrent_downloads_prompt() if is_playlist else 1
    src = _make_source(url, is_playlist, info)
    console.print()
    result = execute([src], plan, cookiefile, concurrency,
                     _run_title(plan, [src], concurrency))
    _log_sources([src], result, plan.kind.upper())
    return result


def resolve_sources(urls: list[str], cookiefile: str | None) -> list[Source]:
    """Fetch metadata for many links at once (6 in flight)."""
    out: list[Source | None] = [None] * len(urls)

    def work(i: int, url: str):
        is_pl = _looks_like_playlist(url)
        info, err = _fetch_info_core(url, is_pl, cookiefile)
        src = _make_source(url, is_pl, info)
        src.error = "" if info else err
        return i, src

    with Progress(
        SpinnerColumn(style="cyan"),
        TextColumn("[bold]Resolving links[/]"),
        BarColumn(),
        TextColumn("{task.completed}/{task.total}"),
        console=console, transient=True,
    ) as prog:
        tid = prog.add_task("", total=len(urls))
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=min(6, len(urls))
        ) as pool:
            futs = [pool.submit(work, i, u) for i, u in enumerate(urls)]
            for fut in concurrent.futures.as_completed(futs):
                i, src = fut.result()
                out[i] = src
                prog.advance(tid)
    return [s for s in out if s is not None]


def show_queue(sources: list[Source]) -> None:
    table = Table(box=box.SIMPLE_HEAD, header_style="bold cyan",
                  expand=True, pad_edge=False)
    table.add_column("#", justify="right", style="dim", no_wrap=True)
    table.add_column("Title", ratio=1, no_wrap=True, overflow="ellipsis")
    table.add_column("Info", no_wrap=True, justify="right")
    for i, s in enumerate(sources, 1):
        if s.error and not s.info:
            info = Text(f"✗ {_short(s.error, 28)}", style="red")
        elif s.is_playlist:
            info = Text(f"☰ {s.count} videos", style="magenta")
        else:
            info = Text(_fmt_dur((s.info or {}).get("duration")), style="dim")
        table.add_row(str(i), Text(s.title), info)
    console.print(Panel(table, title="[cyan]Queue[/]", title_align="left",
                        border_style="cyan", padding=(0, 1)))
    console.print()


def batch_flow(urls: list[str], cookiefile: str | None) -> DownloadResult | None:
    """Many links (videos and/or playlists) downloaded together."""
    urls = _dedupe(urls)
    if not urls:
        return None
    others = [u for u in urls if not is_youtube_url(u)]
    if others and Confirm.ask(
        f"[yellow]{len(others)} link(s) don't look like YouTube. Try anyway?[/]",
        default=False,
    ) is False:
        urls = [u for u in urls if is_youtube_url(u)]
        if not urls:
            return None

    sources = resolve_sources(urls, cookiefile)
    bad = [s for s in sources if not s.info]
    good = [s for s in sources if s.info]
    show_queue(sources)
    for s in bad:
        log_history(s.url, {"title": s.title}, "VIDEO (Batch)", False)
    if not good:
        pause("✗ None of the links could be loaded.")
        return None
    if bad and not Confirm.ask(
        f"[yellow]{len(bad)} link(s) failed to load. Continue with the other {len(good)}?[/]",
        default=True,
    ):
        return None

    total_videos = sum(s.count for s in good)
    if total_videos > 30 and not Confirm.ask(
        f"[yellow]That's {total_videos} videos! Download all of them?[/]",
        default=False,
    ):
        return None

    kind = menu("Select Content Type:", {"1": "Video", "2": "Audio"}, back=True)
    if kind == "0":
        return None
    console.print()
    plan = choose_video_cap() if kind == "1" else choose_audio()
    if plan is None:
        return None

    concurrency = _concurrent_downloads_prompt()
    console.print()
    result = execute(good, plan, cookiefile, concurrency,
                     _run_title(plan, good, concurrency))
    _log_sources(good, result, plan.kind.upper(), batch=True)
    return result


def ask_urls(mode: str) -> list[str]:
    """Prompt for one URL (single/playlist) or many (batch).  Anything that
    contains several links is returned as a list."""
    console.print()
    urls: list[str] = []
    if mode == "batch":
        console.print(
            "[bold]Paste links[/] [dim]— one per line or separated by spaces.\n"
            "Press ENTER on an empty line to start.[/]"
        )
        while True:
            try:
                line = console.input("[cyan]›[/] ").strip()
            except EOFError:
                break
            if not line:
                break
            urls += parse_urls(line)
    else:
        text = Prompt.ask(
            "[bold]Paste YouTube URL[/]  [dim](or several, separated by spaces)[/]"
        ).strip()
        urls = parse_urls(text)
    return _dedupe(urls)


def start_download(mode: str) -> DownloadResult | None:
    cookiefile = get_cookiefile()
    urls = ask_urls(mode)
    if not urls:
        pause("✗ No URL entered.")
        return None
    if mode != "batch" and len(urls) > 1:
        console.print(f"\n[cyan]Found {len(urls)} links — queueing them as a batch.[/]")
        mode = "batch"
    if mode == "batch":
        try:
            return batch_flow(urls, cookiefile)
        except KeyboardInterrupt:
            console.print("\n[yellow]Cancelled.[/]")
            return None
    return simple_flow(urls[0], mode == "playlist", cookiefile)


# ═══════════════════════════════════════════════════════════════
#  HISTORY VIEW
# ═══════════════════════════════════════════════════════════════
def show_history() -> None:
    console.print()
    if not HISTORY_MD_FILE.exists():
        pause("No download history yet.")
        return
    try:
        lines = HISTORY_MD_FILE.read_text(encoding="utf-8").splitlines()
    except Exception:
        pause("Could not read history file.")
        return

    rows = []
    for line in lines:
        if not line.startswith("|"):
            continue
        protected = line.strip("|").replace("\\|", "\x00")
        cells = [c.strip().replace("\x00", "|") for c in protected.split("|")]
        if len(cells) == 5 and _HISTORY_ROW_RE.match(cells[0]):
            rows.append(cells)

    if not rows:
        pause("History is empty.")
        return

    recent = rows[-20:]
    ok_count = sum(1 for r in rows if "OK" in r[1])
    table = Table(box=box.SIMPLE_HEAD, header_style="bold cyan", expand=True, pad_edge=False)
    table.add_column("When", no_wrap=True, style="dim")
    table.add_column("", no_wrap=True, width=1)
    table.add_column("Type", no_wrap=True)
    table.add_column("Title", ratio=1, no_wrap=True, overflow="ellipsis")
    for ts, status, kind, title, url in recent:
        good = "OK" in status
        table.add_row(ts[5:16], Text("✓" if good else "✗", style="green" if good else "red"),
                      Text(kind, style="cyan"), Text(title, style="" if good else "red"))
    console.print(Panel(
        table, title=f"[cyan]Download History — last {len(recent)}[/]",
        subtitle=f"[dim]{ok_count} ok · {len(rows) - ok_count} failed · {len(rows)} total[/]",
        title_align="left", border_style="cyan", padding=(0, 1),
    ))
    console.print(f"[dim]Full history: {esc(str(HISTORY_MD_FILE))}[/]")
    pause()


# ═══════════════════════════════════════════════════════════════
#  FINISH SCREEN
# ═══════════════════════════════════════════════════════════════
def finished(result: DownloadResult) -> bool:
    console.print()
    if result.cancelled:
        console.print(Panel(
            Align.center("[bold yellow]⏹  Cancelled[/]"),
            border_style="yellow", padding=(0, 2),
        ))
    elif result.ok:
        console.print(Panel(
            Align.center("[bold green]✓  Download Complete![/]"),
            border_style="green", padding=(0, 2),
        ))
        console.print(f"\n[bold]Saved in:[/] {esc(str(DOWNLOAD_DIR))}")
    elif result.partial:
        console.print(Panel(
            Align.center(
                f"[bold yellow]⚠ Partially Complete — "
                f"{result.succeeded}/{result.total} downloaded[/]"
            ),
            border_style="yellow", padding=(0, 2),
        ))
        console.print(f"\n[bold]Saved in:[/] {esc(str(DOWNLOAD_DIR))}")
        if result.errors:
            console.print("\n[bold yellow]Items that failed:[/]")
            for msg in result.errors:
                console.print(f"    • {esc(msg)}")
    else:
        console.print(Panel(
            Align.center("[bold red]✗  Download Failed[/]"),
            border_style="red", padding=(0, 2),
        ))
        if result.errors:
            console.print("\n[bold yellow]Reported reason(s):[/]")
            for msg in result.errors:
                console.print(f"    • {esc(msg)}")
        console.print("\n[bold yellow]Troubleshooting tips:[/]")
        console.print("    • Check your internet connection")
        console.print("    • Verify the URL is correct and the video is public")
        console.print("    • YouTube may have changed — update: "
                      "[green]pip install -U yt-dlp[/]")
        console.print("    • Try a lower quality or different format")
        console.print("    • Try enabling cookies support (place cookies.txt "
                      "next to this script)")
    console.print()
    return Confirm.ask("[bold]Download another?[/]", default=True)


# ═══════════════════════════════════════════════════════════════
#  SETTINGS
# ═══════════════════════════════════════════════════════════════
def settings_menu() -> None:
    while True:
        banner()
        key = menu("Settings", {
            "1": f"Parallel downloads (default):  [bold cyan]{SETTINGS['parallel']}[/]",
            "2": f"Cookies:  [bold cyan]{SETTINGS['cookies']}[/]  [dim](ask → on → off)[/]",
        }, back=True)
        if key == "0":
            return
        if key == "1":
            ans = Prompt.ask("[bold]Default parallel downloads [1-16][/]",
                             default=str(SETTINGS["parallel"])).strip()
            if ans.isdigit() and 1 <= int(ans) <= 16:
                SETTINGS["parallel"] = int(ans)
        else:
            order = ["ask", "on", "off"]
            SETTINGS["cookies"] = order[(order.index(SETTINGS["cookies"]) + 1) % 3]
            _cookie_state.update(resolved=False, path=None)
        _save_settings()


# ═══════════════════════════════════════════════════════════════
#  MAIN MENU
# ═══════════════════════════════════════════════════════════════
def main_menu() -> DownloadResult | str | None:
    banner()
    options = {
        "1": "Single Video",
        "2": "Entire Playlist",
        "3": "Multiple URLs  [dim](batch, downloaded in parallel)[/]",
        "4": "View Download History",
        "5": "Settings",
        "6": "Exit",
    }
    if UPDATES.updates:
        options["u"] = "[yellow]⬆ Install updates[/]"
    choice = menu("What do you want to do?", options).lower()

    if choice == "4":
        show_history()
        return None
    if choice == "5":
        settings_menu()
        return None
    if choice == "6":
        return "exit"
    if choice == "u":
        apply_updates()
        return None
    return start_download({"1": "single", "2": "playlist", "3": "batch"}[choice])


# ═══════════════════════════════════════════════════════════════
#  ENTRY POINT
# ═══════════════════════════════════════════════════════════════
def _prefetch_ytdlp() -> None:
    try:
        ytdlp()
    except BaseException as e:      # never let a background import kill the app
        log.debug("yt-dlp prefetch failed: %s", e)


def main() -> None:
    try:
        # Everything slow happens off the main thread while the menu is
        # already usable.
        threading.Thread(target=_prefetch_ytdlp, name="ytdlp-import", daemon=True).start()
        UPDATES.start()

        startup_checks()

        while True:
            result = main_menu()
            if result is None:
                continue
            if result == "exit":
                break
            if not finished(result):
                break

        console.print("\n[bold magenta]Goodbye! 👋[/]\n")
    except (KeyboardInterrupt, EOFError):
        console.print("\n[bold magenta]Goodbye! 👋[/]\n")
    except Exception:
        log.exception("Unhandled error")
        console.print(
            f"\n[bold red]✗ Something unexpected went wrong.[/]\n"
            f"[dim]Details were saved to {DEBUG_LOG_FILE.name} "
            f"— feel free to check it or report the issue.[/]\n"
        )
        sys.exit(1)


if __name__ == "__main__":
    _exit_code = 0
    try:
        main()
    except SystemExit as _e:
        _exit_code = _e.code if isinstance(_e.code, int) else (0 if _e.code is None else 1)
    except (KeyboardInterrupt, EOFError):
        _exit_code = 0

    # On Windows, keep the console window open long enough to read the
    # result — especially when launched by double-clicking the file.
    # Skipped automatically when the app is relaunching itself.
    if (
        sys.platform == "win32"
        and not _RESTARTING
        and os.environ.get("YTD_NO_PAUSE") != "1"
    ):
        try:
            input("\nPress Enter to close this window…")
        except (EOFError, KeyboardInterrupt):
            pass

    sys.exit(_exit_code)
