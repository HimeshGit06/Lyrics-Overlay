#!/usr/bin/env python3
"""
Lyrics Vocal Tagger — Interactive vocal tagging tool for Lyrics Overlay.
Allows tagging lines as Male (M), Female (F), or Duet (D) in real-time or line-by-line.

Controls:
  * [M]           : Tag line as Male (Blue)
  * [F]           : Tag line as Female (Pink)
  * [D]           : Tag line as Duet (Purple)
  * [Space] / [-] : Clear tag (Neutral)
  * [Backspace]   : Clear and move to previous line
  * [Up / Down]   : Select previous / next line
  * [Click line]  : Seek player directly to that line's timestamp
  * [Ctrl+S]      : Save tags to vocal_tags.json and refresh lyrics overlay
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

# Add current directory to path so it can import lyrics_overlay
BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from PyQt6.QtCore import QObject, QPoint, QRectF, QSize, Qt, QThread, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QIcon, QKeySequence, QPainter, QPalette, QShortcut
from PyQt6.QtWidgets import (
    QApplication, QFrame, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem,
    QMainWindow, QPushButton, QSizePolicy, QStyle, QVBoxLayout, QWidget,
)

import lyrics_overlay
from lyrics_overlay import (
    APP, CONFIG_DIR, DemoWatcher, Lyrics, MprisWatcher, Track, VocalTagStore, _send,
    fetch_lyrics, norm, query_overlay,
)

TAG_COLORS = {
    "M": "#7ec8ff",  # Male - Blue
    "F": "#ff6b8b",  # Female - Pink
    "D": "#c79bff",  # Duet - Purple
    "-": "#6b7280",  # Unassigned - Gray
}


def fmt_time(ms: int) -> str:
    s = max(0, ms // 1000)
    return f"{s // 60:02d}:{s % 60:02d}"


class LyricLineWidget(QWidget):
    """Custom widget for rendering a single lyric line with badge and timing."""

    def __init__(self, idx: int, time_ms: int, text: str, tag: str = "-", parent=None):
        super().__init__(parent)
        self.idx = idx
        self.time_ms = time_ms
        self.text = text
        self.tag = tag.upper() if tag.upper() in ("M", "F", "D") else "-"
        self.is_playing = False
        self.is_selected = False

        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 6, 10, 6)
        layout.setSpacing(12)

        # Line number
        self.lbl_idx = QLabel(f"{idx + 1:02d}", self)
        self.lbl_idx.setFixedWidth(28)
        self.lbl_idx.setStyleSheet("color: #64748b; font-size: 12px; font-weight: bold;")
        layout.addWidget(self.lbl_idx)

        # Vocal tag badge
        self.lbl_tag = QLabel(self)
        self.lbl_tag.setFixedSize(32, 22)
        self.lbl_tag.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.lbl_tag)

        # Timestamp
        self.lbl_time = QLabel(fmt_time(time_ms), self)
        self.lbl_time.setFixedWidth(46)
        self.lbl_time.setStyleSheet("color: #64748b; font-size: 12px; font-family: monospace;")
        layout.addWidget(self.lbl_time)

        # Lyrics text
        self.lbl_text = QLabel(text, self)
        self.lbl_text.setWordWrap(True)
        self.lbl_text.setStyleSheet("color: #f1f5f9; font-size: 14px;")
        layout.addWidget(self.lbl_text, 1)

        # Playing indicator
        self.lbl_play = QLabel("", self)
        self.lbl_play.setFixedWidth(20)
        self.lbl_play.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl_play.setStyleSheet("color: #10b981; font-weight: bold; font-size: 13px;")
        layout.addWidget(self.lbl_play)

        self.update_style()

    def set_tag(self, tag: str):
        self.tag = tag.upper() if tag.upper() in ("M", "F", "D") else "-"
        self.update_style()

    def set_playing(self, playing: bool):
        if self.is_playing != playing:
            self.is_playing = playing
            self.lbl_play.setText("▶" if playing else "")
            self.update_style()

    def set_selected(self, selected: bool):
        if self.is_selected != selected:
            self.is_selected = selected
            self.update_style()

    def update_style(self):
        col = TAG_COLORS.get(self.tag, TAG_COLORS["-"])
        tag_char = self.tag if self.tag != "-" else "·"
        self.lbl_tag.setText(tag_char)
        self.lbl_tag.setStyleSheet(
            f"background: {col}22; color: {col}; border: 1px solid {col}66; "
            f"border-radius: 4px; font-weight: bold; font-size: 12px; font-family: monospace;"
        )

        bg = "#1e1e2d"
        border = "transparent"
        if self.is_selected:
            bg = "#282a3e"
            border = "#8b5cf6"
        elif self.is_playing:
            bg = "#23263a"
            border = "#10b98166"

        self.setStyleSheet(
            f"LyricLineWidget {{ background: {bg}; border: 1px solid {border}; border-radius: 6px; }}"
        )


class TaggerWindow(QMainWindow):
    def __init__(self, demo: bool = False):
        super().__init__()
        self.demo = demo
        self.cfg = lyrics_overlay.Config()
        lyrics_overlay.PREFER_LATIN = bool(self.cfg["prefer_latin"])
        lyrics_overlay.SYNCEDLYRICS_PROVIDERS[:] = list(self.cfg["syncedlyrics_providers"])
        self.tag_store = VocalTagStore()

        self.track: Track | None = None
        self.track_key: tuple | None = None
        self.lines: list = []  # list of Line objects with text
        self.tags: list[str] = []
        self.current_idx = 0
        self.playing_idx = -1
        self.pos_ms = 0
        self.status = "none"

        self.setWindowTitle("Lyrics Vocal Tagger")
        self.resize(760, 780)
        self.setMinimumSize(540, 500)
        self._init_ui()
        self._init_shortcuts()

        # Connect MPRIS watcher
        self.watcher = DemoWatcher() if demo else MprisWatcher(self.cfg)
        self.watcher.snapshot.connect(self.on_snapshot)
        self.watcher.start()

    def _init_ui(self):
        self.setStyleSheet("""
            QMainWindow { background: #13131c; }
            QLabel { color: #e2e8f0; font-family: 'Inter', sans-serif; }
            QFrame#card { background: #1b1b28; border: 1px solid #2d2d42; border-radius: 10px; }
            QListWidget { background: transparent; border: none; outline: none; }
            QListWidget::item { padding: 2px 0px; border: none; }
            QListWidget::item:selected { background: transparent; }
            QLineEdit { background: #181824; color: #a5b4fc; border: 1px solid #3730a3;
                        border-radius: 6px; padding: 6px 12px; font-family: monospace; font-size: 13px; }
            QLineEdit:focus { border: 1px solid #818cf8; }
            QPushButton { background: #4f46e5; color: #ffffff; border: none; border-radius: 6px;
                          padding: 8px 16px; font-weight: bold; font-size: 13px; }
            QPushButton:hover { background: #4338ca; }
            QPushButton:pressed { background: #3730a3; }
        """)

        central = QWidget(self)
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(18, 18, 18, 18)
        root.setSpacing(14)

        # ── Header Card ───────────────────────────────────────────────
        header = QFrame(self)
        header.setObjectName("card")
        h_layout = QVBoxLayout(header)
        h_layout.setContentsMargins(16, 14, 16, 14)
        h_layout.setSpacing(6)

        top_row = QHBoxLayout()
        self.lbl_title = QLabel("Waiting for player…", self)
        self.lbl_title.setStyleSheet("font-size: 18px; font-weight: bold; color: #ffffff;")
        top_row.addWidget(self.lbl_title, 1)

        self.lbl_status = QLabel("Idle", self)
        self.lbl_status.setStyleSheet(
            "background: #334155; color: #94a3b8; padding: 3px 10px; border-radius: 12px; font-size: 11px; font-weight: bold;"
        )
        top_row.addWidget(self.lbl_status)
        h_layout.addLayout(top_row)

        sub_row = QHBoxLayout()
        self.lbl_artist = QLabel("Play a track in Spotify, YouTube Music, or any MPRIS player", self)
        self.lbl_artist.setStyleSheet("color: #94a3b8; font-size: 13px;")
        sub_row.addWidget(self.lbl_artist, 1)

        self.lbl_pos = QLabel("00:00 / 00:00", self)
        self.lbl_pos.setStyleSheet("color: #64748b; font-size: 12px; font-family: monospace;")
        sub_row.addWidget(self.lbl_pos)
        h_layout.addLayout(sub_row)
        root.addWidget(header)

        # ── Controls & Legend Bar ─────────────────────────────────────
        legend = QHBoxLayout()
        legend.setSpacing(8)

        def make_chip(key, label, col):
            f = QFrame(self)
            f.setStyleSheet(f"background: {col}18; border: 1px solid {col}55; border-radius: 5px;")
            l = QHBoxLayout(f)
            l.setContentsMargins(8, 4, 8, 4)
            l.setSpacing(6)
            k = QLabel(f"[{key}]", f)
            k.setStyleSheet(f"color: {col}; font-weight: bold; font-family: monospace; font-size: 11px;")
            txt = QLabel(label, f)
            txt.setStyleSheet(f"color: #cbd5e1; font-size: 11px;")
            l.addWidget(k)
            l.addWidget(txt)
            return f

        legend.addWidget(make_chip("M", "Male", TAG_COLORS["M"]))
        legend.addWidget(make_chip("F", "Female", TAG_COLORS["F"]))
        legend.addWidget(make_chip("D", "Duet", TAG_COLORS["D"]))
        legend.addWidget(make_chip("Space", "Clear", TAG_COLORS["-"]))
        legend.addStretch(1)

        help_lbl = QLabel("↑/↓: Navigate  •  Click: Seek player  •  Ctrl+S: Save", self)
        help_lbl.setStyleSheet("color: #64748b; font-size: 11px;")
        legend.addWidget(help_lbl)
        root.addLayout(legend)

        # ── Tag String & Save Bar ─────────────────────────────────────
        tag_bar = QHBoxLayout()
        tag_bar.setSpacing(10)

        tag_lbl = QLabel("Tag String:", self)
        tag_lbl.setStyleSheet("font-weight: bold; color: #cbd5e1; font-size: 12px;")
        tag_bar.addWidget(tag_lbl)

        self.txt_tags = QLineEdit(self)
        self.txt_tags.setPlaceholderText("MMMMFFFFDD…")
        self.txt_tags.textEdited.connect(self._on_tag_string_edited)
        tag_bar.addWidget(self.txt_tags, 1)

        self.lbl_count = QLabel("0 / 0", self)
        self.lbl_count.setStyleSheet("color: #94a3b8; font-size: 12px; font-family: monospace;")
        tag_bar.addWidget(self.lbl_count)

        self.btn_all_male = QPushButton("All Male (#M)", self)
        self.btn_all_male.setStyleSheet("""
            QPushButton {
                background: #1e3a8a; color: #93c5fd; border: 1px solid #3b82f6;
                border-radius: 6px; padding: 7px 12px; font-weight: bold; font-size: 12px;
            }
            QPushButton:hover { background: #1d4ed8; color: #ffffff; }
            QPushButton:pressed { background: #1e40af; }
        """)
        self.btn_all_male.setToolTip("Mark and save entire song as Male vocals (#M)")
        self.btn_all_male.clicked.connect(lambda: self.set_whole_song("#M"))
        tag_bar.addWidget(self.btn_all_male)

        self.btn_all_female = QPushButton("All Female (#F)", self)
        self.btn_all_female.setStyleSheet("""
            QPushButton {
                background: #831843; color: #fbcfe8; border: 1px solid #ec4899;
                border-radius: 6px; padding: 7px 12px; font-weight: bold; font-size: 12px;
            }
            QPushButton:hover { background: #be185d; color: #ffffff; }
            QPushButton:pressed { background: #9d174d; }
        """)
        self.btn_all_female.setToolTip("Mark and save entire song as Female vocals (#F)")
        self.btn_all_female.clicked.connect(lambda: self.set_whole_song("#F"))
        tag_bar.addWidget(self.btn_all_female)

        self.btn_save = QPushButton("Save Tags", self)
        self.btn_save.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_DialogSaveButton))
        self.btn_save.clicked.connect(self.save_tags)
        tag_bar.addWidget(self.btn_save)
        root.addLayout(tag_bar)

        # ── Notification Toast Banner ─────────────────────────────────
        self.banner = QLabel("", self)
        self.banner.setStyleSheet(
            "background: #065f46; color: #6ee7b7; padding: 6px 12px; border-radius: 6px; "
            "font-size: 12px; font-weight: bold;"
        )
        self.banner.hide()
        root.addWidget(self.banner)

        # ── Lyric Lines List ──────────────────────────────────────────
        self.list_widget = QListWidget(self)
        self.list_widget.itemClicked.connect(self._on_item_clicked)
        root.addWidget(self.list_widget, 1)

    def _init_shortcuts(self):
        # M / F / D / Space tagging
        QShortcut(QKeySequence(Qt.Key.Key_M), self, lambda: self.tag_current("M"))
        QShortcut(QKeySequence(Qt.Key.Key_F), self, lambda: self.tag_current("F"))
        QShortcut(QKeySequence(Qt.Key.Key_D), self, lambda: self.tag_current("D"))
        QShortcut(QKeySequence(Qt.Key.Key_Space), self, lambda: self.tag_current("-"))
        QShortcut(QKeySequence(Qt.Key.Key_Minus), self, lambda: self.tag_current("-"))

        # Navigation
        QShortcut(QKeySequence(Qt.Key.Key_Up), self, self.move_prev)
        QShortcut(QKeySequence(Qt.Key.Key_Down), self, self.move_next)
        QShortcut(QKeySequence(Qt.Key.Key_Backspace), self, self.undo_prev)

        # Actions
        QShortcut(QKeySequence("Ctrl+S"), self, self.save_tags)
        QShortcut(QKeySequence("Ctrl+Q"), self, self.close)
        QShortcut(QKeySequence(Qt.Key.Key_Escape), self, self.close)

    # ── Snapshot Handling ─────────────────────────────────────────────
    def on_snapshot(self, s: dict):
        title = s.get("title", "")
        artist = s.get("artist", "")
        album = s.get("album", "")
        dur_s = s.get("length", 0) // 1_000_000
        self.pos_ms = s.get("pos", 0) // 1000
        self.status = s.get("status", "Stopped")

        # Update status badge
        self.lbl_status.setText(self.status)
        if self.status == "Playing":
            self.lbl_status.setStyleSheet("background: #065f46; color: #6ee7b7; padding: 3px 10px; border-radius: 12px; font-size: 11px; font-weight: bold;")
        elif self.status == "Paused":
            self.lbl_status.setStyleSheet("background: #78350f; color: #fde68a; padding: 3px 10px; border-radius: 12px; font-size: 11px; font-weight: bold;")
        else:
            self.lbl_status.setStyleSheet("background: #334155; color: #94a3b8; padding: 3px 10px; border-radius: 12px; font-size: 11px; font-weight: bold;")

        self.lbl_pos.setText(f"{fmt_time(self.pos_ms)} / {fmt_time(dur_s * 1000)}")

        if not title:
            return

        key = (title, artist, album)
        if key != self.track_key:
            self.track_key = key
            self.track = Track(title, artist, album, dur_s * 1000)
            self.lbl_title.setText(title)
            self.lbl_artist.setText(artist or "Unknown Artist")
            self._load_lyrics()

        # Update currently playing line highlight
        self._update_playing_line()

    def _load_lyrics(self):
        self.list_widget.clear()
        self.lines = []
        self.tags = []
        self.current_idx = 0
        self.playing_idx = -1

        if not self.track:
            return

        lyr = None
        # 1. Ask the running lyrics overlay for its exact current lyrics
        overlay_info = query_overlay("get-current")
        if overlay_info and overlay_info.get("lyrics"):
            ov_track = overlay_info.get("track") or {}
            if norm(ov_track.get("title", "")) == norm(self.track.title):
                try:
                    lyr = Lyrics.from_json(overlay_info["lyrics"])
                except Exception:
                    lyr = None

        # 2. Fall back to shared disk cache or providers if overlay not running
        if not lyr or not lyr.lines or lyr.msg:
            provs = ["demo"] if self.demo else self.cfg["providers"]
            lyr = fetch_lyrics(self.track, provs)

        if not lyr or not lyr.lines or lyr.msg:
            item = QListWidgetItem(self.list_widget)
            lbl = QLabel("No synced lyrics found for this track.", self)
            lbl.setStyleSheet("color: #94a3b8; font-style: italic; padding: 20px;")
            lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.list_widget.setItemWidget(item, lbl)
            item.setSizeHint(QSize(100, 60))
            self.txt_tags.setText("")
            self.lbl_count.setText("0 / 0")
            return

        # Extract singing lines with text
        self.lines = [l for l in lyr.lines if l.text.strip()]
        n = len(self.lines)

        # Check existing tags in vocal_tags.json
        existing = (self.tag_store.get(self.track) or "").strip()
        is_whole = False
        if existing:
            if existing.upper().startswith("#") and len(existing) >= 2 and existing[1].upper() in ("M", "F", "D"):
                singer = existing[1].upper()
                self.tags = [singer] * n
                is_whole = True
            else:
                self.tags = [c.upper() if c.upper() in ("M", "F", "D") else "-" for c in existing[:n]]
                if len(self.tags) < n:
                    self.tags.extend(["-"] * (n - len(self.tags)))
        else:
            self.tags = ["-"] * n

        # Build list items
        for i, l in enumerate(self.lines):
            item = QListWidgetItem(self.list_widget)
            widget = LyricLineWidget(i, l.start, l.text, self.tags[i])
            item.setSizeHint(widget.sizeHint())
            self.list_widget.addItem(item)
            self.list_widget.setItemWidget(item, widget)

        if is_whole:
            singer = existing[1].upper()
            label = "Male" if singer == "M" else "Female" if singer == "F" else "Duet"
            self.txt_tags.blockSignals(True)
            self.txt_tags.setText(f"#{singer}")
            self.txt_tags.blockSignals(False)
            self.lbl_count.setText(f"{n} / {n} (All {label})")
        else:
            self._update_tag_string_ui()
        self._select_line(0)

    def _update_playing_line(self):
        if not self.lines:
            return
        # Find which line is currently playing
        active = -1
        for i, l in enumerate(self.lines):
            if l.start <= self.pos_ms:
                active = i
            else:
                break

        if active != self.playing_idx:
            # Unmark previous
            if 0 <= self.playing_idx < self.list_widget.count():
                w = self.list_widget.itemWidget(self.list_widget.item(self.playing_idx))
                if isinstance(w, LyricLineWidget):
                    w.set_playing(False)

            # Mark new
            self.playing_idx = active
            if 0 <= active < self.list_widget.count():
                w = self.list_widget.itemWidget(self.list_widget.item(active))
                if isinstance(w, LyricLineWidget):
                    w.set_playing(True)

    def _select_line(self, idx: int, scroll: bool = True):
        if not self.lines:
            return
        idx = max(0, min(len(self.lines) - 1, idx))

        # Deselect old
        if 0 <= self.current_idx < self.list_widget.count():
            w = self.list_widget.itemWidget(self.list_widget.item(self.current_idx))
            if isinstance(w, LyricLineWidget):
                w.set_selected(False)

        # Select new
        self.current_idx = idx
        item = self.list_widget.item(idx)
        if item:
            w = self.list_widget.itemWidget(item)
            if isinstance(w, LyricLineWidget):
                w.set_selected(True)
            if scroll:
                self.list_widget.scrollToItem(item)

    def _on_item_clicked(self, item: QListWidgetItem):
        idx = self.list_widget.row(item)
        self._select_line(idx, scroll=False)

        # Seek player directly to this line
        if 0 <= idx < len(self.lines):
            target_ms = self.lines[idx].start
            self.watcher.send("seek_to", int(target_ms * 1000))
            self._notify(f"Seeked to {fmt_time(target_ms)} (Line {idx + 1})")

    # ── Tagging Actions ───────────────────────────────────────────────
    def tag_current(self, tag: str):
        if not self.lines or not (0 <= self.current_idx < len(self.tags)):
            return

        tag = tag.upper() if tag.upper() in ("M", "F", "D") else "-"
        self.tags[self.current_idx] = tag

        # Update widget
        item = self.list_widget.item(self.current_idx)
        if item:
            w = self.list_widget.itemWidget(item)
            if isinstance(w, LyricLineWidget):
                w.set_tag(tag)

        self._update_tag_string_ui()
        self.move_next()

    def undo_prev(self):
        if self.current_idx > 0:
            self.move_prev()
            self.tags[self.current_idx] = "-"
            item = self.list_widget.item(self.current_idx)
            if item:
                w = self.list_widget.itemWidget(item)
                if isinstance(w, LyricLineWidget):
                    w.set_tag("-")
            self._update_tag_string_ui()

    def move_prev(self):
        if self.current_idx > 0:
            self._select_line(self.current_idx - 1)

    def move_next(self):
        if self.current_idx < len(self.lines) - 1:
            self._select_line(self.current_idx + 1)

    def _update_tag_string_ui(self):
        curr_text = self.txt_tags.text().strip().upper()
        if curr_text.startswith("#") and len(curr_text) >= 2 and curr_text[1] in ("M", "F", "D"):
            singer = curr_text[1]
            label = "Male" if singer == "M" else "Female" if singer == "F" else "Duet"
            if self.tags and all(t == singer for t in self.tags):
                self.lbl_count.setText(f"{len(self.lines)} / {len(self.lines)} (All {label})")
                return

        tag_str = "".join(self.tags)
        self.txt_tags.blockSignals(True)
        self.txt_tags.setText(tag_str)
        self.txt_tags.blockSignals(False)

        tagged = sum(1 for c in self.tags if c in ("M", "F", "D"))
        total = len(self.lines)
        self.lbl_count.setText(f"{tagged} / {total} tagged")

    def _on_tag_string_edited(self, new_text: str):
        if not self.lines:
            return
        n = len(self.lines)
        clean = new_text.strip().upper()
        if clean.startswith("#") and len(clean) >= 2 and clean[1] in ("M", "F", "D"):
            singer = clean[1]
            label = "Male" if singer == "M" else "Female" if singer == "F" else "Duet"
            self.tags = [singer] * n
            for i in range(min(n, self.list_widget.count())):
                item = self.list_widget.item(i)
                if item:
                    w = self.list_widget.itemWidget(item)
                    if isinstance(w, LyricLineWidget):
                        w.set_tag(singer)
            self.lbl_count.setText(f"{n} / {n} (All {label})")
            return

        parsed = [c.upper() if c.upper() in ("M", "F", "D") else "-" for c in clean[:n]]
        if len(parsed) < n:
            parsed.extend(["-"] * (n - len(parsed)))

        self.tags = parsed
        for i in range(min(n, self.list_widget.count())):
            item = self.list_widget.item(i)
            if item:
                w = self.list_widget.itemWidget(item)
                if isinstance(w, LyricLineWidget):
                    w.set_tag(self.tags[i])

        tagged = sum(1 for c in self.tags if c in ("M", "F", "D"))
        self.lbl_count.setText(f"{tagged} / {n} tagged")

    def set_whole_song(self, tag_code: str):
        if not self.track or not self.lines:
            self._notify("No track or lyrics loaded to tag!", is_error=True)
            return

        tag_code = tag_code.strip().upper()
        singer = tag_code[1] if (len(tag_code) >= 2 and tag_code[1] in ("M", "F", "D")) else "M"
        label = "Male" if singer == "M" else "Female" if singer == "F" else "Duet"

        self.tags = [singer] * len(self.lines)
        for i in range(min(len(self.lines), self.list_widget.count())):
            item = self.list_widget.item(i)
            if item:
                w = self.list_widget.itemWidget(item)
                if isinstance(w, LyricLineWidget):
                    w.set_tag(singer)

        self.txt_tags.blockSignals(True)
        self.txt_tags.setText(f"#{singer}")
        self.txt_tags.blockSignals(False)
        self.lbl_count.setText(f"{len(self.lines)} / {len(self.lines)} (All {label})")

        self.tag_store.set(self.track, f"#{singer}", len(self.lines))

        overlay_updated = _send("reload-tags")
        msg = f"Saved entire song as {label} (#{singer})!"
        if overlay_updated:
            msg += " Lyrics overlay updated."
        self._notify(msg)

    def save_tags(self):
        if not self.track or not self.lines:
            self._notify("No track or lyrics to save!", is_error=True)
            return

        curr_input = self.txt_tags.text().strip().upper()
        if curr_input.startswith("#") and len(curr_input) >= 2 and curr_input[1] in ("M", "F", "D"):
            tag_str = curr_input[:2]
        else:
            tag_str = "".join(self.tags)

        self.tag_store.set(self.track, tag_str, len(self.lines))

        # Ping lyrics overlay to reload tags immediately
        overlay_updated = _send("reload-tags")
        msg = f"Saved tags for '{self.track.title}'!"
        if overlay_updated:
            msg += " Lyrics overlay updated."
        else:
            msg += " (Overlay not currently running)"
        self._notify(msg)

    def _notify(self, text: str, is_error: bool = False):
        bg = "#991b1b" if is_error else "#065f46"
        col = "#fca5a5" if is_error else "#6ee7b7"
        self.banner.setStyleSheet(
            f"background: {bg}; color: {col}; padding: 6px 12px; border-radius: 6px; "
            f"font-size: 12px; font-weight: bold;"
        )
        self.banner.setText(text)
        self.banner.show()
        QTimer.singleShot(3500, self.banner.hide)

    def closeEvent(self, ev):
        self.watcher.stop()
        super().closeEvent(ev)


def main():
    parser = argparse.ArgumentParser(description="Interactive vocal tagging tool for Lyrics Overlay")
    parser.add_argument("--demo", action="store_true", help="use built-in demo track without player")
    args = parser.parse_args()

    app = QApplication(sys.argv)
    app.setApplicationName("Lyrics Vocal Tagger")
    win = TaggerWindow(demo=args.demo)
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
