# ▶ YouTube Downloader — Terminal UI  `v4.1`

A production-ready terminal UI for downloading **videos and audio** from YouTube.
Built on [yt-dlp](https://github.com/yt-dlp/yt-dlp) + [Rich](https://github.com/Textualize/rich).

Works on **Android (Termux), Linux, macOS, and Windows 10/11**.

> **What's new in v4.1** — failures are now explained in plain English with a
> suggested fix, and saved to `failed_links.txt` for an easy retry. *Best available*
> is no longer capped at 1080p (1440p/2160p now reachable), batch mode can target
> 30/60fps, pasted links report duplicates and non-links, and same-titled videos no
> longer overwrite each other.

```
══════════════════════════════════════════════════════════════
▶  YouTube Downloader — Termux / Desktop  (Production v4.1)
══════════════════════════════════════════════════════════════
```

---

## Features

- **Clear failure reports** — every error gets a plain-English headline and a suggested fix, and failed links are saved to `failed_links.txt` so you can retry them later.
- **Quality fix** — *Best available* is no longer capped at 1080p (H.264 is only a tie-breaker), so 1440p/2160p downloads are reachable. Batch mode also offers 720p/1080p at 30 or 60fps.
- **Smart link handling** — pasting text reports anything that isn't a link and any duplicates, and a video already inside a pasted playlist is skipped instead of downloaded twice.
- **No silent overwrites** — two different videos with the same title are saved with their video ID in the filename.
- **Honest playlists** — private/deleted playlist entries are counted and reported instead of being mistaken for "already downloaded".
- **Preflight check** — warns before starting if the download folder is unwritable or nearly full.
- **Won't crash out** — an unexpected error returns you to the menu with details in `debug.log`.
- **Single video, full playlist, or batch of many links** — all handled by one engine.
- **Parallel downloads** with a live dashboard: overall progress, per-item bars, speed, ETA, recent results.
- **Queue multiple links at once** — paste them and they download in parallel (default 4×, configurable).
- **Fast start-up** — yt-dlp loads in the background while the menu is already on screen.
- **Resume support** — interrupted downloads pick up where they left off. Clean `Ctrl+C` stops all workers.
- **Audio extraction** — WAV (lossless), MP3 128 kbps, or MP3 320 kbps with cover art.
- **H.264 / AAC safety net** — re-encodes when needed so files play everywhere.
- **Skip already-downloaded** items in playlists.
- **Cookie authentication** — download age/members-only content using your `cookies.txt` (ask / on / off modes).
- **Automatic dependency management** — installs `yt-dlp` / `rich` if missing, ffmpeg on Windows, and **Python itself** on Windows via the `yt.bat` launcher.
- **Download history** — everything logged to `history.md`.
- **Cross-platform** — Termux storage setup and Windows ffmpeg auto-install included.

---

## Requirements

- **Python 3.9+**
- **ffmpeg** on your `PATH` (the app can auto-install it on Windows, and prompts on Termux/Linux)
- Python packages: `yt-dlp`, `rich` (the app installs these automatically if missing)

---

## Installation & Setup

### Android — Termux

```bash
# 1. Install core packages
pkg update && pkg upgrade
pkg install python ffmpeg git

# 2. Give Termux access to shared storage (needed to save to /sdcard)
termux-setup-storage

# 3. Clone the repo
git clone https://github.com/dearprince147-ops/youtube-va-downloader.git
cd youtube-va-downloader

# 4. (Optional) pre-install deps — the app auto-installs them too
pip install yt-dlp rich

# 5. Run
python app.py
```

Downloads go to `~/storage/downloads/YouTube/`.

### Linux (Debian / Ubuntu)

```bash
sudo apt update
sudo apt install python3 python3-pip ffmpeg git

git clone https://github.com/dearprince147-ops/youtube-va-downloader.git
cd youtube-va-downloader
pip install yt-dlp rich
python3 app.py
```

### Linux (Arch / Fedora)

```bash
# Arch
sudo pacman -S python python-pip ffmpeg git
# Fedora
sudo dnf install python3 python3-pip ffmpeg git

git clone https://github.com/dearprince147-ops/youtube-va-downloader.git
cd youtube-va-downloader
python3 app.py
```

Downloads go to `~/Downloads/YouTube/`.

### macOS

```bash
brew install python ffmpeg git
git clone https://github.com/dearprince147-ops/youtube-va-downloader.git
cd youtube-va-downloader
pip3 install yt-dlp rich
python3 app.py
```

### Windows 10 / 11

1. Install **Python 3.9+** from [python.org](https://www.python.org/downloads/) — tick
   *"Add Python to PATH"* during install.
2. Install **ffmpeg** — either:
   - `winget install Gyan.FFmpeg`, **or**
   - do nothing: the app offers to download and install a static ffmpeg build for you on first run.
3. Clone the repo (Git for Windows, or download the ZIP from GitHub):
   ```powershell
   git clone https://github.com/dearprince147-ops/youtube-va-downloader.git
   cd youtube-va-downloader
   ```
4. **Double-click `yt.bat`** — that's it. See below for the one-tap setup.

Downloads go to `%USERPROFILE%\Downloads\YouTube\`.

#### Windows launcher (`yt.bat`)

**Just double-click `yt.bat`.** It handles everything:

1. **Finds Python.** If Python is missing, it installs it automatically —
   first via `winget` (`Python.Python.3.12`), and if winget isn't available, by
   downloading and running the official python.org installer with
   *"Add python.exe to PATH"* enabled. It then relaunches itself so the new
   PATH takes effect.
2. **Adds a `yt` command to your PATH.** A small `yt.cmd` shim is written to
   `%LOCALAPPDATA%\Microsoft\WindowsApps` (already on every user's PATH), so
   **no admin rights are needed**. On the first run it prints a tip; after that
   it is silent.
3. **Launches the app** in **Windows Terminal** using **PowerShell**
   (`wt.exe -d "<app folder>" powershell.exe`), falling back to a plain
   PowerShell window if Windows Terminal isn't installed.

After the first run you can start the app any of these ways:

- press **Win+R**, type **`yt`**, press **Enter**,
- type **`yt`** in any Terminal / PowerShell / cmd window,
- or double-click **`yt.bat`** again.

> `-NoExit` keeps the terminal open if the app ever crashes, so you can read the
> error instead of the window vanishing. The app then installs `yt-dlp`, `rich`,
> and ffmpeg on its own as needed.

#### Run without the launcher (manual)

```powershell
python app.py
```

Or double-click `app.py` directly — its console window stays open at the end so
you can read the results.

---

## Usage

Run the app and pick from the menu:

```
What do you want to do?
 1  Single Video
 2  Entire Playlist
 3  Multiple URLs  (batch, downloaded in parallel)
 4  View Download History
 5  Settings
 6  Exit
```

- **Single Video** — shows a preview (title, duration, channel, available qualities), pick quality or audio-only, confirm.
- **Entire Playlist** — previews the playlist, choose a quality cap, and it skips anything already downloaded.
- **Multiple URLs** — paste any number of links (one per line or space-separated); they queue and download in parallel. Anything that isn't a link and any duplicates are reported, and a video already inside a pasted playlist is skipped.
- **Settings** — change parallel-download count and cookie mode.
- **Install updates** — appears in the menu when a newer `yt-dlp` is available.

Press `Ctrl+C` at any time for a clean stop — partial files resume next run.

### When a download fails

Each failure is shown with a plain-English reason and a suggested fix, for example:

| What happened | What the app tells you |
|---------------|------------------------|
| Private / deleted video | *Only the owner can watch it — cookies from an account with access may help.* |
| Age-restricted | *Turn on cookies (Settings) with a logged-in `cookies.txt`.* |
| "Sign in to confirm you're not a bot" | *Use a fresh `cookies.txt` and update yt-dlp.* |
| Members-only | *Needs a channel membership — use cookies from that account.* |
| Geo-blocked | *A VPN may help.* |
| Rate-limited (HTTP 429) | *Lower the parallel count (Settings) and retry in a few minutes.* |
| Disk full / no permission | *Free up storage* / *run `termux-setup-storage`.* |
| yt-dlp out of date | *Update yt-dlp (press U in the menu if shown) and retry.* |

Every failed link is also written to **`failed_links.txt`** in the project folder, so you
can open **Multiple URLs** and paste them straight back in to retry.

### Settings (`settings.json`)

A small JSON file is created next to the script:

```json
{
  "parallel": 4,
  "cookies": "off"
}
```

| Key | Values | Meaning |
|-----|--------|---------|
| `parallel` | integer | How many downloads run at once (default `4`). |
| `cookies` | `ask` / `on` / `off` | Whether to use `cookies.txt`. `ask` prompts each session. |

---

## Cookies (age-restricted / private / members-only videos)

If a video fails with a "sign in to confirm you're not a bot" or age-gate error, use cookies:

1. Install a browser extension that exports **Netscape-format** cookies
   (e.g. *"Get cookies.txt LOCALLY"*).
2. Log into YouTube in that browser, then export cookies for `youtube.com`.
3. Save the exported file as **`cookies.txt`** in the same directory as `app.py`.
4. In the app, set **Settings → Cookies → on** (or leave it on `ask`).

> ⚠️ `cookies.txt` contains live session credentials. **Never commit or share it.** It is excluded by `.gitignore`.

---

## Termux Widget Setup

Run the downloader with a single tap from your Android home screen using
[Termux:Widget](https://github.com/termux/termux-widget).

1. **Install Termux:Widget** (from F-Droid) and the base **Termux** app.
2. **Create the shortcut directory and symlink the included launcher:**
   ```bash
   mkdir -p ~/.shortcuts
   ln -sf ~/youtube-va-downloader/yt_dlp ~/.shortcuts/yt_dlp
   chmod +x ~/youtube-va-downloader/yt_dlp
   ```
   *(Adjust the path to wherever you cloned the repo.)* The launcher `cd`s into its own
   directory and runs `app.py`, so it works from any clone location without editing.

3. **Add the widget to your home screen:**
   - Long-press an empty spot on your home screen → **Widgets** → **Termux:Widget**.
   - The widget lists every executable script in `~/.shortcuts/`.
   - Tap **`yt_dlp`** to launch the downloader in a Termux session.

4. **Optional — put it in a folder/task group:** scripts inside `~/.shortcuts/tasks/`
   (for the `Termux:Tasker` plugin) or subfolders of `~/.shortcuts/` appear as groups.

> Tip: to make the shortcut appear with a nicer name, use a filename like `YouTube Downloader`
> (spaces are allowed) or create a matching icon in `~/.shortcuts/icons/`.

---

## Troubleshooting

| Problem | Fix |
|---------|-----|
| `ffmpeg not found` | `pkg install ffmpeg` (Termux) / `apt install ffmpeg` (Linux) / `winget install Gyan.FFmpeg` (Windows). The app prompts on Windows and can install it for you. |
| "Sign in to confirm you're not a bot" | Add `cookies.txt` and set Cookies to **on**. |
| Downloads fail after a while | Update yt-dlp: run the menu's **Install updates**, or `pip install -U yt-dlp`. |
| Can't write to `/sdcard` on Termux | Run `termux-setup-storage` and grant permission. |
| Some videos failed | Read the failure panel — each one says why and what to do. Then retry from `failed_links.txt`. |
| Something crashed | Check `debug.log` in the project folder. |

---

## Credits

Built on the excellent [yt-dlp](https://github.com/yt-dlp/yt-dlp) and [Rich](https://github.com/Textualize/rich).

## License

MIT — do what you want, no warranty.
