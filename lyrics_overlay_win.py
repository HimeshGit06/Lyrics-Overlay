#!/usr/bin/env python3
"""
Lyrics Overlay — synced lyrics overlay for Windows (SMTC / System Media Controls)
and Linux (MPRIS2) supporting Spotify, YouTube Music, Chrome, Firefox, VLC, and more.

  * Players   : track any media playing via Windows System Media Transport Controls
  * Lyrics    : LRCLIB, Musixmatch/Megalobiz (syncedlyrics), YouTube Music, KuGou, NetEase, QQ
  * Rendering : custom 60 fps painter, spring-physics scrolling, karaoke word fill,
                singer colours (M:/F:/D: tags), 3 layouts (minimal / focus / scroll)
  * Control   : system tray icon, right-click menu, or command-line IPC (`python lyrics_overlay.py ctl ...`)

Dependencies (Windows): pip install PyQt6 winsdk syncedlyrics ytmusicapi
Run:                    python lyrics_overlay.py            (add --demo to try without a player)
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import bisect
import difflib
import hashlib
import html
import importlib.util
import json
import logging
import math
import os
import queue
import re
import socket
import subprocess
import sys
import threading
import time
import unicodedata
import zlib
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path
from typing import Optional
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urlencode, urlparse
from urllib.request import Request, urlopen

from PyQt6.QtCore import (
    QObject, QPointF, QRect, QRectF, QRunnable, Qt, QThread, QThreadPool, QTimer, pyqtSignal,
)
from PyQt6.QtGui import (
    QColor, QCursor, QFont, QGuiApplication, QIcon, QImage,
    QLinearGradient, QPainter, QPainterPath, QPixmap, QRadialGradient, QTextLayout, QTextOption,
)
from PyQt6.QtNetwork import QLocalServer, QLocalSocket
from PyQt6.QtWidgets import (
    QApplication, QGraphicsBlurEffect, QGraphicsPixmapItem, QGraphicsScene, QMenu,
    QSystemTrayIcon, QWidget,
)

APP = "lyrics-overlay"
VERSION = "2.0"
UA = f"{APP}/{VERSION} (https://github.com/ - personal lyrics overlay)"
BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

if sys.platform == "win32":
    CONFIG_DIR = Path(os.environ.get("APPDATA", Path.home())) / APP
    CACHE_DIR = Path(os.environ.get("LOCALAPPDATA", Path.home())) / APP
    DATA_DIR = Path(os.environ.get("LOCALAPPDATA", Path.home())) / APP
else:
    CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / APP
    CACHE_DIR = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / APP
    DATA_DIR = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / APP

VERBOSE = False


def log(*a):
    if VERBOSE:
        print(f"[{APP}]", *a, file=sys.stderr, flush=True)


# ── Devanagari Script Detector ─────────────────────────────────────────────────
def contains_devanagari(text: str) -> bool:
    """Returns True if the string contains any Devanagari characters (\u0900-\u097F)."""
    return bool(re.search(r"[\u0900-\u097F]", text))


# ── optional lyric sources that need pip packages ──────────────────────────────
DEPS_DIR = DATA_DIR / "deps" / f"py{sys.version_info.major}.{sys.version_info.minor}"
EXTRA_PACKAGES = {"syncedlyrics": "syncedlyrics", "ytmusicapi": "ytmusicapi"}


def activate_deps():
    if DEPS_DIR.is_dir() and str(DEPS_DIR) not in sys.path:
        sys.path.append(str(DEPS_DIR))
    importlib.invalidate_caches()


def missing_extras() -> list:
    return [pip for mod, pip in EXTRA_PACKAGES.items() if importlib.util.find_spec(mod) is None]


def install_extras(echo=print) -> bool:
    try:
        ok = subprocess.run([sys.executable, "-m", "pip", "--version"], capture_output=True, timeout=30).returncode == 0
    except (OSError, subprocess.SubprocessError):
        ok = False
    if not ok:
        echo("pip is not available. Install it first: python -m ensurepip --default-pip")
        return False
    DEPS_DIR.mkdir(parents=True, exist_ok=True)
    cmd = [sys.executable, "-m", "pip", "install", "--upgrade", "--quiet", "--no-warn-script-location",
           "--target", str(DEPS_DIR), *EXTRA_PACKAGES.values()]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
    except (OSError, subprocess.SubprocessError) as exc:
        echo(f"pip failed to start: {exc}")
        return False
    if proc.returncode != 0:
        echo("pip failed:\n" + (proc.stderr or proc.stdout)[-600:])
        return False
    activate_deps()
    return True


def _extras_due() -> bool:
    try:
        return time.time() - float((CACHE_DIR / "extras_attempt").read_text()) > 7 * 86400
    except (OSError, ValueError):
        return True


def _extras_mark():
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        (CACHE_DIR / "extras_attempt").write_text(str(time.time()))
    except OSError:
        pass


activate_deps()
logging.getLogger("syncedlyrics").setLevel(logging.CRITICAL)


# ══════════════════════════════════════════════════════════════════ config ══

DEFAULT_PROVIDERS = ["lrclib", "syncedlyrics", "ytmusic", "kugou", "netease", "qq"]

DEFAULTS: dict = {
    "mode": "focus",
    "width": 820,
    "font_family": "Segoe UI",
    "font_size": 30,
    "font_weight": 700,
    "align": "center",
    "inactive_scale": 0.86,
    "text_color": "#ffffff",
    "colors": {"M": "#7ec8ff", "F": "#ff6b8b", "D": "#c79bff"},
    "estimate_words": True,
    "lead_ms": 140,
    "global_offset_ms": 0,
    "providers": list(DEFAULT_PROVIDERS),
    "auto_install_extras": True,
    "prefer_latin": True,
    "syncedlyrics_providers": ["musixmatch", "megalobiz", "lyricsify"],
    "preferred_player": "",
    "ignore_players": [],
    "hide_when_idle": True,
    "spring": 14.0,
    "bg_style": "blur",
    "bg_blur": 44,
    "bg_alpha": 1.0,
    "pos": None,
}


class Config:
    def __init__(self):
        self.path = CONFIG_DIR / "config.json"
        self.data = json.loads(json.dumps(DEFAULTS))
        try:
            user = json.loads(self.path.read_text())
            for k, v in user.items():
                if isinstance(v, dict) and isinstance(self.data.get(k), dict):
                    self.data[k].update(v)
                else:
                    self.data[k] = v
        except FileNotFoundError:
            pass
        except Exception as exc:
            print(f"[{APP}] bad config ({exc}); using defaults", file=sys.stderr)

    def __getitem__(self, k):
        return self.data[k]

    def set(self, k, v):
        self.data[k] = v
        self.save()

    def save(self):
        try:
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self.data, indent=2, ensure_ascii=False))
        except OSError as exc:
            log("cannot save config:", exc)


class OffsetStore:
    def __init__(self):
        self.path = CONFIG_DIR / "offsets.json"
        try:
            self.d = json.loads(self.path.read_text())
        except Exception:
            self.d = {}

    def get(self, key: str) -> int:
        return int(self.d.get(key, 0))

    def set(self, key: str, ms: int):
        if ms:
            self.d[key] = ms
        else:
            self.d.pop(key, None)
        try:
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self.d))
        except OSError:
            pass


# ═════════════════════════════════════════════════════════════ lyric models ══

@dataclass
class Word:
    start: int
    end: int
    text: str
    c0: int = 0
    c1: int = 0


@dataclass
class Line:
    start: int
    end: int = 0
    text: str = ""
    singer: Optional[str] = None
    trans: str = ""
    words: list = field(default_factory=list)


@dataclass
class Lyrics:
    lines: list
    source: str = ""
    instrumental: bool = False
    msg: bool = False

    @property
    def has_words(self) -> bool:
        return any(l.words for l in self.lines)

    def to_json(self) -> dict:
        return {
            "source": self.source, "instrumental": self.instrumental,
            "lines": [[l.start, l.end, l.text, l.singer, l.trans,
                       [[w.start, w.end, w.text] for w in l.words]] for l in self.lines],
        }

    @staticmethod
    def from_json(d: dict) -> "Lyrics":
        lines = []
        for s, e, t, sg, tr, ws in d["lines"]:
            ln = Line(s, e, t, sg, tr, [Word(a, b, c) for a, b, c in ws])
            index_words(ln)
            lines.append(ln)
        return Lyrics(lines, d.get("source", ""), d.get("instrumental", False))


def index_words(line: Line):
    pos = 0
    for w in line.words:
        w.c0 = pos
        pos += len(w.text)
        w.c1 = pos


@dataclass
class Track:
    title: str = ""
    artist: str = ""
    album: str = ""
    duration_ms: int = 0
    url: str = ""
    variants: list = field(default_factory=list, compare=False, repr=False)

    def key(self) -> str:
        raw = f"{skeleton(self.title)}|{skeleton(clean_artist(self.artist))}"
        return hashlib.sha1(raw.encode()).hexdigest()[:20]


# ═══════════════════════════════════════════════════════════════ LRC parsing ══

TS = re.compile(r"\[(\d+):(\d{1,2})(?:[.:](\d{1,3}))?\]")
WORD_TS = re.compile(r"<(\d+):(\d{1,2})(?:[.:](\d{1,3}))?>")
META_OFFSET = re.compile(r"^\[offset:\s*([+-]?\d+)\s*\]", re.I)
VOCAL_TAG = re.compile(r"^\s*([MFD])\s*:\s*", re.I)


def _ts(mm, ss, frac) -> int:
    ms = 0
    if frac:
        ms = int(frac) * (100 if len(frac) == 1 else 10 if len(frac) == 2 else 1)
    return int(mm) * 60_000 + int(ss) * 1000 + ms


# ── translation avoidance: keep the ORIGINAL lyrics of bilingual LRC files ─────
# Bilingual files come in several shapes; all of them are handled here, for every source:
#   1. translation on a second line with the same (or nearly the same) timestamp
#   2. translation lines on their own timestamps, alternating with the original lines
#   3. "original / translation" or "original (translation)" on a single line
_EN_STOP = frozenset("the a an and or but of in on for with from is are was were be been i you she it we they "
                     "me my your his her our their this that these those not will would can could if what "
                     "when where who how all just".split())


def english_score(text: str) -> float:
    """Share of words that are common English function words (0 for non-Latin text)."""
    toks = re.findall(r"[A-Za-z']+", text)
    if not toks or not _is_latin(text):
        return 0.0
    return sum(t.lower() in _EN_STOP for t in toks) / len(toks)


def _is_english(text: str) -> bool:
    return bool(text) and english_score(text) >= 0.25


def _strip_same_time(lines: list) -> list:
    """Lines that start within 250 ms of each other are original + translation: keep one."""
    groups: list = []
    for l in lines:
        g = groups[-1] if groups else None
        if (g and l.text and g[0].text and l.start - g[0].start <= 250
                and not (l.singer and g[0].singer and l.singer != g[0].singer)):   # a duet is not a translation
            g.append(l)
        else:
            groups.append([l])
    pairs = [g for g in groups if len(g) > 1]
    if not pairs:
        return lines
    # Usually the original comes first. If the 2nd line is clearly *less* English than the 1st
    # across the whole file, the translation was listed first - keep the last one instead.
    d = sum(english_score(g[1].text) - english_score(g[0].text) for g in pairs) / len(pairs)
    keep = -1 if d <= -0.12 else 0
    return [g[keep] for g in groups]


def _strip_alternating(lines: list) -> list:
    """Original, translation, original, translation ... on different timestamps.

    One of the two interleaved sets (odd or even lines) is mostly English while the other is not:
    drop that whole set. Judging by set, not by line, also catches one-word translations."""
    text = [l for l in lines if l.text]
    if len(text) < 8:
        return lines
    share = []
    for parity in (0, 1):
        part = text[parity::2]
        share.append(sum(_is_english(l.text) for l in part) / len(part))
    p = 0 if share[0] >= share[1] else 1
    if share[p] >= 0.6 and share[1 - p] <= 0.2:
        drop = {id(l) for l in text[p::2]}
        return [l for l in lines if id(l) not in drop]
    return lines


_INLINE_TRANSLATION = (re.compile(r"^(?P<o>.+?)\s+[/|]\s+(?P<t>.+)$"),
                       re.compile(r"^(?P<o>.+?)\s*[\(\[（](?P<t>[^()\[\]（）]{4,})[\)\]）]\s*$"))


def _strip_inline(lines: list) -> list:
    """'original / translation' and 'original (translation)' -> 'original'.

    A file only counts as bilingual when at least a third of its lines have the pattern *and*
    the second part reads as a translation (English, or Latin script after a non-Latin original);
    then every line of that pattern is cut. Ordinary ad-libs such as '(oh oh)' are left alone."""
    text_lines = [l for l in lines if l.text]
    parsed = []                                     # (line, pattern index, original, looks like translation)
    for l in text_lines:
        for k, rx in enumerate(_INLINE_TRANSLATION):
            m = rx.match(l.text)
            if m:
                o, t = m.group("o").strip(), m.group("t").strip()
                looks = bool(o) and (english_score(t) - english_score(o) >= 0.2
                                     or (not _is_latin(o) and _is_latin(t)))
                parsed.append((l, k, o, looks))
                break
    for k in range(len(_INLINE_TRANSLATION)):
        mine = [p for p in parsed if p[1] == k]
        if sum(p[3] for p in mine) >= max(3, 0.35 * len(text_lines)):
            for l, _, o, _ in mine:                 # whole pattern, so one-word translations go too
                if o:
                    l.text, l.words = o, []
            break
    return lines


def strip_translations(lines: list) -> list:
    """lines: sorted by start time. Returns them without translation lines."""
    if len(lines) < 4:
        return lines
    return _strip_inline(_strip_alternating(_strip_same_time(lines)))


def is_translated_lrc(text: str) -> bool:
    """True if this LRC text is bilingual (contains an embedded translation)."""
    if not text:
        return False
    raw = parse_lrc(text, 0, strip_translation=False)
    before = [l.text for l in raw]
    out = strip_translations(list(raw))
    return len(out) != len(raw) or [l.text for l in raw] != before


def _parse_enhanced(body: str) -> tuple[list, str]:
    parts = WORD_TS.split(body)
    pre = parts[0]
    words: list[Word] = []
    for k in range((len(parts) - 1) // 4):
        mm, ss, fr, txt = parts[1 + 4 * k: 5 + 4 * k]
        start = _ts(mm, ss, fr)
        if txt == "" and words:
            words[-1].end = start
        else:
            if words and not words[-1].end:
                words[-1].end = start
            words.append(Word(start, 0, txt))
    if pre.strip() and words:
        words[0].text = pre + words[0].text
    if words:
        words[0].text = words[0].text.lstrip()
        words[-1].text = words[-1].text.rstrip()
    words = [w for w in words if w.text]
    return words, "".join(w.text for w in words)


def parse_lrc(text: str, duration_ms: int = 0, strip_translation: bool = True) -> list:
    lines: list[Line] = []
    offset = 0
    for raw in text.splitlines():
        raw = raw.strip()
        if not raw:
            continue
        mo = META_OFFSET.match(raw)
        if mo:
            offset = int(mo.group(1))
            continue
        stamps, pos = [], 0
        while True:
            m = TS.match(raw, pos)
            if not m:
                break
            stamps.append(_ts(*m.groups()))
            pos = m.end()
        if not stamps:
            continue
        body = raw[pos:]
        singer = None
        tag = VOCAL_TAG.match(body)
        if tag:
            singer = tag.group(1).upper()
            body = body[tag.end():]
        words: list[Word] = []
        if WORD_TS.search(body):
            words, body = _parse_enhanced(body)
        body = body.strip()
        for st in stamps:
            ln = Line(st, 0, body, singer, "", [Word(w.start, w.end, w.text) for w in words]
                      if len(stamps) == 1 else [])
            lines.append(ln)
    return finalize(lines, duration_ms, offset, strip_translation)


def finalize(lines: list, duration_ms: int = 0, offset: int = 0, strip_translation: bool = False) -> list:
    lines.sort(key=lambda l: l.start)
    out: list[Line] = []
    for l in lines:
        l.start -= offset
        for w in l.words:
            w.start -= offset
            w.end = w.end - offset if w.end else 0
        if not l.text and out and not out[-1].text:
            continue
        out.append(l)
    if strip_translation:
        out = strip_translations(out)
    for i, l in enumerate(out):
        nxt = out[i + 1].start if i + 1 < len(out) else (duration_ms or l.start + 6000)
        l.end = nxt if nxt > l.start else l.start + 3000
        for j, w in enumerate(l.words):
            if not w.end or w.end < w.start:
                w.end = l.words[j + 1].start if j + 1 < len(l.words) else min(l.end, w.start + 1500)
        index_words(l)
    return out


def parse_yrc(text: str) -> list:
    line_re = re.compile(r"^\[(\d+),(\d+)\](.*)$")
    word_re = re.compile(r"\((\d+),(\d+),-?\d+\)([^(]*)")
    lines: list[Line] = []
    for raw in text.splitlines():
        m = line_re.match(raw.strip())
        if not m:
            continue
        st, dur, body = int(m.group(1)), int(m.group(2)), m.group(3)
        words = [Word(int(a), int(a) + int(b), t) for a, b, t in word_re.findall(body)]
        if not words:
            continue
        words[0].text = words[0].text.lstrip()
        words[-1].text = words[-1].text.rstrip()
        words = [w for w in words if w.text]
        if not words:
            continue
        ln = Line(st, st + dur, "".join(w.text for w in words), None, "", words)
        index_words(ln)
        lines.append(ln)
    lines.sort(key=lambda l: l.start)
    return lines


def attach_translation(lines: list, trans: list):
    if not trans:
        return
    starts = [t.start for t in trans]
    for l in lines:
        i = bisect.bisect_left(starts, l.start - 400)
        if i < len(trans) and abs(trans[i].start - l.start) <= 800 and trans[i].text:
            l.trans = trans[i].text


# ═══════════════════════════════════════════════════════════ text matching ══

_BRACKETS = re.compile(r"[\(\[（【].*?[\)\]）】]")
_FEAT = re.compile(r"\b(feat|ft|featuring)\.?\s.*$", re.I)
_SUFFIX = re.compile(r"\s+-\s+(remaster(ed)?|live|single|radio edit|mono|stereo|from\b|"
                     r"\d{4}\b|.*\b(version|mix|edit|remaster(ed)?)\b).*$", re.I)


def clean_title(s: str) -> str:
    s = _BRACKETS.sub(" ", s)
    s = _FEAT.sub("", s)
    s = _SUFFIX.sub("", s)
    return re.sub(r"\s+", " ", s).strip()


def clean_artist(s: str) -> str:
    s = re.sub(r"\s*-\s*Topic$", "", s, flags=re.I)
    s = re.sub(r"VEVO$", "", s, flags=re.I)
    return re.split(r",|;|&| feat\.? | ft\.? | x ", s, maxsplit=1)[0].strip()


def fold(s: str) -> str:
    s = unicodedata.normalize("NFKD", s)
    return "".join(c for c in s if not unicodedata.combining(c)).lower()


def norm(s: str) -> str:
    return re.sub(r"[\W_]+", " ", fold(clean_title(s)).replace("&", " and ")).strip()


def skeleton(s: str) -> str:
    s = norm(s).replace(" ", "")
    for a, b in (("ee", "i"), ("oo", "u"), ("aa", "a"), ("ph", "f"), ("ck", "k"), ("q", "k"), ("w", "v"), ("z", "j")):
        s = s.replace(a, b)
    return re.sub(r"(.)\1+", r"\1", s)


def _is_latin(s: str) -> bool:
    letters = [c for c in s if c.isalpha()]
    return not letters or sum(1 for c in letters if ord(c) < 0x250) / len(letters) >= 0.5


def _text_sim(a: str, b: str) -> Optional[float]:
    na, nb = norm(a), norm(b)
    if not na or not nb or _is_latin(na) != _is_latin(nb):
        return None
    if na == nb:
        return 1.0
    r = max(difflib.SequenceMatcher(None, na, nb).ratio(),
            difflib.SequenceMatcher(None, skeleton(na), skeleton(nb)).ratio())
    if set(na.split()) == set(nb.split()):
        r = max(r, 0.95)
    short, long_ = sorted((na, nb), key=len)
    if f" {short} " in f" {long_} " and (" " in short or len(short) >= 8):
        r = max(r, 0.85)
    return r


def sim(a: str, b: str) -> float:
    v = _text_sim(a, b)
    return 0.0 if v is None else v


_ARTIST_SPLIT = re.compile(r"\s*(?:,|;|&|/|\+|\bfeat\.?(?=\s)|\bft\.?(?=\s)|\bfeaturing\b|\bx\b|\band\b)\s*", re.I)


def artist_sim(artist: str, cands: list) -> Optional[float]:
    mine = [p for p in _ARTIST_SPLIT.split(artist) if p.strip()] + [artist]
    theirs = [p for c in cands for p in _ARTIST_SPLIT.split(c) if p.strip()] + [c for c in cands if c]
    best = None
    for x in mine:
        for y in theirs:
            v = _text_sim(x, y)
            if v is not None and (best is None or v > best):
                best = v
    return best


def duration_score(diff_s: float) -> float:
    for limit, val in ((1.5, 1.0), (3.0, 0.92), (5.0, 0.7), (8.0, 0.45), (12.0, 0.2), (20.0, 0.05)):
        if diff_s <= limit:
            return val
    return 0.0


ACCEPT_MATCH = 0.72


def match_score(track: Track, title: str, artists: list, dur_ms: int) -> float:
    ts = _text_sim(track.title, title)
    if ts is not None and ts < 0.78:
        return 0.0
    parts = [(0.50, 0.5 if ts is None else ts)]
    sa = artist_sim(track.artist, artists) if track.artist and artists else None
    has_dur = bool(track.duration_ms and dur_ms)
    if sa is not None:
        if sa < 0.45 and (not has_dur or (ts is not None and ts < 0.95)):
            return 0.0
        parts.append((0.20, sa))
    if has_dur:
        d = abs(track.duration_ms - dur_ms) / 1000
        if d > 20:
            return 0.0
        parts.append((0.30, duration_score(d)))
    if len(parts) == 1 and (ts is None or ts < 0.9):
        return 0.0
    return sum(w * v for w, v in parts) / sum(w for w, _ in parts)


def ranked(track: Track, cands: list, min_score: float = ACCEPT_MATCH) -> list:
    scored = [(match_score(track, t, a, d), i, p) for i, (t, a, d, p) in enumerate(cands)]
    return [p for sc, _, p in sorted(scored, key=lambda x: (-x[0], x[1])) if sc >= min_score]


# ═════════════════════════════════════════════════════════════ providers ══

RETRY_STATUS = {429, 500, 502, 503, 504}


def http_text(url: str, params: Optional[dict] = None, headers: Optional[dict] = None,
              timeout: float = 6.0) -> str:
    if params:
        url += ("&" if "?" in url else "?") + urlencode(params)
    for attempt in (0, 1):
        try:
            req = Request(url, headers={"User-Agent": UA, **(headers or {})})
            with urlopen(req, timeout=timeout) as r:
                return r.read().decode("utf-8", "replace")
        except HTTPError as exc:
            if exc.code not in RETRY_STATUS or attempt:
                raise
        except (URLError, TimeoutError, ConnectionError):
            if attempt:
                raise
        time.sleep(0.5)
    raise RuntimeError("unreachable")


def http_json(url: str, params: Optional[dict] = None, headers: Optional[dict] = None,
              timeout: float = 6.0):
    return json.loads(http_text(url, params, {"Accept": "application/json", **(headers or {})}, timeout))


def _json_loose(text: str):
    text = text.strip()
    m = re.match(r"^[\w$.]+\((.*)\)\s*;?$", text, re.S)
    return json.loads(m.group(1) if m else text)


PREFER_LATIN = True


def latin_ratio(lines: list) -> float:
    letters = [ch for l in lines for ch in l.text if ch.isalpha()]
    return sum(1 for ch in letters if ord(ch) < 0x250) / len(letters) if letters else 1.0


_NOISE = re.compile(
    r"\b(official(\s+(music|lyric|lyrical))?(\s+(video|audio|song))?|full\s+(video\s+)?song|video\s+song|"
    r"lyric(al)?(\s+video)?|audio(\s+song)?|with\s+lyrics|hd|hq|4k)\b", re.I)


def query_variants(track: Track) -> list:
    if track.variants:
        return track.variants
    title = re.split(r"\s*[|｜]\s*", track.title)[0]
    title = _NOISE.sub(" ", title)
    artist = re.sub(r"\s*-\s*Topic$|VEVO$|\s+Official$", "", track.artist, flags=re.I).strip()
    if " - " in title:
        left, right = (x.strip() for x in title.split(" - ", 1))
        if not artist or sim(left, artist) > 0.6:
            artist, title = left, right
    title = clean_title(title) or track.title.strip()
    alt = clean_title(re.split(r"\s+[/–—]\s+", title)[0])
    primary = clean_artist(artist)
    out: list = []
    for t, a in ((title, primary), (title, ""), (title, artist), (alt, primary), (alt, "")):
        if t and (t, a) not in out:
            out.append((t, a))
    return out[:4] or [(track.title, track.artist)]


def canonical_track(track: Track) -> Track:
    v = query_variants(track)
    return Track(v[0][0], v[0][1], track.album, track.duration_ms, track.url, v)


def search_queries(track: Track) -> list:
    qs: list = []
    for t, a in query_variants(track):
        q = f"{t} {a}".strip()
        if q not in qs:
            qs.append(q)
    return qs[:3]


def _pick(track: Track, cands: list, min_score: float = ACCEPT_MATCH):
    best = ranked(track, cands, min_score)
    return best[0] if best else None


def provider_lrclib(track: Track) -> Optional[Lyrics]:
    base = "https://lrclib.net/api"
    variants = query_variants(track)
    dur = round(track.duration_ms / 1000)

    def get_exact(title, artist, album):
        params = {"track_name": title, "artist_name": artist, "duration": dur}
        if album:
            params["album_name"] = album
        try:
            return [http_json(f"{base}/get", params)]
        except HTTPError as exc:
            if exc.code == 404:
                return []
            raise

    def search(**params):
        return http_json(f"{base}/search", params) or []

    def gather(jobs: list) -> tuple:
        recs, errors, last = {}, 0, None
        with ThreadPoolExecutor(max_workers=max(1, len(jobs))) as ex:
            for fut in [ex.submit(j) for j in jobs]:
                try:
                    for r in fut.result():
                        recs.setdefault(r.get("id") or id(r), r)
                except Exception as exc:
                    errors, last = errors + 1, exc
        if jobs and errors == len(jobs):
            raise last
        return list(recs.values()), errors

    def best_lyrics(recs: list) -> Optional[Lyrics]:
        withl = [(r.get("trackName", ""), [r.get("artistName", "")], int((r.get("duration") or 0) * 1000), r)
                 for r in recs if r.get("syncedLyrics")]
        ranked_recs = ranked(track, withl)

        # 1. prefer a record that has no embedded translation, 2. else the best one (translation stripped)
        for want_clean in (True, False):
            for rec in ranked_recs:
                if want_clean and is_translated_lrc(rec["syncedLyrics"]):
                    continue
                lines = parse_lrc(rec["syncedLyrics"], track.duration_ms)
                if lines:
                    return Lyrics(lines, "lrclib")

        return None

    def instrumental_only(recs: list) -> bool:
        cands = [(r.get("trackName", ""), [r.get("artistName", "")], int((r.get("duration") or 0) * 1000), r)
                 for r in recs if r.get("instrumental") and not r.get("syncedLyrics")]
        return bool(ranked(track, cands, 0.8))

    pool: list = []

    def stage(jobs):
        recs, _ = gather(jobs)
        pool.extend(recs)
        return best_lyrics(pool)

    t0, a0 = variants[0]
    jobs = [lambda: search(track_name=t0)]
    if a0:
        jobs.append(lambda: search(track_name=t0, artist_name=a0))
        if dur:
            jobs.append(lambda: get_exact(t0, a0, track.album))
    r = stage(jobs)
    if r:
        return r
    jobs = [lambda: search(q=f"{t0} {a0}".strip())]
    for t, a in variants[1:]:
        jobs.append(lambda t=t, a=a: search(track_name=t, **({"artist_name": a} if a else {})))
        if a:
            jobs.append(lambda t=t, a=a: search(q=f"{t} {a}"))
    r = stage(jobs[:6])
    if r:
        return r
    return Lyrics([], "lrclib", instrumental=True) if instrumental_only(pool) else None


def provider_netease(track: Track) -> Optional[Lyrics]:
    hdr = {"Referer": "https://music.163.com/", "User-Agent": BROWSER_UA}
    for q in search_queries(track):
        res = http_json("https://music.163.com/api/search/get/web",
                        {"s": q, "type": 1, "offset": 0, "limit": 10, "total": "true"}, hdr)
        songs = (res.get("result") or {}).get("songs") or []
        sid = _pick(track, [(s.get("name", ""), [a.get("name", "") for a in s.get("artists") or []],
                             int(s.get("duration") or 0), s["id"]) for s in songs])
        if not sid:
            continue
        data = None
        try:
            data = http_json("https://music.163.com/api/song/lyric/v1",
                             {"id": sid, "lv": -1, "kv": -1, "tv": -1, "yv": -1}, hdr)
        except Exception as exc:
            log("netease v1 failed:", exc)
        if not data or not (data.get("lrc") or {}).get("lyric"):
            data = http_json("https://music.163.com/api/song/lyric",
                             {"id": sid, "lv": 1, "kv": 1, "tv": -1}, hdr)
        lrc_txt = (data.get("lrc") or {}).get("lyric") or ""
        yrc_txt = (data.get("yrc") or {}).get("lyric") or ""
        lines = parse_yrc(yrc_txt) if yrc_txt else []
        if not lines and lrc_txt:
            lines = parse_lrc(lrc_txt, track.duration_ms)
        if lines:
            tr = (data.get("tlyric") or {}).get("lyric") or ""
            if tr:
                attach_translation(lines, parse_lrc(tr, track.duration_ms))
            return Lyrics(lines, "netease" + ("+words" if lines[0].words else ""))
    return None


def provider_qq(track: Track) -> Optional[Lyrics]:
    hdr = {"Referer": "https://y.qq.com/", "User-Agent": BROWSER_UA}

    def decode(txt: str) -> str:
        txt = txt or ""
        if txt and not txt.lstrip().startswith("["):
            try:
                txt = base64.b64decode(txt).decode("utf-8", "replace")
            except Exception:
                pass
        return html.unescape(txt)

    for q in search_queries(track):
        res = _json_loose(http_text("https://c.y.qq.com/soso/fcgi-bin/client_search_cp",
                                    {"w": q, "format": "json", "p": 1, "n": 10, "cr": 1, "new_json": 1}, hdr))
        songs = ((res.get("data") or {}).get("song") or {}).get("list") or []
        mid = _pick(track, [(s.get("name") or s.get("songname") or "",
                             [x.get("name", "") for x in s.get("singer") or []],
                             int(s.get("interval") or 0) * 1000, s.get("mid") or s.get("songmid"))
                            for s in songs if s.get("mid") or s.get("songmid")])
        if not mid:
            continue
        data = _json_loose(http_text("https://c.y.qq.com/lyric/fcgi-bin/fcg_query_lyric_new.fcg",
                                     {"songmid": mid, "g_tk": 5381, "format": "json", "inCharset": "utf8",
                                      "outCharset": "utf-8", "nobase64": 1}, hdr))
        lines = parse_lrc(decode(data.get("lyric")), track.duration_ms)
        if lines:
            attach_translation(lines, parse_lrc(decode(data.get("trans")), track.duration_ms))
            return Lyrics(lines, "qq")
    return None


_KRC_KEY = bytes([64, 71, 97, 119, 94, 50, 116, 71, 81, 54, 49, 45, 206, 210, 110, 105])


def krc_decode(raw: bytes) -> str:
    if raw[:4] != b"krc1":
        raise ValueError("not a krc file")
    data = bytes(b ^ _KRC_KEY[i % 16] for i, b in enumerate(raw[4:]))
    return zlib.decompress(data).decode("utf-8", "replace")


def parse_krc(text: str) -> list:
    line_re = re.compile(r"^\[(\d+),(\d+)\](.*)$")
    word_re = re.compile(r"<(\d+),(\d+),-?\d+>([^<]*)")
    lines: list = []
    for raw in text.splitlines():
        m = line_re.match(raw.strip())
        if not m:
            continue
        st, dur, body = int(m.group(1)), int(m.group(2)), m.group(3)
        words = [Word(st + int(o), st + int(o) + int(d), t) for o, d, t in word_re.findall(body)]
        if words:
            words[0].text = words[0].text.lstrip()
            words[-1].text = words[-1].text.rstrip()
            words = [w for w in words if w.text]
        text_ = "".join(w.text for w in words) if words else body.strip()
        ln = Line(st, st + dur, text_, None, "", words)
        index_words(ln)
        lines.append(ln)
    lines.sort(key=lambda l: l.start)
    return lines


def provider_kugou(track: Track) -> Optional[Lyrics]:
    hdr = {"User-Agent": BROWSER_UA}
    for q in search_queries(track):
        res = _json_loose(http_text("https://mobilecdn.kugou.com/api/v3/search/song",
                                    {"format": "json", "keyword": q, "page": 1, "pagesize": 10, "showtype": 1}, hdr))
        infos = [i for i in (res.get("data") or {}).get("info") or [] if i.get("hash")]
        pick = _pick(track, [(i.get("songname", ""), [i.get("singername", "")],
                              int(i.get("duration") or 0) * 1000, i) for i in infos])
        if not pick:
            continue
        srch = _json_loose(http_text("https://lyrics.kugou.com/search",
                                     {"ver": 1, "man": "yes", "client": "pc", "keyword": q,
                                      "duration": track.duration_ms or int(pick.get("duration", 0)) * 1000,
                                      "hash": pick["hash"]}, hdr))
        cand = (srch.get("candidates") or [None])[0]
        if not cand:
            continue
        for fmt in ("krc", "lrc"):
            try:
                dl = _json_loose(http_text("https://lyrics.kugou.com/download",
                                           {"ver": 1, "client": "pc", "id": cand["id"],
                                            "accesskey": cand["accesskey"], "fmt": fmt, "charset": "utf8"}, hdr))
                raw = base64.b64decode(dl.get("content") or "")
                if fmt == "krc":
                    lines = parse_krc(krc_decode(raw))
                    src = "kugou+words"
                else:
                    lines = parse_lrc(raw.decode("utf-8", "replace").lstrip("\ufeff"), track.duration_ms)
                    src = "kugou"
            except Exception as exc:
                log(f"kugou {fmt} failed: {exc!r}")
                continue
            if lines:
                return Lyrics(lines, src)
    return None


_YT = None
_YT_LOCK = threading.Lock()


def provider_ytmusic(track: Track) -> Optional[Lyrics]:
    global _YT
    try:
        from ytmusicapi import YTMusic
    except ImportError:
        return None
    with _YT_LOCK:
        if _YT is None:
            _YT = YTMusic()
    vid = None
    m = re.search(r"youtube\.com/watch\?(?:[^#]*&)?v=([\w-]{11})", track.url or "")
    if m:
        vid = m.group(1)
    if not vid:
        for q in search_queries(track):
            res = _YT.search(q, filter="songs", limit=8)
            vid = _pick(track, [(r.get("title", ""), [a.get("name", "") for a in r.get("artists") or []],
                                 int(r.get("duration_seconds") or 0) * 1000, r["videoId"])
                                for r in res if r.get("videoId")])
            if vid:
                break
    if not vid:
        return None
    browse_id = (_YT.get_watch_playlist(vid) or {}).get("lyrics")
    if not browse_id:
        return None
    ly = _YT.get_lyrics(browse_id, timestamps=True)
    if not ly or not ly.get("hasTimestamps"):
        return None

    def g(obj, name):
        return obj.get(name) if isinstance(obj, dict) else getattr(obj, name, None)

    lines = [Line(int(g(x, "start_time") or 0), int(g(x, "end_time") or 0), (g(x, "text") or "").strip())
             for x in ly["lyrics"]]
    lines = finalize([l for l in lines if l.start or l.text], track.duration_ms)
    return Lyrics(lines, "ytmusic") if lines else None


SYNCEDLYRICS_PROVIDERS: list = ["musixmatch", "megalobiz", "lyricsify"]


def provider_syncedlyrics(track: Track) -> Optional[Lyrics]:
    try:
        import syncedlyrics
    except ImportError:
        return None
    for n, q in enumerate(search_queries(track)):
        for enhanced in ((True, False) if n == 0 else (False,)):
            try:
                txt = syncedlyrics.search(q, synced_only=True, providers=SYNCEDLYRICS_PROVIDERS,
                                          enhanced=enhanced)
            except Exception as exc:
                log("syncedlyrics failed:", exc)
                txt = None
            lines = parse_lrc(txt, track.duration_ms) if txt else []
            if lines:
                words = any(l.words for l in lines)
                return Lyrics(lines, "syncedlyrics" + ("+words" if words else ""))
    return None


DEMO_LRC = """\
[00:01.00]M: <00:01.00>City <00:01.45>lights <00:01.90>are <00:02.20>melting <00:02.90>into <00:03.20>the <00:03.50>bay<00:04.40>
[00:05.00]F: <00:05.00>I <00:05.30>can <00:05.60>hear <00:06.00>the <00:06.20>engines <00:06.90>humming <00:07.60>low<00:08.50>
[00:09.00]D: <00:09.00>We <00:09.40>were <00:09.80>never <00:10.30>meant <00:10.80>to <00:11.00>stay<00:12.20>
[00:13.00]
[00:15.00]<00:15.00>Neon <00:15.50>harbor, <00:16.20>take <00:16.60>me <00:16.90>home <00:17.60>tonight, <00:18.60>carry <00:19.20>me <00:19.60>through <00:20.10>the <00:20.40>quiet <00:21.00>blue <00:21.80>and <00:22.10>gold<00:23.50>
[00:24.00]Every line is a little slower than the last, a plain line without word timing
[00:25.00]
[00:29.00]M: <00:29.00>Hold <00:29.50>the <00:29.80>light <00:30.40>a <00:30.60>little <00:31.10>longer<00:32.40>
[00:33.00]F: <00:33.00>Let <00:33.40>the <00:33.70>morning <00:34.30>find <00:34.80>us <00:35.30>here<00:36.50>
[00:37.00]<00:37.00>Neon <00:37.50>harbor, <00:38.20>take <00:38.60>me <00:38.90>home<00:40.20>
[00:41.00]<00:41.00>Nobody <00:41.60>has <00:41.90>to <00:42.10>know <00:42.70>the <00:42.90>way<00:44.20>
[00:45.00]D: <00:45.00>We <00:45.40>will <00:45.80>sing <00:46.30>until <00:46.90>the <00:47.10>sun <00:47.60>comes <00:48.00>up<00:49.50>
"""


def provider_demo(track: Track) -> Optional[Lyrics]:
    lines = parse_lrc(DEMO_LRC, 55_000)
    for l in lines:
        l.start += 4000
        l.end += 4000
        for w in l.words:
            w.start += 4000
            w.end += 4000
    tr = {5000: "Las luces de la ciudad se derriten en la bahía",
          9000: "Puedo oír los motores zumbando bajo",
          19000: "Puerto de neón, llévame a casa esta noche"}
    for l in lines:
        l.trans = tr.get(l.start, "")
    return Lyrics(lines, "demo")


PROVIDERS: dict = {
    "lrclib": provider_lrclib,
    "syncedlyrics": provider_syncedlyrics,
    "ytmusic": provider_ytmusic,
    "kugou": provider_kugou,
    "netease": provider_netease,
    "qq": provider_qq,
    "demo": provider_demo,
}
WORD_CAPABLE = {"syncedlyrics", "kugou", "netease"}
NEG_TTL = 20 * 60
INSTRUMENTAL_TTL = 12 * 3600
RACE_DEADLINE = 9.0
SOFT_WAIT = 4.0
WORD_GRACE = 2.0
ACCEPT = 55

_HEALTH: dict = {}


def _usable(name: str) -> bool:
    return time.time() >= _HEALTH.get(name, (0, 0.0))[1]


def _note(name: str, ok: bool):
    fails = _HEALTH.get(name, (0, 0.0))[0]
    _HEALTH[name] = (0, 0.0) if ok else (fails + 1, time.time() + 300 if fails + 1 >= 3 else 0.0)


def _run_provider(name: str, track: Track) -> tuple:
    try:
        r = PROVIDERS[name](track)
        _note(name, True)
        return r, 0
    except Exception as exc:
        log(f"provider {name} failed: {exc!r}")
        _note(name, False)
        return None, 1


def rate_result(r: Lyrics, track: Track, idx: int, n: int) -> float:
    if r.instrumental:
        return 10.0
    lines = [l for l in r.lines if l.text]
    score = 100.0 if r.has_words else 70.0
    if len(lines) < 4:
        score -= 30
    if PREFER_LATIN and latin_ratio(r.lines) < 0.6:
        score -= 15  # Reduced from 55 to allow valid Devanagari/Hindi lyrics to pass
    d = track.duration_ms
    if d and lines:
        if lines[-1].start > d + 4000:
            score -= 60
        elif lines[-1].start < d * 0.5:
            score -= 15
    return score + (n - idx) * 2

_WAIT = object()


def _race(canon: Track, names: list) -> tuple:
    ex = ThreadPoolExecutor(max_workers=max(1, len(names)))
    t0 = time.monotonic()
    futs = {n: ex.submit(_run_provider, n, canon) for n in names}
    n = len(names)
    out: dict = {}
    seen: set = set()
    state = {"errors": 0, "accept_at": None}

    def absorb():
        for i, nm in enumerate(names):
            if nm not in seen and futs[nm].done():
                seen.add(nm)
                r, err = futs[nm].result()
                state["errors"] += err
                if r and (r.lines or r.instrumental):
                    out[nm] = (rate_result(r, canon, i, n), r)
                    log(f"{nm}: rating {out[nm][0]:.0f}, {len(r.lines)} lines")

    def best_of_all():
        return max(out.values(), key=lambda v: v[0])[1] if out else None

    def decide(now: float):
        have_ok = any(v[0] >= ACCEPT for v in out.values())
        if have_ok and state["accept_at"] is None:
            state["accept_at"] = now
        for i, nm in enumerate(names):
            if nm not in seen:
                if have_ok and now >= SOFT_WAIT:
                    continue
                return _WAIT
            if nm in out and out[nm][0] >= ACCEPT:
                r = out[nm][1]
                if not (r.has_words or r.instrumental):
                    later = [x for x in names[i + 1:] if x in WORD_CAPABLE]
                    for x in later:
                        if x in out and out[x][1].has_words and out[x][0] >= ACCEPT:
                            return out[x][1]
                    if any(x not in seen for x in later) and now < state["accept_at"] + WORD_GRACE:
                        return _WAIT
                return r
        return best_of_all() if len(seen) == n else _WAIT

    try:
        while True:
            absorb()
            now = time.monotonic() - t0
            d = decide(now)
            if d is not _WAIT:
                return d, state["errors"]
            if now >= RACE_DEADLINE:
                return best_of_all(), state["errors"] + (n - len(seen))
            wait([futs[x] for x in names if x not in seen], timeout=0.05, return_when=FIRST_COMPLETED)
    finally:
        ex.shutdown(wait=False, cancel_futures=True)


CACHE_VERSION = 4                                     # v4: purge entries cached with translations
_CACHE_LOCK = threading.Lock()


def _cache_file(track: Track) -> Path:
    key = hashlib.sha1(skeleton(track.title).encode()).hexdigest()[:20]
    return CACHE_DIR / "lyrics" / f"{key}.json"


def _cache_read(track: Track) -> list:
    try:
        d = json.loads(_cache_file(track).read_text(encoding="utf-8"))
        return d.get("entries", []) if d.get("v") == CACHE_VERSION else []
    except Exception:
        return []


def cache_lookup(track: Track) -> tuple:
    now, neg = time.time(), False
    with _CACHE_LOCK:
        entries = _cache_read(track)
    best = None
    for e in entries:
        sc = match_score(track, e.get("title", ""), [e.get("artist", "")], int(e.get("dur", 0)))
        if sc < 0.8:
            continue
        age = now - e.get("ts", 0)
        if e.get("found"):
            if e.get("lyrics", {}).get("instrumental") and age > INSTRUMENTAL_TTL:
                continue
            if best is None or sc > best[0]:
                best = (sc, e)
        elif age < NEG_TTL:
            neg = True
    if best:
        try:
            return "hit", Lyrics.from_json(best[1]["lyrics"])
        except Exception:
            pass
    return ("none", None) if neg else ("miss", None)


def cache_store(track: Track, lyr: Optional[Lyrics], forget_only: bool = False):
    entry = {"title": track.title, "artist": track.artist, "dur": track.duration_ms, "ts": time.time(),
             "found": lyr is not None}
    if lyr is not None:
        entry["lyrics"] = lyr.to_json()
    with _CACHE_LOCK:
        keep = [e for e in _cache_read(track)
                if match_score(track, e.get("title", ""), [e.get("artist", "")], int(e.get("dur", 0))) < 0.8]
        if not forget_only:
            keep.append(entry)
        try:
            path = _cache_file(track)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"v": CACHE_VERSION, "entries": keep[-8:]}, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass


def fetch_lyrics_ex(track: Track, providers: list, force: bool = False) -> tuple:
    canon = canonical_track(track)
    if not canon.title.strip():
        return None, 0
    if force:
        cache_store(canon, None, forget_only=True)
    else:
        state, lyr = cache_lookup(canon)
        if state == "hit":
            return lyr, 0
        if state == "none":
            return None, 0
    names = [p for p in providers if p in PROVIDERS and _usable(p)]
    if not names:
        return None, 1
    lyr, errors = _race(canon, names)
    if lyr or errors * 2 < len(names):
        cache_store(canon, lyr)
    return lyr, errors


def fetch_lyrics(track: Track, providers: list, force: bool = False) -> Optional[Lyrics]:
    return fetch_lyrics_ex(track, providers, force)[0]


class FetchSignals(QObject):
    done = pyqtSignal(int, object, int)


class FetchTask(QRunnable):
    def __init__(self, token, track, providers, force, signals):
        super().__init__()
        self.token, self.track, self.providers = token, track, providers
        self.force, self.signals = force, signals

    def run(self):
        res, errors = None, 1
        try:
            res, errors = fetch_lyrics_ex(self.track, self.providers, self.force)
        except Exception as exc:
            log("fetch crashed:", repr(exc))
        self.signals.done.emit(self.token, res, errors)


class ArtSignals(QObject):
    done = pyqtSignal(str, QImage)


class ArtTask(QRunnable):
    def __init__(self, url, signals):
        super().__init__()
        self.url, self.signals = url, signals

    def run(self):
        img = QImage()
        try:
            if self.url.startswith("data:"):
                _, data = self.url.split(",", 1)
                img.loadFromData(base64.b64decode(data))
            elif self.url.startswith("file://"):
                img.load(unquote(urlparse(self.url).path))
            elif self.url.startswith("http"):
                req = Request(self.url, headers={"User-Agent": UA})
                with urlopen(req, timeout=8) as r:
                    img.loadFromData(r.read())
        except Exception as exc:
            log("art failed:", exc)
        self.signals.done.emit(self.url, img)


# ════════════════════════════════════════════ Windows Media Watcher ══

class WinMediaWatcher(QThread):
    """Polls Windows System Media Transport Controls (SMTC) in a background thread."""
    snapshot = pyqtSignal(dict)

    def __init__(self, cfg: Config):
        super().__init__()
        self.cmds: "queue.Queue[tuple]" = queue.Queue()
        self.pin = cfg["preferred_player"]
        self.ignore = [s.lower() for s in cfg["ignore_players"]]
        self._running = True

    def send(self, *cmd):
        self.cmds.put(cmd)

    def stop(self):
        self._running = False
        self.wait(1500)

    def run(self):
        import ctypes
        try:
            # Initialize COM in Multithreaded Apartment mode
            ctypes.windll.ole32.CoInitializeEx(None, 0)
        except Exception:
            pass
        asyncio.run(self._async_run())

    async def _async_run(self):
        try:
            from winsdk.windows.media.control import (
                GlobalSystemMediaTransportControlsSessionManager as MediaManager,
            )
        except ImportError:
            self.snapshot.emit({"error": "winsdk module missing — run: pip install winsdk"})
            return

        try:
            manager = await MediaManager.request_async()
        except Exception as exc:
            self.snapshot.emit({"error": f"Failed to access Windows Media Manager: {exc}"})
            return

        while self._running:
            try:
                while not self.cmds.empty():
                    cmd = self.cmds.get_nowait()
                    await self._handle_cmd(manager, cmd)

                sessions = manager.get_sessions()
                session_list = []
                active_session = None

                for s in sessions:
                    app_id = s.source_app_user_model_id or "Unknown"
                    if any(ig in app_id.lower() for ig in self.ignore):
                        continue

                    st_clean = "Stopped"
                    try:
                        pb = s.get_playback_info()
                        if pb:
                            st_name = pb.playback_status.name
                            st_clean = "Playing" if st_name == "PLAYING" else ("Paused" if st_name == "PAUSED" else "Stopped")
                    except Exception:
                        pass

                    ident = app_id.split("!")[0].split("\\")[-1].replace(".exe", "").capitalize()
                    session_list.append({"name": app_id, "identity": ident, "status": st_clean})

                    # If this session is playing, mark it as a candidate
                    if st_clean == "Playing" and not active_session:
                        active_session = s

                # Try Windows current session first
                current_session = manager.get_current_session()

                # Fallback to the active playing session or first available session if current_session is None
                if not current_session:
                    current_session = active_session or (sessions[0] if len(sessions) > 0 else None)

                if self.pin:
                    for s in sessions:
                        if self.pin.lower() in (s.source_app_user_model_id or "").lower():
                            current_session = s
                            break

                if current_session:
                    info = await self._read_session(current_session)
                    info["players"] = session_list
                    self.snapshot.emit(info)
                else:
                    self.snapshot.emit({"player": None, "players": session_list})

            except Exception as exc:
                log("WinMediaWatcher loop error:", exc)

            await asyncio.sleep(0.3)

    async def _read_session(self, session) -> dict:
        app_id = session.source_app_user_model_id or "Unknown"
        ident = app_id.split("!")[0].split("\\")[-1].replace(".exe", "").capitalize()

        st_clean = "Stopped"
        can_seek = True
        try:
            pb = session.get_playback_info()
            if pb:
                st_name = pb.playback_status.name
                st_clean = "Playing" if st_name == "PLAYING" else ("Paused" if st_name == "PAUSED" else "Stopped")
                if pb.controls:
                    can_seek = pb.controls.is_seek_enabled
        except Exception:
            pass

        title, artist, album, art_data = "", "", "", ""
        try:
            props = await session.try_get_media_properties_async()
            if props:
                # Clean null bytes and leading/trailing whitespace
                title = (props.title or "").replace("\x00", "").strip()
                artist = (props.artist or "").replace("\x00", "").strip()
                album = (props.album_title or "").replace("\x00", "").strip()
                if props.thumbnail:
                    try:
                        from winsdk.windows.storage.streams import DataReader
                        stream = await props.thumbnail.open_read_async()
                        reader = DataReader(stream)
                        await reader.load_async(stream.size)
                        buf = reader.read_buffer(stream.size)
                        art_data = "data:image/jpeg;base64," + base64.b64encode(bytes(buf)).decode("ascii")
                    except Exception:
                        pass
        except Exception as exc:
            log("Failed reading props:", exc)

        pos_us, len_us = 0, 0
        try:
            timeline = session.get_timeline_properties()
            if timeline:
                pos_us = int(timeline.position.total_seconds() * 1e6)
                len_us = int(timeline.end_time.total_seconds() * 1e6)
        except Exception:
            pass

        return {
            "player": app_id,
            "identity": ident,
            "status": st_clean,
            "title": title,
            "artist": artist,
            "album": album,
            "url": "",
            "length": len_us,
            "art": art_data,
            "trackid": "",
            "pos": pos_us,
            "rate": 1.0,
            "can_seek": can_seek,
        }

    async def _handle_cmd(self, manager, cmd):
        if cmd[0] == "pin":
            self.pin = cmd[1] or ""
            return
        session = manager.get_current_session()
        if not session:
            return
        try:
            if cmd[0] == "playpause":
                await session.try_toggle_play_pause_async()
            elif cmd[0] == "next":
                await session.try_skip_next_async()
            elif cmd[0] == "prev":
                await session.try_skip_previous_async()
            elif cmd[0] == "seek_to":
                target_us = int(cmd[1])
                await session.try_change_playback_position_async(target_us * 10)
        except Exception:
            pass

# ════════════════════════════════════════════ Linux MPRIS Fallback ══

MPRIS_PREFIX = "org.mpris.MediaPlayer2."
PLAYER_IFACE = "org.mpris.MediaPlayer2.Player"


class MprisWatcher(QThread):
    snapshot = pyqtSignal(dict)

    def __init__(self, cfg: Config):
        super().__init__()
        self.cmds: "queue.Queue[tuple]" = queue.Queue()
        self.pin = cfg["preferred_player"]
        self.ignore = [s.lower() for s in cfg["ignore_players"]]
        self._running = True
        self._current: Optional[str] = None
        self._started: dict[str, float] = {}
        self._prev_status: dict[str, str] = {}
        self._last_pos = 0

    def send(self, *cmd):
        self.cmds.put(cmd)

    def stop(self):
        self._running = False
        self.wait(1500)

    def run(self):
        try:
            import dbus
        except ImportError:
            self.snapshot.emit({"error": "python-dbus is missing — install python-dbus"})
            return
        bus = dbus.SessionBus(private=True)
        proxies: dict = {}
        identities: dict = {}
        names: list = []
        names_t = 0.0

        def props_for(n):
            if n not in proxies:
                obj = bus.get_object(n, "/org/mpris/MediaPlayer2", introspect=False)
                proxies[n] = (obj, dbus.Interface(obj, "org.freedesktop.DBus.Properties"))
            return proxies[n][1]

        def read(n):
            p = props_for(n)
            all_ = p.GetAll(PLAYER_IFACE)
            meta = all_.get("Metadata", {}) or {}
            art = meta.get("xesam:artist", "")
            if isinstance(art, (list, tuple)):
                art = ", ".join(str(a) for a in art)
            if n not in identities:
                try:
                    identities[n] = str(p.Get("org.mpris.MediaPlayer2", "Identity"))
                except Exception:
                    identities[n] = n[len(MPRIS_PREFIX):]
            return {
                "name": n, "identity": identities[n],
                "status": str(all_.get("PlaybackStatus", "Stopped")),
                "title": str(meta.get("xesam:title", "")), "artist": str(art),
                "album": str(meta.get("xesam:album", "")),
                "url": str(meta.get("xesam:url", "")),
                "length": int(meta.get("mpris:length", 0) or 0),
                "art": str(meta.get("mpris:artUrl", "")),
                "trackid": str(meta.get("mpris:trackid", "")),
                "pos": int(all_.get("Position", 0) or 0),
                "rate": float(all_.get("Rate", 1.0) or 1.0),
                "can_seek": bool(all_.get("CanSeek", False)),
            }

        infos: dict = {}
        while self._running:
            now = time.monotonic()
            if now - names_t > 1.0:
                try:
                    names = [str(n) for n in bus.list_names() if str(n).startswith(MPRIS_PREFIX)]
                except Exception as exc:
                    log("list_names:", exc)
                    names = []
                names_t = now
                for gone in [n for n in proxies if n not in names]:
                    proxies.pop(gone, None)
                infos = {}
                for n in names:
                    if any(ig in n.lower() for ig in self.ignore):
                        continue
                    try:
                        infos[n] = read(n)
                    except Exception:
                        proxies.pop(n, None)
            elif self._current in infos:
                try:
                    infos[self._current] = read(self._current)
                except Exception:
                    infos.pop(self._current, None)
                    proxies.pop(self._current, None)
            for n, i in infos.items():
                if i["status"] == "Playing" and self._prev_status.get(n) != "Playing":
                    self._started[n] = now
                self._prev_status[n] = i["status"]
            chosen = self._choose(infos)
            self._current = chosen
            players = [{"name": n[len(MPRIS_PREFIX):], "identity": i["identity"],
                        "status": i["status"]} for n, i in infos.items()]
            if chosen:
                s = dict(infos[chosen])
                s["player"] = chosen
                self._last_pos = s["pos"]
            else:
                s = {"player": None}
            s["players"] = players
            self.snapshot.emit(s)
            try:
                cmd = self.cmds.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                self._handle(cmd, infos.get(chosen), proxies, dbus)
            except Exception as exc:
                log("command failed:", cmd, exc)
        bus.close()

    def _choose(self, infos: dict) -> Optional[str]:
        if not infos:
            return None
        if self.pin:
            for n in infos:
                if self.pin.lower() in n.lower():
                    return n
        playing = [n for n, i in infos.items() if i["status"] == "Playing"]
        if playing:
            if self._current in playing:
                return self._current
            return max(playing, key=lambda n: self._started.get(n, 0))
        if self._current in infos:
            return self._current
        for n, i in infos.items():
            if i["title"]:
                return n
        return next(iter(infos))

    def _handle(self, cmd, info, proxies, dbus):
        if cmd[0] == "pin":
            self.pin = cmd[1] or ""
            return
        if not info or info["name"] not in proxies:
            return
        iface = dbus.Interface(proxies[info["name"]][0], PLAYER_IFACE)
        if cmd[0] == "playpause":
            iface.PlayPause()
        elif cmd[0] == "next":
            iface.Next()
        elif cmd[0] == "prev":
            iface.Previous()
        elif cmd[0] == "seek_to":
            target_us = int(cmd[1])
            tid = info.get("trackid", "")
            if tid.startswith("/") and not tid.endswith("NoTrack"):
                try:
                    iface.SetPosition(dbus.ObjectPath(tid), dbus.Int64(target_us))
                    return
                except Exception:
                    pass
            iface.Seek(dbus.Int64(target_us - self._last_pos))


class DemoWatcher(QObject):
    snapshot = pyqtSignal(dict)

    def __init__(self, start_at: float = 0.0):
        super().__init__()
        self.t0 = time.monotonic() - start_at
        self.paused_at: Optional[float] = None
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._emit)

    def start(self):
        self.timer.start(200)
        self._emit()

    def stop(self):
        self.timer.stop()

    def _pos(self) -> float:
        base = self.paused_at if self.paused_at is not None else time.monotonic() - self.t0
        return base % 55.0

    def _emit(self):
        self.snapshot.emit({
            "player": "demo", "identity": "Demo",
            "status": "Paused" if self.paused_at is not None else "Playing",
            "title": "Neon Harbor", "artist": "The Demo Band", "album": "", "url": "",
            "length": 55_000_000, "art": "", "trackid": "", "pos": int(self._pos() * 1e6),
            "rate": 1.0, "can_seek": True, "players": [],
        })

    def send(self, *cmd):
        if cmd[0] == "playpause":
            if self.paused_at is None:
                self.paused_at = time.monotonic() - self.t0
            else:
                self.t0 = time.monotonic() - self.paused_at
                self.paused_at = None
        elif cmd[0] == "seek_to":
            if self.paused_at is not None:
                self.paused_at = cmd[1] / 1e6
            else:
                self.t0 = time.monotonic() - cmd[1] / 1e6
        self._emit()


class Clock:
    def __init__(self):
        self.anchor = 0.0
        self.t0 = time.monotonic()
        self.playing = False
        self.rate = 1.0

    def now_ms(self) -> float:
        if not self.playing:
            return self.anchor
        return self.anchor + (time.monotonic() - self.t0) * 1000.0 * self.rate

    def update(self, pos_ms: float, playing: bool, rate: float = 1.0, force: bool = False):
        pred = self.now_ms()
        if force or playing != self.playing or not playing or abs(pos_ms - pred) > 300:
            self.anchor = pos_ms
        else:
            self.anchor = pred + 0.25 * (pos_ms - pred)
        self.t0 = time.monotonic()
        self.playing, self.rate = playing, rate or 1.0


# ═════════════════════════════════════════════════════════════ background ══

class BgSource:
    def __init__(self, img: QImage, blur: float = 44.0):
        self.img = img.convertToFormat(QImage.Format.Format_RGB32)
        self.blur = blur
        self._pix: dict = {}
        self._stops: dict = {}

    @cached_property
    def palette(self) -> list:
        small = self.img.scaled(32, 32, Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                                Qt.TransformationMode.SmoothTransformation)
        buckets: dict = {}
        for y in range(small.height()):
            for x in range(small.width()):
                c = QColor(small.pixel(x, y))
                h, s, v, _ = c.getHsvF()
                if v < 0.10:
                    continue
                w = s * s * (0.4 + v) + 0.02
                acc = buckets.setdefault(-1 if h < 0 else int(h * 12) % 12, [0.0, 0.0, 0.0, 0.0])
                acc[0] += c.redF() * w
                acc[1] += c.greenF() * w
                acc[2] += c.blueF() * w
                acc[3] += w
        if not buckets:
            return [QColor(70, 70, 110), QColor(40, 60, 100), QColor(90, 60, 110),
                    QColor(60, 80, 120), QColor(80, 60, 100)]

        def vivid(acc) -> QColor:
            c = QColor.fromRgbF(*(acc[i] / acc[3] for i in range(3)))
            h, s, v, _ = c.getHsvF()
            return QColor.fromHsvF(max(h, 0.0), min(1.0, s * 1.25), max(0.55, min(v, 0.92)))

        chosen: list = []
        keys: list = []
        for k, acc in sorted(buckets.items(), key=lambda kv: -kv[1][3]):
            if all(k < 0 or kk < 0 or min((k - kk) % 12, (kk - k) % 12) >= 1 for kk in keys):
                chosen.append(vivid(acc))
                keys.append(k)
            if len(chosen) == 5:
                break
        i = 0
        while len(chosen) < 5:
            h, s, v, _ = chosen[i % len(chosen)].getHsvF()
            chosen.append(QColor.fromHsvF((max(h, 0.0) + 0.05 * (i + 1)) % 1.0, s,
                                          max(0.4, min(0.9, v * (0.8 + 0.12 * (i % 3))))))
            i += 1
        return chosen

    def _stops_for(self, n: int) -> list:
        st = self._stops.get(n)
        if st is None:
            st = []
            for i in range(n):
                c = QColor(self.palette[i % 5])
                if i >= 5:
                    h, sat, v, _ = c.getHsvF()
                    j = i // 5
                    c = QColor.fromHsvF((max(h, 0.0) + 0.035 * j * (1 if i % 2 else -1)) % 1.0,
                                        min(1.0, sat * (0.9 + 0.05 * (i % 3))),
                                        max(0.5, min(0.95, v * (0.88 + 0.07 * (i % 4)))))
                c0, c1, c2 = QColor(c), QColor(c), QColor(c)
                c0.setAlpha(255)
                c1.setAlpha(175)
                c2.setAlpha(0)
                st.append((c0, c1, c2))
            self._stops[n] = st
        return st

    @cached_property
    def _blob_base(self) -> QColor:
        c = self.palette[0]
        return QColor.fromHsvF(max(c.hsvHueF(), 0.0), c.hsvSaturationF(), c.valueF() * 0.28)

    @cached_property
    def _phase(self) -> float:
        return (sum(c.rgb() for c in self.palette) % 628) / 100.0

    def blobs(self, w: int, h: int, t: float) -> QImage:
        k = blob_k(w, h)
        n = max(4, min(24, round(5 * k)))
        rk = 1.0 / math.sqrt(max(1.0, k))
        scale = max(4, math.ceil(max(w, h) / 220))
        lw, lh = max(8, w // scale), max(8, h // scale)
        img = QImage(lw, lh, QImage.Format.Format_ARGB32_Premultiplied)
        img.fill(self._blob_base)
        q = QPainter(img)
        q.setRenderHint(QPainter.RenderHint.Antialiasing)
        q.setPen(Qt.PenStyle.NoPen)
        big = max(lw, lh)
        for i, (c0, c1, c2) in enumerate(self._stops_for(n)):
            fi, mult = i % 5, 1.0 + 0.07 * (i // 5)
            ph = self._phase + i * 1.7
            cx = lw * (0.5 + 0.42 * math.sin(t * (0.115 + 0.021 * fi) * mult + ph))
            cy = lh * (0.5 + 0.42 * math.cos(t * (0.093 + 0.017 * fi) * mult + ph * 1.3))
            r = big * rk * (0.56 + 0.12 * math.sin(t * (0.07 + 0.013 * fi) * mult + ph * 0.7))
            g = QRadialGradient(QPointF(cx, cy), r)
            g.setColorAt(0.0, c0)
            g.setColorAt(0.5, c1)
            g.setColorAt(1.0, c2)
            q.setBrush(g)
            q.drawEllipse(QPointF(cx, cy), r, r)
        q.end()
        return img

    @cached_property
    def _blur_base(self) -> QPixmap:
        S, M = 256, 72
        src = QPixmap.fromImage(self.img).scaled(
            S + 2 * M, S + 2 * M, Qt.AspectRatioMode.KeepAspectRatioByExpanding,
            Qt.TransformationMode.SmoothTransformation)
        item = QGraphicsPixmapItem(src)
        fx = QGraphicsBlurEffect()
        fx.setBlurHints(QGraphicsBlurEffect.BlurHint.QualityHint)
        fx.setBlurRadius(self.blur)
        item.setGraphicsEffect(fx)
        scene = QGraphicsScene()
        scene.addItem(item)
        out = QImage(S + 2 * M, S + 2 * M, QImage.Format.Format_ARGB32_Premultiplied)
        out.fill(Qt.GlobalColor.transparent)
        q = QPainter(out)
        q.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        scene.render(q, QRectF(out.rect()), QRectF(0, 0, S + 2 * M, S + 2 * M))
        q.end()
        return QPixmap.fromImage(out.copy(M, M, S, S))

    def blurred(self, w: int, h: int) -> QPixmap:
        pm = self._pix.get((w, h))
        if pm is None:
            big = self._blur_base.scaled(w, h, Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                                         Qt.TransformationMode.SmoothTransformation)
            pm = QPixmap(w, h)
            q = QPainter(pm)
            q.drawPixmap((w - big.width()) // 2, (h - big.height()) // 2, big)
            q.fillRect(0, 0, w, h, QColor(0, 0, 0, 90))
            q.end()
            if len(self._pix) > 3:
                self._pix.clear()
            self._pix[(w, h)] = pm
        return pm


def demo_art() -> QImage:
    img = QImage(300, 300, QImage.Format.Format_RGB32)
    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    g = QLinearGradient(0, 0, 300, 300)
    g.setColorAt(0, QColor("#0f2a6b"))
    g.setColorAt(1, QColor("#1b1038"))
    p.fillRect(0, 0, 300, 300, g)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor("#ff5f6d"))
    p.drawEllipse(40, 150, 170, 170)
    p.setBrush(QColor("#ffc371"))
    p.drawEllipse(170, 30, 110, 110)
    p.end()
    return img


# ═════════════════════════════════════════════════════════════════ render ══

MODES = {
    "minimal": {"h": 80, "gap": 10, "before": [1.0, 0.0], "after": [1.0, 0.0]},
    "focus":   {"h": 250, "gap": 18, "before": [1.0, 0.0], "after": [1.0, 0.36, 0.0]},
    "scroll":  {"h": 540, "gap": 22, "before": [1.0, 0.42, 0.2, 0.08, 0.0],
                "after": [1.0, 0.52, 0.32, 0.2, 0.1, 0.0]},
}
MODE_ORDER = list(MODES)
BG_STYLES = {"blur": "Blurred album art", "blobs": "Drifting colour blobs", "off": "None (transparent)"}
PAD = 28
MARGIN = 10
SHADOW = [(0, 2, 0.22), (0, 4, 0.16), (1, 3, 0.14), (-1, 3, 0.14), (0, 6, 0.08)]
TGAP = 4
BLOB_REF = 300.0
INTRO_DOTS_MS = 5000
GAP_DOTS_MS = 7000
DOTS_MIN_MS = 3500
BROWSE_SECS = 3.5


def profile(arr: list, x: float) -> float:
    x = abs(x)
    if x >= len(arr) - 1:
        return arr[-1]
    i = int(x)
    return arr[i] + (arr[i + 1] - arr[i]) * (x - i)


def smooth(x: float) -> float:
    x = max(0.0, min(1.0, x))
    return x * x * (3 - 2 * x)


def spring(x: float, v: float, target: float, dt: float, omega: float) -> tuple:
    f = 1.0 + 2.0 * dt * omega
    hoo = dt * omega * omega
    det_inv = 1.0 / (f + dt * hoo)
    return ((f * x + dt * v + dt * hoo * target) * det_inv,
            (v + hoo * (target - x)) * det_inv)


def blob_k(w: float, h: float) -> float:
    return math.sqrt(max(w, 1) * max(h, 1)) / BLOB_REF


def blob_speed(w: float, h: float) -> float:
    return min(3.0, max(0.7, math.sqrt(blob_k(w, h))))


def _cursor_x(line, i: int) -> float:
    r = line.cursorToX(i)
    return float(r[0] if isinstance(r, tuple) else r)


def with_interludes(lines: list) -> list:
    if not lines:
        return lines
    out = []
    if lines[0].text and lines[0].start >= INTRO_DOTS_MS:
        out.append(Line(0, lines[0].start, ""))
    for i, l in enumerate(lines):
        out.append(l)
        nxt = lines[i + 1] if i + 1 < len(lines) else None
        if nxt and l.text and nxt.text and l.words and nxt.start - l.words[-1].end >= GAP_DOTS_MS:
            out.append(Line(l.words[-1].end + 400, nxt.start, ""))
    return out


def count_syllables_en(word: str) -> int:
    word = word.lower().strip()
    if not word:
        return 0
    if len(word) <= 3:
        return 1
    word = re.sub(r'(?:[^laeiouy]es|ed|[^laeiouy]e)$', '', word)
    word = re.sub(r'^y', '', word)
    syllables = len(re.findall(r'[aeiouy]{1,2}', word))
    return max(1, syllables)


def count_syllables_devanagari(text: str) -> int:
    aksharas = re.findall(r'[\u0905-\u0914]|[\u0915-\u0939]\u094D?[\u093E-\u094C\u0902\u0903]?', text)
    return max(1, len(aksharas))


def est_words(line: Line) -> list:
    text = line.text
    if not text:
        return []

    raw_words = re.findall(r"\S+\s*", text)
    weights = []
    for w in raw_words:
        clean_w = w.strip()
        if contains_devanagari(clean_w):
            s_count = count_syllables_devanagari(clean_w)
        else:
            s_count = count_syllables_en(clean_w)

        if clean_w and clean_w[-1] in ",.!?":
            s_count += 0.5
        weights.append(s_count)

    total_syllables = sum(weights) or 1.0
    total_line_dur = max(400, line.end - line.start)

    max_vocal_dur = total_syllables * 450
    active_vocal_dur = min(total_line_dur * 0.82, max_vocal_dur)

    out, pos, t = [], 0, float(line.start)
    for tok, weight in zip(raw_words, weights):
        w_dur = active_vocal_dur * (weight / total_syllables)
        out.append(Word(int(t), int(t + w_dur), tok, pos, pos + len(tok)))
        pos += len(tok)
        t += w_dur

    return out


class Box:
    def __init__(self, line: Line, width: int):
        self.line, self.w = line, width
        self.main = self.trans = None
        self.h_main = self.h = 0.0
        self.words: list = []
        self.total = 0
        self.vl = None
        self.kind = "text"
        self.dur = 0
        self.dim = self.bright = None


class LyricsView(QWidget):
    seek_ms = pyqtSignal(int)
    offset_changed = pyqtSignal(int)
    menu_requested = pyqtSignal()

    def _get_resize_edge(self, pos) -> Optional[str]:
        margin = 12
        x, y = pos.x(), pos.y()
        w, h = self.width(), self.height()
        left, right = x <= margin, x >= w - margin
        top, bottom = y <= margin, y >= h - margin

        if left and top: return "top-left"
        if right and top: return "top-right"
        if left and bottom: return "bottom-left"
        if right and bottom: return "bottom-right"
        if left: return "left"
        if right: return "right"
        if top: return "top"
        if bottom: return "bottom"
        return None

    def __init__(self, cfg: Config, clock: Clock):
        flags = Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.Tool
        super().__init__(None, flags)
        self.cfg, self.clock = cfg, clock
        self.setWindowTitle("Lyrics Overlay")
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setMinimumSize(280, 90)
        self.setMouseTracking(True)
        self._resize_edge = None
        self._start_geo = None
        self._press_global = None
        self.lyrics = Lyrics([], msg=True)
        self.boxes: list = []
        self.starts: list = []
        self.cs: list = []
        self.mode = cfg["mode"] if cfg["mode"] in MODES else "focus"
        self.track_offset = 0
        self.status = "none"
        self.title = self.artist = ""
        self.can_seek = False

        self._pos_f = 0.0
        self._vel = 0.0
        self._act_f = 0.0
        self._act_v = 0.0
        self._idx = -1
        self._browse: Optional[float] = None
        self._browse_until = 0.0
        self._wheel_t = -10.0
        self._thumb_a = 0.0
        self._layer: Optional[QImage] = None
        self._layer_key = None
        self._line_rects: list = []
        self._press = None
        self._dragging = False
        self._manual_move = False
        self._drag_off = None
        self._alpha = 1.0
        self._text_alpha = 1.0
        self._pause_dim = 1.0
        self._idle_at: Optional[float] = None
        self._force_hidden = False
        self._bg_cur: Optional[BgSource] = None
        self._bg_prev: Optional[BgSource] = None
        self._bg_t = 1.0
        self._bg_time = 0.0
        self._drift_frame = False
        self._dirty = True
        self._last = time.monotonic()
        self._last_purge = 0.0
        self._make_fonts()

        self._timer = QTimer(self)
        self._timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._timer.timeout.connect(self._tick)
        self._timer.start(16)
        self.resize(int(cfg["width"]), self._mode_height())

    def _make_fonts(self):
        size = int(self.cfg["font_size"])
        w = min(range(100, 1000, 100), key=lambda v: abs(v - int(self.cfg["font_weight"])))
        f = QFont()
        f.setFamilies([self.cfg["font_family"], "Segoe UI", "Inter", "Arial", "sans-serif"])
        f.setPixelSize(size)
        f.setWeight(QFont.Weight(w))
        f.setStyleStrategy(QFont.StyleStrategy.PreferAntialias)
        t = QFont(f)
        t.setPixelSize(max(12, int(size * 0.56)))
        t.setWeight(QFont.Weight(500))
        self.font_main, self.font_trans = f, t

    def _size_scale(self) -> float:
        return max(0.7, int(self.cfg["font_size"]) / 30)

    def _mode_height(self) -> int:
        return int(MODES[self.mode]["h"] * self._size_scale())

    def _gap(self) -> float:
        return MODES[self.mode]["gap"] * self._size_scale()

    def _align(self):
        return Qt.AlignmentFlag.AlignLeft if self.cfg["align"] == "left" else Qt.AlignmentFlag.AlignHCenter

    def set_lyrics(self, lyr: Lyrics):
        if not lyr.msg:
            lyr = Lyrics(with_interludes(lyr.lines), lyr.source, lyr.instrumental, lyr.msg)
        self.lyrics = lyr
        self.starts = [l.start for l in lyr.lines]
        self._rebuild()
        self._browse = None
        self._idx = self._find_idx(self._t_active())
        self._pos_f = self._act_f = self._follow_target()
        self._vel = self._act_v = 0.0
        self._text_alpha = 0.0
        self._dirty = True

    def set_message(self, text: str, sub: str = ""):
        self.set_lyrics(Lyrics([Line(0, 10 ** 9, text, None, sub)], "msg", msg=True))

    def set_playback(self, status: str):
        if status == self.status:
            return
        self.status = status
        if status in ("none", "stopped"):
            if self._idle_at is None:
                self._idle_at = time.monotonic()
        else:
            self._idle_at = None
            if self.isHidden() and not self._force_hidden:
                self.show()
        self._dirty = True

    def set_meta(self, title: str, artist: str, can_seek: bool):
        self.title, self.artist, self.can_seek = title, artist, can_seek

    def set_art(self, img: Optional[QImage]):
        had = self._bg_cur
        self._bg_cur = None if img is None or img.isNull() else BgSource(img, float(self.cfg["bg_blur"]))
        if had is not self._bg_cur:
            self._bg_prev, self._bg_t = had, 0.0
        self._dirty = True

    def set_bg_style(self, style: str):
        if style in BG_STYLES:
            self.cfg.set("bg_style", style)
            self._dirty = True

    def set_mode(self, mode: str):
        if mode in MODES:
            self.mode = mode
            self.cfg.set("mode", mode)
            self._browse = None
            self.resize(self.width(), self._mode_height())
            self._rebuild()
            self._pos_f = self._follow_target()

    def set_font_size(self, size: int):
        self.cfg.set("font_size", max(14, min(90, size)))
        self._make_fonts()
        self.resize(self.width(), self._mode_height())
        self._rebuild()

    def adjust_offset(self, delta_ms: int, absolute: Optional[int] = None):
        self.track_offset = absolute if absolute is not None else self.track_offset + delta_ms
        self.offset_changed.emit(self.track_offset)

    def reconfigure(self):
        self._make_fonts()
        self._rebuild()

    def user_hide(self, hidden: bool):
        self._force_hidden = hidden
        self.hide() if hidden else self.show()

    def _text_width(self) -> int:
        return max(120, self.width() - 2 * PAD)

    @staticmethod
    def _layout(lay: QTextLayout, w: float) -> float:
        lay.beginLayout()
        y = 0.0
        while True:
            ln = lay.createLine()
            if not ln.isValid():
                break
            ln.setLineWidth(w)
            ln.setPosition(QPointF(0, y))
            y += ln.height()
        lay.endLayout()
        return y

    def _make_box(self, line: Line, w: int, last: bool = False) -> Box:
        b = Box(line, w)
        if not line.text and not self.lyrics.msg:
            b.dur = line.end - line.start
            long_gap = b.dur >= DOTS_MIN_MS and not last
            b.kind = "dots" if long_gap else "gap"
            b.h = float(self.cfg["font_size"]) * (0.95 if long_gap else 0.3)
            return b
        opt = QTextOption(self._align())
        opt.setWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
        text = line.text or "♪"

        font_to_use = self.font_main
        if contains_devanagari(text):
            font_to_use = QFont(self.font_main)
            font_to_use.setPixelSize(int(int(self.cfg["font_size"]) * 1.18))

        b.main = QTextLayout(text, font_to_use)
        b.main.setTextOption(opt)
        b.h_main = b.h = self._layout(b.main, w)

        b.words = line.words or (est_words(line) if self.cfg["estimate_words"] and line.text else [])
        b.total = len(text)
        return b

    def _rebuild(self):
        w = self._text_width()
        n = len(self.lyrics.lines)
        self.boxes = [self._make_box(l, w, i == n - 1) for i, l in enumerate(self.lyrics.lines)]
        gap, y = self._gap(), 0.0
        self.cs = []
        for b in self.boxes:
            self.cs.append(y + b.h / 2)
            y += b.h + gap
        self._dirty = True

    def resizeEvent(self, ev):
        if self.boxes and self._text_width() != self.boxes[0].w:
            self._rebuild()
        super().resizeEvent(ev)

    def _center_at(self, f: float) -> float:
        cs, n = self.cs, len(self.cs)
        if n == 0:
            return 0.0
        if n == 1:
            return cs[0] + f * (self.boxes[0].h + self._gap())
        if f <= 0:
            return cs[0] + f * (cs[1] - cs[0])
        if f >= n - 1:
            return cs[-1] + (f - (n - 1)) * (cs[-1] - cs[-2])
        i = int(f)
        return cs[i] + (cs[i + 1] - cs[i]) * (f - i)

    def _t_true(self) -> float:
        return self.clock.now_ms() + self.cfg["global_offset_ms"] + self.track_offset

    def _t_active(self) -> float:
        return self._t_true() + self.cfg["lead_ms"]

    def _find_idx(self, t: float) -> int:
        return bisect.bisect_right(self.starts, t) - 1 if self.starts else -1

    def _follow_target(self) -> float:
        if self.lyrics.msg:
            return 0.0
        if self.mode == "focus" and 0 <= self._idx < len(self.boxes) - 1:
            return float(self._idx) + 0.5
        return float(self._idx) if self._idx >= 0 else -0.8

    def _tick(self):
        try:
            now = time.monotonic()
            dt = min(now - self._last, 0.05)
            self._last = now
            self._idx = self._find_idx(self._t_active())
            omega = float(self.cfg["spring"])

            if self._browse is not None and now > self._browse_until:
                self._browse = None
            target = self._browse if self._browse is not None else self._follow_target()
            self._pos_f, self._vel = spring(self._pos_f, self._vel, target, dt, omega)
            self._act_f, self._act_v = spring(
                self._act_f,
                self._act_v,
                float(max(self._idx, -1)) if not self.lyrics.msg else 0.0,
                dt,
                omega,
            )
            moving = (
                abs(self._vel) > 1e-3
                or abs(self._pos_f - target) > 1e-3
                or abs(self._act_v) > 1e-3
            )

            # Do not auto-hide if a status message is active (e.g. searching/waiting)
            want_alpha = (
                0.0
                if (
                    self.cfg["hide_when_idle"]
                    and self._idle_at is not None
                    and now - self._idle_at > 6.0
                    and not self.lyrics.msg
                )
                else 1.0
            )

            fade = abs(want_alpha - self._alpha) > 0.004
            self._alpha += (want_alpha - self._alpha) * min(1.0, dt * 6)

            if want_alpha > 0.0 and self.isHidden() and not self._force_hidden:
                self.show()

            if want_alpha == 0.0 and self._alpha < 0.02 and not self.isHidden():
                self.hide()

            want_dim = 0.55 if self.status == "paused" else 1.0
            dim = abs(want_dim - self._pause_dim) > 0.004
            self._pause_dim += (want_dim - self._pause_dim) * min(1.0, dt * 8)

            want_thumb = (
                1.0 if (now - self._wheel_t < 0.9 and self.mode == "scroll") else 0.0
            )
            thumb = abs(want_thumb - self._thumb_a) > 0.01
            self._thumb_a += (want_thumb - self._thumb_a) * min(1.0, dt * 10)

            text_fade = self._text_alpha < 0.995
            self._text_alpha += (1.0 - self._text_alpha) * min(1.0, dt * 9)

            bg_fade = self._bg_t < 1.0
            if bg_fade:
                self._bg_t = min(1.0, self._bg_t + dt / 0.6)
                if self._bg_t >= 1.0:
                    self._bg_prev = None

            drift = (
                self.cfg["bg_style"] == "blobs"
                and self._bg_cur is not None
                and self.isVisible()
            )
            if drift:
                self._bg_time += (
                    dt
                    * blob_speed(self.width(), self.height())
                    * (1.0 if self.status == "playing" else 0.25)
                )

            karaoke = (
                self.clock.playing
                and 0 <= self._idx < len(self.boxes)
                and (
                    bool(self.boxes[self._idx].words)
                    or self.boxes[self._idx].kind == "dots"
                )
            )

            busy = moving or fade or dim or text_fade or bg_fade or karaoke or thumb
            if now - self._last_purge > 3.0:
                self._last_purge = now
                for i, b in enumerate(self.boxes):
                    if abs(i - self._idx) > 12:
                        b.dim = b.bright = None

            self._drift_frame = not self._drift_frame
            if (busy or self._dirty or (drift and self._drift_frame)) and self.isVisible():
                self._dirty = False
                self.update()

            self._timer.setInterval(16 if (busy or drift) else 100)
        except Exception as exc:
            log("_tick exception:", exc)

    def _color_for(self, singer: Optional[str]) -> QColor:
        if singer and singer in self.cfg["colors"]:
            return QColor(self.cfg["colors"][singer])
        return QColor(self.cfg["text_color"])

    def _render(self, b: Box, bright: bool) -> QPixmap:
        dpr = self.devicePixelRatioF()
        m = MARGIN
        pm = QPixmap(math.ceil((b.w + 2 * m) * dpr), math.ceil((b.h + 2 * m) * dpr))
        pm.setDevicePixelRatio(dpr)
        pm.fill(Qt.GlobalColor.transparent)
        p = QPainter(pm)
        p.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.TextAntialiasing)
        col = self._color_for(b.line.singer) if bright else QColor(255, 255, 255)
        tcol = QColor(col)
        tcol.setAlphaF(0.78)

        def draw(x, y):
            b.main.draw(p, QPointF(x, y))
            if b.trans is not None:
                b.trans.draw(p, QPointF(x, y + b.h_main + TGAP))

        p.setPen(QColor(0, 0, 0))
        for dx, dy, a in SHADOW:
            p.setOpacity(a)
            draw(m + dx, m + dy)
        p.setOpacity(1.0)
        p.setPen(col)
        b.main.draw(p, QPointF(m, m))
        if b.trans is not None:
            p.setPen(tcol)
            b.trans.draw(p, QPointF(m, m + b.h_main + TGAP))
        p.end()
        return pm

    def _visual_lines(self, b: Box) -> list:
        if b.vl is None:
            b.vl = []
            for k in range(b.main.lineCount()):
                ln = b.main.lineAt(k)
                s, n = ln.textStart(), ln.textLength()
                b.vl.append((ln.y(), ln.height(), s, n, [_cursor_x(ln, s + j) for j in range(n + 1)]))
        return b.vl

    @staticmethod
    def _char_pos(b: Box, t: float) -> float:
        prev = 0.0
        for w in b.words:
            if t < w.start:
                return prev
            if t < w.end:
                return w.c0 + (w.c1 - w.c0) * (t - w.start) / max(1, w.end - w.start)
            prev = float(w.c1)
        return prev

    def _karaoke_clip(self, b: Box, c: float, x0: float, y0: float, pw: float) -> QPainterPath:
        clip = QPainterPath()
        for ly, lh, ls, n, xs in self._visual_lines(b):
            if c <= ls:
                continue
            if c >= ls + n:
                x = pw
            else:
                j = int(c - ls)
                x = MARGIN + xs[j] + (xs[min(j + 1, n)] - xs[j]) * ((c - ls) - j)
            clip.addRect(QRectF(x0, y0 + MARGIN + ly - 1, x, lh + 2))
        return clip

    def paintEvent(self, ev):
        p = QPainter(self)
        p.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.TextAntialiasing |
                         QPainter.RenderHint.SmoothPixmapTransform)
        p.setCompositionMode(QPainter.CompositionMode.CompositionMode_Source)
        p.fillRect(self.rect(), QColor(0, 0, 0, 0))
        p.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
        self._paint_bg(p)
        if self.mode == "scroll":
            self._paint_lyrics_faded(p)
        else:
            self._paint_lyrics(p)
        self._paint_thumb(p)
        p.end()

    def _paint_bg(self, p: QPainter):
        style = self.cfg["bg_style"]
        if style == "gradient":
            style = "blobs"
        cur, prev, t = self._bg_cur, self._bg_prev, self._bg_t
        if style not in ("blur", "blobs") or (cur is None and prev is None):
            return
        op = float(self.cfg["bg_alpha"]) * self._alpha
        if op < 0.01:
            return
        w, h = self.width(), self.height()
        path = QPainterPath()
        path.addRoundedRect(QRectF(0, 0, w, h), 18, 18)
        p.save()
        p.setClipPath(path)

        def draw(src: BgSource):
            if style == "blobs":
                p.drawImage(QRectF(0, 0, w, h), src.blobs(w, h, self._bg_time))
                p.fillRect(0, 0, w, h, QColor(0, 0, 0, 38))
            else:
                p.drawPixmap(0, 0, src.blurred(w, h))

        if prev is not None:
            p.setOpacity(op * (1.0 if cur is not None else 1.0 - t))
            draw(prev)
        if cur is not None:
            p.setOpacity(op * t)
            draw(cur)
        p.restore()
        p.setOpacity(1.0)

    def _paint_lyrics(self, p: QPainter):
        self._line_rects = []
        n = len(self.boxes)
        if n == 0:
            return
        mode = MODES[self.mode]
        base = self._alpha * self._text_alpha * self._pause_dim
        t = self._t_true()
        pos_f = self._pos_f
        cpos = self._center_at(pos_f)
        cy0 = self.height() / 2
        left = self.cfg["align"] == "left"
        insc = float(self.cfg["inactive_scale"])
        W = self._text_width()
        m = MARGIN
        for i in range(max(0, int(math.floor(pos_f)) - 6), min(n, int(math.ceil(pos_f)) + 8)):
            b = self.boxes[i]
            s = i - pos_f
            if self.lyrics.msg:
                op, a = 0.85, 0.0
            else:
                a = smooth(1 - abs(i - self._act_f))
                op = min(profile(mode["before"] if s < 0 else mode["after"], s), 0.55 + 0.45 * a)
            op *= base
            if op < 0.01:
                continue
            y = cy0 + (self.cs[i] - cpos)
            sc = insc + (1 - insc) * a
            half = b.h * sc / 2
            if y + half < -10 or y - half > self.height() + 10:
                continue
            if b.kind != "text":
                if b.kind == "dots":
                    self._paint_dots(p, b, y, sc, op, a, t, left)
                    self._line_rects.append((i, QRectF(self.width() / 2 - W / 2, y - half, W, half * 2)))
                continue
            if b.dim is None:
                b.dim = self._render(b, False)
            pw, ph = b.w + 2 * m, b.h + 2 * m
            x0 = -m if left else -b.w / 2 - m
            y0 = -ph / 2
            p.save()
            p.translate(PAD if left else self.width() / 2, y)
            p.scale(sc, sc)
            karaoke = bool(b.words) and a > 0.01
            p.setOpacity(op * (1 - 0.62 * a if karaoke else 1.0))
            p.drawPixmap(QPointF(x0, y0), b.dim)
            if a > 0.01:
                if b.bright is None:
                    b.bright = self._render(b, True)
                p.setOpacity(a * base)
                if karaoke:
                    c = self._char_pos(b, t)
                    if c < b.total:
                        p.setClipPath(self._karaoke_clip(b, c, x0, y0, pw))
                p.drawPixmap(QPointF(x0, y0), b.bright)
            p.restore()
            self._line_rects.append((i, QRectF(self.width() / 2 - W / 2, y - half, W, half * 2)))
        p.setOpacity(1.0)

    def _paint_dots(self, p: QPainter, b: Box, y: float, sc: float, op: float, a: float,
                    t: float, left: bool):
        r = float(self.cfg["font_size"]) * 0.25
        spacing = r * 3.0
        start = b.line.start
        prog = max(0.0, min(1.0, (t - start) / max(1, b.dur))) if t >= start else 0.0
        breathe = 1.0 + 0.10 * math.sin(t / 1000.0 * 2 * math.pi / 2.6) if a > 0.01 else 1.0
        leave = smooth((prog - 0.90) / 0.10)
        p.save()
        p.translate(PAD + spacing * 1.5 if left else self.width() / 2, y)
        p.scale(sc * breathe * (1 - 0.35 * leave), sc * breathe * (1 - 0.35 * leave))
        p.setPen(Qt.PenStyle.NoPen)
        for k in (-1, 0, 1):
            level = max(0.0, min(1.0, prog * 3 - (k + 1)))
            alpha = op * (1 - leave) * (0.38 + 0.62 * level)
            rad = r * (0.8 + 0.2 * level)
            p.setBrush(QColor(0, 0, 0))
            p.setOpacity(alpha * 0.28)
            p.drawEllipse(QPointF(k * spacing, r * 0.4), rad, rad)
            p.setBrush(QColor(255, 255, 255))
            p.setOpacity(alpha)
            p.drawEllipse(QPointF(k * spacing, 0), rad, rad)
        p.restore()
        p.setOpacity(1.0)

    def _paint_lyrics_faded(self, p: QPainter):
        dpr = self.devicePixelRatioF()
        w, h = self.width(), self.height()
        key = (w, h, dpr)
        if self._layer is None or self._layer_key != key:
            self._layer = QImage(math.ceil(w * dpr), math.ceil(h * dpr), QImage.Format.Format_ARGB32_Premultiplied)
            self._layer.setDevicePixelRatio(dpr)
            self._layer_key = key
        self._layer.fill(Qt.GlobalColor.transparent)
        lp = QPainter(self._layer)
        lp.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.TextAntialiasing |
                          QPainter.RenderHint.SmoothPixmapTransform)
        self._paint_lyrics(lp)
        lp.setCompositionMode(QPainter.CompositionMode.CompositionMode_DestinationIn)
        f = min(0.22, 90.0 / h)
        g = QLinearGradient(0, 0, 0, h)
        g.setColorAt(0.0, QColor(0, 0, 0, 0))
        g.setColorAt(f, QColor(0, 0, 0, 255))
        g.setColorAt(1.0 - f, QColor(0, 0, 0, 255))
        g.setColorAt(1.0, QColor(0, 0, 0, 0))
        lp.fillRect(QRectF(0, 0, w, h), g)
        lp.end()
        p.drawImage(0, 0, self._layer)

    def _paint_thumb(self, p: QPainter):
        n = len(self.boxes)
        if self._thumb_a < 0.02 or n < 2:
            return
        h, th, margin = self.height(), 34.0, 22.0
        frac = max(0.0, min(1.0, self._pos_f / (n - 1)))
        y = margin + frac * (h - 2 * margin - th)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(255, 255, 255, int(150 * self._thumb_a * self._alpha)))
        p.drawRoundedRect(QRectF(self.width() - 9, y, 3.5, th), 1.75, 1.75)

    def mousePressEvent(self, ev):
        if ev.button() == Qt.MouseButton.LeftButton:
            pos = ev.position()
            self._resize_edge = self._get_resize_edge(pos)
            self._press = pos
            self._press_global = ev.globalPosition()
            self._start_geo = self.geometry()
            self._dragging = False

    def mouseMoveEvent(self, ev):
        pos = ev.position()

        # Handle active edge resizing
        if self._resize_edge and self._press_global:
            delta = ev.globalPosition() - self._press_global
            geo = QRect(self._start_geo)

            if "right" in self._resize_edge:
                geo.setRight(max(geo.left() + 280, int(self._start_geo.right() + delta.x())))
            if "left" in self._resize_edge:
                geo.setLeft(min(geo.right() - 280, int(self._start_geo.left() + delta.x())))
            if "bottom" in self._resize_edge:
                geo.setBottom(max(geo.top() + 70, int(self._start_geo.bottom() + delta.y())))
            if "top" in self._resize_edge:
                geo.setTop(min(geo.bottom() - 70, int(self._start_geo.top() + delta.y())))

            self.setGeometry(geo)
            return

        # Update cursor when hovering over window borders
        if self._press is None:
            edge = self._get_resize_edge(pos)
            if edge in ("left", "right"):
                self.setCursor(Qt.CursorShape.SizeHorCursor)
            elif edge in ("top", "bottom"):
                self.setCursor(Qt.CursorShape.SizeVerCursor)
            elif edge in ("top-left", "bottom-right"):
                self.setCursor(Qt.CursorShape.SizeFDiagCursor)
            elif edge in ("top-right", "bottom-left"):
                self.setCursor(Qt.CursorShape.SizeBDiagCursor)
            else:
                self.setCursor(Qt.CursorShape.ArrowCursor)

        # Standard window dragging
        if self._press is not None and not self._resize_edge:
            if not self._dragging and (ev.position() - self._press).manhattanLength() > 8:
                self._dragging = True
                h = self.windowHandle()
                if not (h and h.startSystemMove()):
                    self._drag_off = ev.globalPosition().toPoint() - self.frameGeometry().topLeft()
                    self._manual_move = True
            if self._manual_move:
                self.move(ev.globalPosition().toPoint() - self._drag_off)

    def mouseReleaseEvent(self, ev):
        if ev.button() == Qt.MouseButton.LeftButton:
            if self._resize_edge or self._dragging:
                self.cfg.set("width", self.width())
                self.cfg.set("pos", [self.x(), self.y()])
            elif self.can_seek and not self.lyrics.msg:
                pt = QPointF(ev.position())
                for i, r in self._line_rects:
                    if r.contains(pt):
                        self._browse = None
                        tgt = self.lyrics.lines[i].start - self.cfg["global_offset_ms"] - self.track_offset
                        self.seek_ms.emit(max(0, int(tgt)))
                        return
            self._press = self._press_global = self._resize_edge = None
            self._dragging = self._manual_move = False
            self.setCursor(Qt.CursorShape.ArrowCursor)

    def wheelEvent(self, ev):
        dy = ev.angleDelta().y()
        mods = ev.modifiers()
        if mods & Qt.KeyboardModifier.ControlModifier:
            self.set_font_size(int(self.cfg["font_size"]) + (2 if dy > 0 else -2))
        elif mods & (Qt.KeyboardModifier.ShiftModifier | Qt.KeyboardModifier.AltModifier):
            self.adjust_offset(50 if dy > 0 else -50)
        elif self.mode == "scroll" and self.boxes and not self.lyrics.msg:
            base = self._browse if self._browse is not None else self._pos_f
            self._browse = max(0.0, min(len(self.boxes) - 1.0, base - dy / 120.0))
            self._browse_until = self._wheel_t = time.monotonic()
            self._browse_until += BROWSE_SECS
            self._dirty = True
        else:
            ev.ignore()
            return
        ev.accept()

    def contextMenuEvent(self, ev):
        self.menu_requested.emit()


# ═══════════════════════════════════════════════════════════════ controller ══

MENU_CSS = """
QMenu { background: rgba(22,22,32,245); color: #f2f2f6; border: 1px solid rgba(255,255,255,40);
        border-radius: 10px; padding: 6px; font-size: 13px; }
