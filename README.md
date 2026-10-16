# Lyrics Overlay

A sleek, modern, real-time synced lyrics overlay for Linux desktop environments (Wayland & X11) powered by MPRIS2.

---

## Features

* **Universal Player Support:** Automatically hooks into any playing MPRIS2 media player (Spotify, YouTube Music, Firefox, Chrome, mpv, Strawberry, Elisa, etc.).
* **Multi-Provider Search:** Parallel lyric scraping across LRCLIB, Musixmatch, Megalobiz, YouTube Music, KuGou, NetEase, and QQ Music with smart fallback and local disk caching.
* **Smart Syllable-Based Word Sync:** Smooth word-by-word karaoke sweeps on plain LRC lyrics using script-aware syllable estimation (English/Latin & Devanagari/Indic) and active vocal windowing.
* **Dynamic Backgrounds:** Live album art color extraction with smooth drifting color blob animations, soft Gaussian blur, or fully transparent overlay.
* **Flexible Layouts:**
* `Focus` — Compact centered view with smooth spring physics scrolling.
* `Scroll` — Multi-line browser layout with interactive mouse-wheel navigation.
* `Minimal` — Single-line sleek status bar mode.


* **Singer Color Differentiation:** Color coding for multi-singer/duet tracks (`M:`, `F:`, `D:` tags).
* **CLI & Socket Control:** Control visibility, switch modes, or adjust sync offsets on the fly via WM keybindings using `lyrics_overlay.py ctl`.

---

## Dependencies & Installation

### 1. System Dependencies

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

---

### 2. Optional Lyric Sources (Musixmatch & YouTube Music)

To enable Musixmatch, Megalobiz, and YouTube Music providers, run the built-in installer. This installs `syncedlyrics` and `ytmusicapi` into the app's user directory without requiring root or a virtual environment:

```bash
python lyrics_overlay.py install-extras

```

---

## Running the Application

Clone the repository and run the main script:

```bash
git clone https://github.com/your-username/lyrics-overlay.git
cd lyrics-overlay
python lyrics_overlay.py

```

To test the UI without launching a media player:

```bash
python lyrics_overlay.py --demo

```

---

## Window Manager & Desktop Environment Compatibility

The overlay is packaged with `app_id` / `WM_CLASS` set to **`lyrics-overlay`**.

### Hyprland (Wayland)

Add the following window rules to your `hyprland.conf`:

```ini
# Floating overlay setup
windowrulev2 = float, class:^(lyrics-overlay)$
windowrulev2 = pin, class:^(lyrics-overlay)$
windowrulev2 = noinitialfocus, class:^(lyrics-overlay)$
windowrulev2 = nofocus, class:^(lyrics-overlay)$
windowrulev2 = borderSize 0, class:^(lyrics-overlay)$

# Optional: Position and size
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

### KDE Plasma & GNOME (Wayland / X11)

* **Out of the box:** The overlay runs as a borderless window stay-on-top.
* **KDE Plasma:** Create a Window Rule (`System Settings -> Window Management -> Window Rules`) matching `Window class = lyrics-overlay` to force floating and keep on all desktops.
* **GNOME:** Use the *Auto Move Windows* or *Window Rules* GNOME extension if you want to force the overlay to stick across all workspaces.

### i3 / bspwm / X11 WMs

Add to your WM config file (e.g., `~/.config/i3/config`):

```i3
for_window [class="lyrics-overlay"] floating enable, sticky enable, border pixel 0

```

---

## CLI & Keybindings

Control the overlay using the socket command interface while it is running:

| Command | Action |
| --- | --- |
| `python lyrics_overlay.py ctl toggle` | Show / Hide the overlay |
| `python lyrics_overlay.py ctl next-mode` | Cycle through Minimal, Focus, and Scroll layouts |
| `python lyrics_overlay.py ctl mode <minimal|focus|scroll>` | Set a specific layout mode |
| `python lyrics_overlay.py ctl next-bg` | Cycle background styles (Blur, Blobs, Off) |
| `python lyrics_overlay.py ctl offset +200` | Shift lyrics 200 ms earlier |
| `python lyrics_overlay.py ctl offset -200` | Shift lyrics 200 ms later |
| `python lyrics_overlay.py ctl offset reset` | Reset track sync offset |
| `python lyrics_overlay.py ctl reload` | Force-reload lyrics from network |

### Hyprland Keybinding Example

```ini
bind = SUPER SHIFT, L, exec, python /path/to/lyrics_overlay.py ctl toggle
bind = SUPER SHIFT, M, exec, python /path/to/lyrics_overlay.py ctl next-mode

```

---

## Mouse Controls

* **Drag:** Left-click and drag to move the overlay anywhere on screen.
* **Right Click:** Open the context menu to tweak fonts, background blur, alignment, player pinning, and lyric provider priorities.
* **Click Line:** Click any lyric line to jump directly to that timestamp in your player (if player supports seeking).
* **Scroll Wheel:** Browse lyrics manually (Scroll layout) or change font size with `Ctrl + Wheel`.
