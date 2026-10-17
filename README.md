# Lyrics Overlay

A sleek, modern, real-time synced lyrics overlay for **Linux** (Wayland & X11) and **Windows** (10 & 11).

---

## Features

* **Cross-Platform Player Support:**
* **Linux:** Automatically hooks into any MPRIS2 media player (Spotify, YouTube Music, Firefox, Chrome, mpv, Strawberry, Elisa, etc.).


* **Windows:** Seamlessly tracks media via Windows System Media Transport Controls (SMTC) for Spotify, YouTube Music, Chrome, Edge, Firefox, VLC, and Windows Media Player.


* **Multi-Provider Search:** Parallel lyric scraping across LRCLIB, Musixmatch, Megalobiz, YouTube Music, KuGou, NetEase, and QQ Music with smart fallback and local disk caching.


* **Smart Syllable-Based Word Sync:** Smooth word-by-word karaoke sweeps on plain LRC lyrics using script-aware syllable estimation (English/Latin & Devanagari/Indic) and active vocal windowing.


* **Dynamic Backgrounds:** Live album art color extraction with smooth drifting color blob animations, soft Gaussian blur, or fully transparent overlay.


* **Flexible Layouts:**

* `Focus` — Compact centered view with smooth spring-physics scrolling.


* `Scroll` — Multi-line browser layout with interactive mouse-wheel navigation.


* `Minimal` — Single-line sleek status bar mode.




* **Singer Color Differentiation:** Color coding for multi-singer/duet tracks (`M:`, `F:`, `D:` tags).


* **CLI & IPC Control:** Control visibility, switch modes, or adjust sync offsets on the fly via socket commands or WM keybindings.


## Dependencies & Installation

### 1. Windows Setup

Requirements: **Python 3.9+** (Ensure *"Add python.exe to PATH"* is checked during installation).

Open Command Prompt or PowerShell and install the required dependencies:

```cmd
pip install PyQt6 winsdk syncedlyrics ytmusicapi

```

---

### 2. Linux Setup

Requirements: **Python 3.9+**, **PyQt6**, **python-dbus**, and **qt6-wayland** (if running on Wayland).

#### Arch Linux / Manjaro

```bash
sudo pacman -S python python-pyqt6 python-dbus qt6-wayland

```

#### Ubuntu / Debian / Pop!_OS

```bash
sudo apt update
sudo apt install python3 python3-pyqt6 python3-dbus qt6-wayland

```

#### Fedora

```bash
sudo dnf install python3 python3-qt6 python3-dbus qt6-qtwayland

```

#### Optional Lyric Sources (Musixmatch & YouTube Music)

To enable Musixmatch, Megalobiz, and YouTube Music providers on Linux, run the built-in dependency installer:

```bash
python lyrics_overlay_linux.py install-extras

```

---

## Running the Application

### On Windows

```cmd
python lyrics_overlay_win.py

```

### On Linux

```bash
python lyrics_overlay_linux.py

```

### Testing UI in Demo Mode

To test the overlay rendering without launching a media player:

* **Windows:** `python lyrics_overlay_win.py --demo`
* **Linux:** `python lyrics_overlay_linux.py --demo`


---

## Linux Window Manager Setup

The Linux overlay runs with `app_id` / `WM_CLASS` set to **`lyrics-overlay`**.

### Hyprland (Wayland)

Add to your `hyprland.conf`:

```ini
windowrulev2 = float, class:^(lyrics-overlay)$
windowrulev2 = pin, class:^(lyrics-overlay)$
windowrulev2 = noinitialfocus, class:^(lyrics-overlay)$
windowrulev2 = nofocus, class:^(lyrics-overlay)$
windowrulev2 = borderSize 0, class:^(lyrics-overlay)$
windowrulev2 = size 820 250, class:^(lyrics-overlay)$
windowrulev2 = move 50% 88%, class:^(lyrics-overlay)$

```

### Sway (Wayland)

Add to `~/.config/sway/config`:

```sway
for_window [app_id="lyrics-overlay"] {
    floating enable
    sticky enable
    border none
}

```

### i3 / bspwm / X11 WMs

Add to `~/.config/i3/config`:

```i3
for_window [class="lyrics-overlay"] floating enable, sticky enable, border pixel 0

```

---

## CLI & Keybindings

Control the running overlay instance from terminal, scripts, or hotkey daemons:

| Command (Linux / Windows) | Action |
| --- | --- |
| `python <script> ctl toggle` | Show / Hide the overlay

 |
| `python <script> ctl next-mode` | Cycle through Minimal, Focus, and Scroll layouts

 |
| `python <script> ctl mode <minimal|focus|scroll>` | Set a specific layout mode

 |
| `python <script> ctl next-bg` | Cycle background styles (Blur, Blobs, Off)

 |
| `python <script> ctl offset +200` | Shift lyrics 200 ms earlier

 |
| `python <script> ctl offset -200` | Shift lyrics 200 ms later

 |
| `python <script> ctl offset reset` | Reset track sync offset

 |
| `python <script> ctl reload` | Force-reload lyrics from network

 |

*(Replace `<script>` with `lyrics_overlay_win.py` on Windows or `lyrics_overlay_linux.py` on Linux)*

---

## Mouse Controls & Window Resizing

* **Move Overlay:** Left-click and drag anywhere inside the overlay.


* **Resize Overlay:** Drag any window edge or corner to resize the width and height.
* **Context Menu:** Right-click to access fonts, background blur, alignment, player pinning, and provider settings.


* **Timestamp Seek:** Click any lyric line to jump directly to that part of the track.


* **Font Scaling:** Hold `Ctrl` and scroll the mouse wheel to scale text size on the fly.


* **Manual Scroll:** Scroll mouse wheel in `Scroll` mode to browse full lyrics.