QMenu::item { padding: 6px 22px 6px 14px; border-radius: 6px; }
QMenu::item:selected { background: rgba(255,255,255,30); }
QMenu::item:disabled { color: rgba(255,255,255,120); }
QMenu::separator { height: 1px; background: rgba(255,255,255,30); margin: 5px 8px; }
"""


def make_icon() -> QIcon:
    pm = QPixmap(64, 64)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    g = QLinearGradient(0, 0, 64, 64)
    g.setColorAt(0, QColor("#8b5cf6"))
    g.setColorAt(1, QColor("#ec4899"))
    p.setBrush(g)
    p.setPen(Qt.PenStyle.NoPen)
    p.drawRoundedRect(QRectF(4, 4, 56, 56), 16, 16)
    f = QFont()
    f.setPixelSize(38)
    f.setBold(True)
    p.setFont(f)
    p.setPen(QColor("white"))
    p.drawText(QRectF(0, 0, 64, 62), Qt.AlignmentFlag.AlignCenter, "♪")
    p.end()
    return QIcon(pm)


class InstallSignals(QObject):
    done = pyqtSignal(bool, str)


class InstallTask(QRunnable):
    def __init__(self, signals):
        super().__init__()
        self.signals = signals

    def run(self):
        msgs: list = []
        ok = install_extras(msgs.append)
        self.signals.done.emit(ok, "\n".join(msgs))


class OverlayApp(QObject):
    def __init__(self, cfg: Config, demo: bool = False, demo_at: float = 0.0):
        super().__init__()
        self.cfg, self.demo = cfg, demo
        self.clock = Clock()
        self.view = LyricsView(cfg, self.clock)
        self.offsets = OffsetStore()
        self.pool = QThreadPool.globalInstance()
        self.pool.setMaxThreadCount(4)
        self.fetch_sig, self.art_sig = FetchSignals(), ArtSignals()
        self.fetch_sig.done.connect(self._on_fetched)
        self.art_sig.done.connect(self._on_art)
        self.token = 0
        self._quiet = False
        self._retried = False
        self._installing = False
        self._shown_sig = None
        self.inst_sig = InstallSignals()
        self.inst_sig.done.connect(self._on_installed)
        self.track: Optional[Track] = None
        self.track_key: Optional[tuple] = None
        self.art_url = ""
        self.players: list = []
        self.status = "none"
        self.debounce = QTimer(self)
        self.debounce.setSingleShot(True)
        self.debounce.timeout.connect(lambda: self._start_fetch(False))
        self.providers = ["demo"] if demo else list(cfg["providers"])

        if demo:
            self.watcher = DemoWatcher(demo_at)
        elif sys.platform == "win32":
            self.watcher = WinMediaWatcher(cfg)
        else:
            self.watcher = MprisWatcher(cfg)

        self.watcher.snapshot.connect(self.on_snapshot)

        v = self.view
        v.seek_ms.connect(self.seek_to)
        v.offset_changed.connect(self._save_offset)
        v.menu_requested.connect(self.show_menu)
        self._waiting()

        self.tray = None
        if QSystemTrayIcon.isSystemTrayAvailable():
            self.tray = QSystemTrayIcon(make_icon(), self)
            self.tray.setToolTip("Lyrics Overlay")
            self._tray_menu = QMenu()
            self._tray_menu.setStyleSheet(MENU_CSS)
            self._tray_menu.aboutToShow.connect(self._refresh_tray_menu)
            self.tray.setContextMenu(self._tray_menu)
            self.tray.activated.connect(
                lambda r: r == QSystemTrayIcon.ActivationReason.Trigger and self.handle("toggle"))
            self.tray.show()

        self.server = QLocalServer(self)
        QLocalServer.removeServer(APP)
        if self.server.listen(APP):
            self.server.newConnection.connect(self._on_connection)

    def start(self):
        self.view.show()
        if self.demo:
            self.view.set_art(demo_art())
        if not self.demo and missing_extras():
            if self.cfg["auto_install_extras"] and _extras_due():
                self.install_extras()
            else:
                print(f"[{APP}] tip: extra sources available — run python lyrics_overlay.py install-extras",
                      file=sys.stderr)
        self.watcher.start()

    def shutdown(self):
        self.watcher.stop()
        QLocalServer.removeServer(APP)

    def _waiting(self):
        self.view.set_message("Waiting for media playing…", "Start Spotify, YouTube Music, Browser, VLC …")

    def on_snapshot(self, s: dict):
        v = self.view
        if s.get("error"):
            v.set_message("Error", s["error"])
            return
        self.players = s.get("players", [])
        name = s.get("player")
        
        title = (s.get("title") or "").replace("\x00", "").strip()
        artist = (s.get("artist") or "").replace("\x00", "").strip()
        album = (s.get("album") or "").replace("\x00", "").strip()
        
        if not name or (s["status"] == "Stopped" and not title):
            if self.track_key is not None or self.status != "none":
                self.track_key = self.track = None
                self._waiting()
            self.status = "none"
            v.set_playback("none")
            self.clock.update(0, False)
            return
            
        self.status = status = s["status"].lower()
        key = (name, title, artist, album)
        new_track = key != self.track_key
        
        if new_track:
            self.track_key = key
            s["title"] = title
            s["artist"] = artist
            s["album"] = album
            self._new_track(s)
        elif self.track and not self.track.duration_ms and s.get("length", 0) > 0:
            # Update track duration without bumping self.token so active fetch is not dropped
            self.track.duration_ms = s["length"] // 1000

        self.clock.update(s["pos"] / 1000.0, status == "playing", s["rate"], force=new_track)
        v.set_playback(status)
        v.set_meta(title, artist, s["can_seek"])
        if s["art"] != self.art_url:
            self.art_url = s["art"]
            if s["art"]:
                self.pool.start(ArtTask(s["art"], self.art_sig))
            else:
                v.set_art(None)

    def _on_art(self, url: str, img: QImage):
        if url == self.art_url:
            self.view.set_art(img)

    def _new_track(self, s: dict):
        self.token += 1
        self._quiet = self._retried = False
        # Reset provider health locks when a new track starts
        _HEALTH.clear()
        
        title = (s.get("title") or "").replace("\x00", "").strip()
        artist = (s.get("artist") or "").replace("\x00", "").strip()
        
        self.track = Track(title, artist, s.get("album", ""), s.get("length", 0) // 1000, s.get("url", ""))
        self.view.track_offset = self.offsets.get(self.track.key())
        self.view.set_message(f"♪ {title or 'Unknown track'}", "Searching lyrics…")
        self.debounce.start(350)

    def _start_fetch(self, force: bool):
        if self.track and self.track.title:
            self.pool.start(FetchTask(self.token, self.track, self.providers, force, self.fetch_sig))

    def _on_fetched(self, token: int, lyr, errors: int = 0):
        t = self.track
        if t is None:
            return
        # Allow matching if token matches or track key remains identical
        if token != self.token and (self.track_key != (s_player := getattr(self, 'track_key', None))):
            return
            
        self._quiet = False
        shown = not self.view.lyrics.msg
        who = f"{t.title} — {t.artist}" if t.artist else t.title
        
        if lyr is None:
            if errors and not self._retried and not shown:
                self._retried = True
                QTimer.singleShot(8000, lambda tok=token: self._retry(tok))
                self.view.set_message(f"♪ {who}", "Couldn't reach lyric sources — retrying…")
            elif not shown:
                self.view.set_message(f"♪ {who}", "No synced lyrics found")
        elif lyr.instrumental:
            if not shown:
                self.view.set_message("♪ Instrumental ♪", t.title)
        else:
            sig = (lyr.source, len(lyr.lines), lyr.lines[0].start)
            if not (shown and sig == self._shown_sig):
                self._shown_sig = sig
                self.view.set_lyrics(lyr)

    def _retry(self, token: int):
        if token == self.token:
            self._start_fetch(False)

    def install_extras(self):
        if self._installing:
            return
        self._installing = True
        _extras_mark()
        if self.view.lyrics.msg:
            self.view.set_message("Installing extra lyric sources…", "one-time setup in background")
        self.pool.start(InstallTask(self.inst_sig))

    def _on_installed(self, ok: bool, info: str):
        self._installing = False
        if not ok:
            print(f"[{APP}] failed installing extra lyric sources:\n{info}\n", file=sys.stderr)
        if self.track and self.view.lyrics.msg:
            self.handle("reload")
        elif self.view.lyrics.msg:
            self._waiting()

    def _save_offset(self, ms: int):
        if self.track:
            self.offsets.set(self.track.key(), ms)

    def seek_to(self, ms: int):
        self.watcher.send("seek_to", int(ms * 1000))
        self.clock.update(ms, self.status == "playing", force=True)

    def handle(self, cmd: str):
        parts = cmd.split()
        if not parts:
            return
        c, arg, v = parts[0], parts[1:], self.view
        try:
            if c == "toggle":
                v.user_hide(not v.isHidden())
            elif c in ("show", "hide"):
                v.user_hide(c == "hide")
            elif c == "mode" and arg:
                v.set_mode(arg[0])
            elif c == "next-mode":
                v.set_mode(MODE_ORDER[(MODE_ORDER.index(v.mode) + 1) % len(MODE_ORDER)])
            elif c == "bg" and arg:
                v.set_bg_style(arg[0])
            elif c == "next-bg":
                order = list(BG_STYLES)
                v.set_bg_style(order[(order.index(self.cfg["bg_style"]) + 1) % len(order)]
                               if self.cfg["bg_style"] in order else "blur")
            elif c == "offset" and arg:
                v.adjust_offset(0, absolute=0) if arg[0] == "reset" else v.adjust_offset(int(arg[0]))
            elif c == "font" and arg:
                v.set_font_size(int(self.cfg["font_size"]) + int(arg[0]))
            elif c == "reload":
                self.token += 1
                self._quiet = self._retried = False
                v.set_message("♪ Reloading…", "Searching lyrics…")
                self._start_fetch(True)
            elif c == "quit":
                QApplication.quit()
        except ValueError:
            pass

    def _on_connection(self):
        while self.server.hasPendingConnections():
            sock = self.server.nextPendingConnection()
            sock.waitForReadyRead(200)
            for line in bytes(sock.readAll()).decode("utf-8", "ignore").splitlines():
                self.handle(line.strip())
            sock.disconnectFromServer()

    def populate_menu(self, m: QMenu):
        v, cfg = self.view, self.cfg

        def radio(menu, items, current, apply):
            for key, label in items:
                a = menu.addAction(label)
                a.setCheckable(True)
                a.setChecked(current == key)
                a.triggered.connect(lambda _=False, k=key: apply(k))

        def toggle(label, key):
            a = m.addAction(label)
            a.setCheckable(True)
            a.setChecked(bool(cfg[key]))
            a.triggered.connect(lambda c: (cfg.set(key, c), v.reconfigure()))

        head = m.addAction("♪  " + (v.title or "Lyrics Overlay") + (f" — {v.artist}" if v.artist else ""))
        head.setEnabled(False)
        m.addAction("Show overlay" if v.isHidden() else "Hide overlay", lambda: self.handle("toggle"))
        m.addSeparator()
        radio(m.addMenu("Layout"), [(k, k.capitalize()) for k in MODE_ORDER], v.mode, v.set_mode)
        radio(m.addMenu("Background"), list(BG_STYLES.items()),
              "blobs" if cfg["bg_style"] == "gradient" else cfg["bg_style"], v.set_bg_style)
        radio(m.addMenu("Alignment"), [("center", "Center"), ("left", "Left")], cfg["align"],
              lambda k: (cfg.set("align", k), v.reconfigure()))
        toggle("Karaoke sweep on lines without word timing", "estimate_words")
        m.addSeparator()
        m.addAction("Font bigger  (Ctrl+wheel)", lambda: v.set_font_size(int(cfg["font_size"]) + 2))
        m.addAction("Font smaller", lambda: v.set_font_size(int(cfg["font_size"]) - 2))
        m.addAction("Lyrics earlier  (+50 ms, Shift+wheel)", lambda: v.adjust_offset(50))
        m.addAction("Lyrics later  (−50 ms)", lambda: v.adjust_offset(-50))
        m.addAction("Reset sync offset", lambda: v.adjust_offset(0, absolute=0))
        m.addSeparator()
        pm = m.addMenu("Player")
        pin = cfg["preferred_player"].lower()
        radio(pm, [("", "Auto (whatever is playing)")] +
              [(p["name"], f"{p['identity']}  ·  {p['status']}") for p in self.players],
              next((p["name"] for p in self.players if pin and pin in p["name"].lower()), ""),
              self._pin)
        m.addAction("Reload lyrics", lambda: self.handle("reload"))
        if missing_extras():
            m.addAction("Install extra lyric sources (Musixmatch, YouTube Music)", self.install_extras)
        m.addSeparator()
        m.addAction("Quit", QApplication.quit)

    def show_menu(self):
        m = QMenu(self.view)
        m.setStyleSheet(MENU_CSS)
        self.populate_menu(m)
        m.exec(QCursor.pos())

    def _refresh_tray_menu(self):
        self._tray_menu.clear()
        self.populate_menu(self._tray_menu)

    def _pin(self, name: str):
        self.cfg.set("preferred_player", name)
        self.watcher.send("pin", name)


# ═════════════════════════════════════════════════════════════════════ CLI ══

CTL_HELP = """usage: python lyrics_overlay.py ctl <command>
  toggle | show | hide
  mode <minimal|focus|scroll> | next-mode
  bg <blur|blobs|off> | next-bg
  offset <+/-ms|reset> | font <+/-px>
  reload | quit"""


def ctl_main(args: list) -> int:
    if not args:
        print(CTL_HELP)
        return 1
    return 0 if _send(" ".join(args)) else _fail("lyrics overlay is not running")


def _fail(msg: str) -> int:
    print(msg, file=sys.stderr)
    return 1


def _send(text: str) -> bool:
    sock = QLocalSocket()
    sock.connectToServer(APP)
    if sock.waitForConnected(500):
        sock.write((text + "\n").encode("utf-8"))
        sock.waitForBytesWritten(500)
        sock.disconnectFromServer()
        return True
    return False


def main() -> int:
    global VERBOSE, PREFER_LATIN
    
    # Force UTF-8 stdout encoding on Windows console
    if sys.platform == "win32":
        try:
            sys.stdout.reconfigure(encoding="utf-8")
            sys.stderr.reconfigure(encoding="utf-8")
        except Exception:
            pass

    ap = argparse.ArgumentParser(description="Synced lyrics overlay for Windows and Linux")
    ap.add_argument("--demo", action="store_true", help="fake player + built-in demo lyrics")
    ap.add_argument("--mode", choices=MODE_ORDER, help="start in this layout")
    ap.add_argument("--bgstyle", choices=list(BG_STYLES), help="background style for this run")
    ap.add_argument("--verbose", "-v", action="store_true")
    args = ap.parse_args()
    VERBOSE = args.verbose

    if not args.demo and _send("show"):
        print("Lyrics overlay is already running.")
        return 0

    qapp = QApplication(sys.argv)
    qapp.setApplicationName(APP)
    qapp.setQuitOnLastWindowClosed(False)

    cfg = Config()
    PREFER_LATIN = bool(cfg["prefer_latin"])
    SYNCEDLYRICS_PROVIDERS[:] = list(cfg["syncedlyrics_providers"])

    if args.mode:
        cfg.data["mode"] = args.mode
    if args.bgstyle:
        cfg.data["bg_style"] = args.bgstyle

    app = OverlayApp(cfg, demo=args.demo)

    scr = qapp.primaryScreen().availableGeometry()
    pos = cfg["pos"]
    app.view.move(*(map(int, pos) if pos else (scr.center().x() - app.view.width() // 2,
                                                scr.bottom() - app.view.height() - 80)))

    app.start()
    qapp.aboutToQuit.connect(app.shutdown)
    return qapp.exec()


if __name__ == "__main__":
    sys.exit(main())
