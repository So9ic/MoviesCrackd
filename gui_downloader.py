#!/usr/bin/env python3
"""
GUI Downloader — Modern download manager for driveseed.org / ModPro / AnimeFlix.

v1.2 — Pipeline overlap, DNS pre-warming, session reuse, resume support,
        V1/V2 method label, smarter file-exists check.
"""

import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from html import unescape
from pathlib import Path
from queue import Empty, PriorityQueue
from urllib.parse import parse_qs, urlparse

import customtkinter as ctk
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from env_loader import load_env_file, session_file_exists

load_env_file()

# ── Reuse the core logic from the CLI tools ─────────────────────────────

try:
    from concurrent.futures import ThreadPoolExecutor, as_completed

    from batch_episodes import resolve_link, scrape_links

    HAS_BATCH = True
except ImportError:
    HAS_BATCH = False

from direct_downloader import (
    HEADERS,
    get_driveseed_download_url,
    get_driveseed_file_metadata,
)

try:
    from telegram_fallback import TelegramDownloadError

    HAS_TELEGRAM_FALLBACK = True
except ImportError:
    HAS_TELEGRAM_FALLBACK = False

    class TelegramDownloadError(Exception):
        pass

# ── Movie Search Engine Imports ─────────────────────────────────────────
try:
    from movie_search import search_movies, extract_download_options
except ImportError:
    pass

# ── Shared Session with connection pooling ──────────────────────────────


def make_session():
    """Create a requests.Session with connection pooling and retries."""
    s = requests.Session()
    s.headers.update(HEADERS)
    adapter = HTTPAdapter(
        pool_connections=20,
        pool_maxsize=20,
        max_retries=Retry(total=2, backoff_factor=0.3),
    )
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    return s


SESSION = make_session()


# ── DNS Pre-warming ─────────────────────────────────────────────────────

DNS_DOMAINS = [
    "driveseed.org",
    "instant.video-leech.pro",
    "cdn.video-leech.pro",
    "instant.video-gen.xyz",
    "cdn.video-gen.xyz",
    "video-seed.dev",
    "video-seed.pro",
    "video-downloads.googleusercontent.com",
    "cloud.unblockedgames.world",
]


def prewarm_dns():
    """Resolve all known domains in parallel to warm the DNS cache."""

    def _resolve(host):
        try:
            socket.getaddrinfo(host, 443)
        except Exception:
            pass

    with ThreadPoolExecutor(max_workers=len(DNS_DOMAINS)) as pool:
        pool.map(_resolve, DNS_DOMAINS)


# ── Appearance ──────────────────────────────────────────────────────────

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")

FONT_FAMILY = "Segoe UI"
BG = "#0d0d12"
CARD = "#16161e"
CARD_DONE = "#111a15"
CARD_FAIL = "#1a1114"
ACCENT = "#7c6aef"
ACCENT2 = "#3fb8f0"
GREEN = "#34d399"
RED = "#ef4444"
AMBER = "#f59e0b"
DIM = "#555"
TEXT = "#e4e4e8"
SUBTEXT = "#8b8b9e"

MAX_CONCURRENT = 2
CHUNK_SIZE = 2 * 1024 * 1024  # 2 MB

TELEGRAM_VERBOSE_DEBUG = (
    os.getenv("TELEGRAM_VERBOSE_DEBUG", "1").strip().lower() not in ("0", "false", "no", "off")
)


def tg_debug(msg: str) -> None:
    if TELEGRAM_VERBOSE_DEBUG:
        ts = time.strftime("%H:%M:%S")
        print(f"[TG-DEBUG {ts}] {msg}", flush=True)


def fmt_bytes(num_bytes: int | None) -> str:
    if not num_bytes or num_bytes <= 0:
        return "unknown"
    value = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1000 or unit == "TB":
            if unit == "B":
                return f"{int(value)} {unit}"
            return f"{value:.1f} {unit}"
        value /= 1000
    return f"{num_bytes} B"


def parse_size_hint_bytes(text: str | None) -> int | None:
    if not text:
        return None
    m = re.search(r"(\d+(?:\.\d+)?)\s*([kmgt]i?b)", text, re.IGNORECASE)
    if not m:
        return None

    value = float(m.group(1))
    unit = m.group(2).lower()
    multipliers = {
        "b": 1,
        "kb": 1000,
        "mb": 1000 ** 2,
        "gb": 1000 ** 3,
        "tb": 1000 ** 4,
        "kib": 1024,
        "mib": 1024 ** 2,
        "gib": 1024 ** 3,
        "tib": 1024 ** 4,
    }
    factor = multipliers.get(unit)
    if not factor:
        return None
    return int(value * factor)


# ── Download Item Widget ────────────────────────────────────────────────


class DownloadCard(ctk.CTkFrame):
    """A single download item with progress bar and status info."""

    STATE_PENDING = 0
    STATE_DOWNLOADING = 1
    STATE_DONE = 2
    STATE_FAILED = 3

    @staticmethod
    def _method_color(method):
        if method == "V2":
            return GREEN
        if method == "V1":
            return AMBER
        if method == "CLOUD":
            return ACCENT2
        if method == "TELEGRAM":
            return "#60a5fa"
        return DIM

    def __init__(self, master, index, filename, method="", **kw):
        super().__init__(master, corner_radius=10, fg_color=CARD, **kw)
        self.grid_columnconfigure(0, weight=1)
        self.state = self.STATE_PENDING
        self.item_data = None

        # Row 0: index + filename + method badge + status
        header = ctk.CTkFrame(self, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", padx=12, pady=(10, 2))
        header.grid_columnconfigure(1, weight=1)

        self.idx_lbl = ctk.CTkLabel(
            header, text=f"#{index}", font=(FONT_FAMILY, 11, "bold"), text_color=ACCENT
        )
        self.idx_lbl.grid(row=0, column=0, padx=(0, 8))

        self.name_lbl = ctk.CTkLabel(
            header, text=filename, font=(FONT_FAMILY, 12), text_color=TEXT, anchor="w"
        )
        self.name_lbl.grid(row=0, column=1, sticky="w")

        # Method badge (V2/V1)
        badge_color = self._method_color(method)
        self.method_lbl = ctk.CTkLabel(
            header,
            text=method or "…",
            font=(FONT_FAMILY, 9, "bold"),
            text_color="#000",
            fg_color=badge_color,
            corner_radius=4,
            width=52,
            height=18,
        )
        self.method_lbl.grid(row=0, column=2, padx=(6, 6))

        self.status_lbl = ctk.CTkLabel(
            header, text="Pending", font=(FONT_FAMILY, 11), text_color=SUBTEXT
        )
        self.status_lbl.grid(row=0, column=3, padx=(0, 0))

        self.action_btn = ctk.CTkButton(
            header,
            text="",
            width=78,
            height=22,
            corner_radius=7,
            font=(FONT_FAMILY, 10, "bold"),
            fg_color="#2563eb",
            hover_color="#1d4ed8",
            command=lambda: None,
        )
        self.action_btn.grid(row=0, column=4, padx=(8, 0))
        self.action_btn.grid_remove()

        # Row 1: progress bar
        self.progress = ctk.CTkProgressBar(
            self, height=6, corner_radius=3, progress_color=ACCENT
        )
        self.progress.grid(row=1, column=0, sticky="ew", padx=12, pady=(4, 2))
        self.progress.set(0)

        # Row 2: details
        self.detail_lbl = ctk.CTkLabel(
            self, text="", font=(FONT_FAMILY, 10), text_color=DIM, anchor="w"
        )
        self.detail_lbl.grid(row=2, column=0, sticky="ew", padx=14, pady=(0, 8))

    def set_method(self, method):
        color = self._method_color(method)
        self.method_lbl.configure(text=method, fg_color=color)

    def set_status(self, status, color=SUBTEXT):
        self.status_lbl.configure(text=status, text_color=color)

    def set_progress(self, value):
        self.progress.set(value)

    def set_detail(self, text):
        self.detail_lbl.configure(text=text)

    def set_action(self, text, command, state="normal"):
        self.action_btn.configure(text=text, command=command, state=state)
        self.action_btn.grid()

    def hide_action(self):
        self.action_btn.grid_remove()

    def mark_done(self):
        self.state = self.STATE_DONE
        self.configure(fg_color=CARD_DONE)
        self.set_status("✓ Done", GREEN)
        self.progress.configure(progress_color=GREEN)
        self.set_progress(1.0)
        self.hide_action()

    def mark_failed(self, reason=""):
        self.state = self.STATE_FAILED
        self.configure(fg_color=CARD_FAIL)
        self.set_status("✗ Failed", RED)
        self.progress.configure(progress_color=RED)
        if reason:
            self.set_detail(reason[:80])

    def mark_downloading(self):
        self.state = self.STATE_DOWNLOADING
        self.set_status("Downloading…", ACCENT2)

    def mark_pending(self):
        self.state = self.STATE_PENDING
        self.configure(fg_color=CARD)
        self.set_status("Pending", SUBTEXT)
        self.set_detail("Retrying…")
        self.progress.configure(progress_color=ACCENT)
        self.set_progress(0)


# ── Search Engine Helper Widgets ─────────────────────────────────────────

class SearchResultCard(ctk.CTkFrame):
    def __init__(self, parent, item, on_click_callback):
        super().__init__(
            parent,
            fg_color=CARD,
            corner_radius=12,
            border_width=1,
            border_color="#2a2a3a",
            height=140,
        )
        self.item = item
        self.on_click_callback = on_click_callback
        
        self.grid_propagate(False)
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)
        
        # Larger Poster Thumbnail (asynchronous loading)
        self.img_frame = ctk.CTkFrame(self, width=90, height=125, fg_color="#2a2a3a", corner_radius=6)
        self.img_frame.grid(row=0, column=0, padx=12, pady=7, sticky="ns")
        
        if item.get("thumbnail"):
            threading.Thread(target=self._load_image, daemon=True).start()
            
        # Category Badge + Title Frame
        info_frame = ctk.CTkFrame(self, fg_color="transparent")
        info_frame.grid(row=0, column=1, sticky="nsew", padx=8, pady=8)
        info_frame.grid_columnconfigure(0, weight=1)
        
        # Category Badge
        cat = item.get("category", "ALL").upper()
        cat_colors = {
            "HOLLYWOOD": "#10b981", # emerald green
            "BOLLYWOOD": "#3b82f6", # bright blue
            "ANIME": "#ef4444",     # red
        }
        badge_color = cat_colors.get(cat, "#8b5cf6")
        
        cat_lbl = ctk.CTkLabel(
            info_frame,
            text=f"  {cat}  ",
            font=(FONT_FAMILY, 9, "bold"),
            text_color="white",
            fg_color=badge_color,
            corner_radius=6,
        )
        cat_lbl.pack(anchor="w", pady=(2, 4))
        
        # Movie/Show Title
        title_lbl = ctk.CTkLabel(
            info_frame,
            text=item.get("title", ""),
            font=(FONT_FAMILY, 14, "bold"),
            text_color="white",
            anchor="w",
            justify="left",
            wraplength=480,
        )
        title_lbl.pack(fill="x", anchor="w")
        
        # Forward scroll events to the parent scrollable frame's canvas
        canvas = parent._parent_canvas
        
        # Make entire card clickable and hovered
        for w in [self, self.img_frame, info_frame, cat_lbl, title_lbl]:
            w.bind("<Button-1>", lambda e: self._on_click())
            w.bind("<Enter>", lambda e: self.configure(border_color=ACCENT, fg_color="#222230"))
            w.bind("<Leave>", lambda e: self.configure(border_color="#2a2a3a", fg_color=CARD))

        curr = self
        found_app = None
        while curr is not None:
            if hasattr(curr, '_bind_scroll_to_canvas'):
                found_app = curr
                break
            curr = curr.master
        if found_app:
            found_app._bind_scroll_to_canvas(self, canvas)

    def _on_click(self):
        self.on_click_callback(self.item)

    def _load_image(self):
        try:
            from PIL import Image, ImageTk
            import io
            
            resp = requests.get(self.item["thumbnail"], timeout=5)
            if resp.status_code == 200:
                img = Image.open(io.BytesIO(resp.content))
                img = img.resize((90, 125), Image.Resampling.LANCZOS)
                photo = ctk.CTkImage(light_image=img, dark_image=img, size=(90, 125))
                
                def update_ui():
                    if self.winfo_exists():
                        self.img_frame.grid_forget()
                        self.img_lbl = ctk.CTkLabel(self, image=photo, text="")
                        self.img_lbl.image = photo
                        self.img_lbl.grid(row=0, column=0, padx=12, pady=7, sticky="ns")
                        self.img_lbl.bind("<Button-1>", lambda e: self._on_click())
                        
                        canvas = self.master._parent_canvas
                        
                        # Bind scroll to image widget too
                        self.img_lbl.bind("<Enter>", lambda e: self.configure(border_color=ACCENT, fg_color="#222230"))
                        self.img_lbl.bind("<Leave>", lambda e: self.configure(border_color="#2a2a3a", fg_color=CARD))
                        
                        # Trace app to bind scroll safely
                        curr = self
                        found_app = None
                        while curr is not None:
                            if hasattr(curr, '_bind_scroll_to_canvas'):
                                found_app = curr
                                break
                            curr = curr.master
                        if found_app:
                            found_app._bind_scroll_to_canvas(self.img_lbl, canvas)
                            
                self.after(0, update_ui)
        except Exception:
            pass


class QualitySelectorDialog(ctk.CTkToplevel):
    def __init__(self, parent, movie_item, options, on_select_callback):
        super().__init__(parent)
        self.title("Select Quality / Option")
        self.geometry("520x400")
        self.transient(parent)
        self.configure(fg_color="#13131a")
        
        # Center relative to parent
        try:
            parent.update_idletasks()
            x = parent.winfo_x() + (parent.winfo_width() - 520) // 2
            y = parent.winfo_y() + (parent.winfo_height() - 400) // 2
            self.geometry(f"+{x}+{y}")
        except Exception:
            pass

        # Make modal safely by waiting for window visibility
        self.deiconify()
        self.wait_visibility()
        self.grab_set()
            
        self.grid_rowconfigure(2, weight=1)
        self.grid_columnconfigure(0, weight=1)
        
        # Movie/show Title
        ctk.CTkLabel(
            self,
            text=movie_item["title"],
            font=(FONT_FAMILY, 13, "bold"),
            text_color="white",
            justify="center",
            wraplength=480,
        ).grid(row=0, column=0, padx=20, pady=(16, 4))
        
        ctk.CTkLabel(
            self,
            text="Available Download Options:",
            font=(FONT_FAMILY, 11),
            text_color=SUBTEXT,
        ).grid(row=1, column=0, padx=20, pady=(0, 10))
        
        # Option Scrollable Frame
        scroll = ctk.CTkScrollableFrame(
            self,
            fg_color="transparent",
            corner_radius=0,
        )
        scroll.grid(row=2, column=0, sticky="nsew", padx=16, pady=4)
        scroll.grid_columnconfigure(0, weight=1)
        
        for opt in options:
            opt_frame = ctk.CTkFrame(scroll, fg_color="#1a1a24", corner_radius=8, border_width=1, border_color="#2a2a3a")
            opt_frame.pack(fill="x", pady=4, ipady=4)
            opt_frame.grid_columnconfigure(0, weight=1)
            
            lbl = ctk.CTkLabel(
                opt_frame,
                text=opt["quality"],
                font=(FONT_FAMILY, 11, "bold"),
                text_color="white",
                anchor="w",
                justify="left",
                wraplength=340,
            )
            lbl.grid(row=0, column=0, sticky="w", padx=12, pady=6)
            
            btn = ctk.CTkButton(
                opt_frame,
                text=opt["button_text"],
                width=110,
                height=30,
                corner_radius=6,
                font=(FONT_FAMILY, 11, "bold"),
                fg_color=ACCENT,
                hover_color="#6a5ad4",
                command=lambda url=opt["url"], d=self: [d.destroy(), on_select_callback(url)]
            )
            btn.grid(row=0, column=1, padx=12, pady=6)


# ── Main App ────────────────────────────────────────────────────────────


class DownloaderApp(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("DriveSeed Downloader")
        self.geometry("680x720")
        self.configure(fg_color=BG)
        self.minsize(520, 400)

        self.output_dir = os.path.expanduser("/media/so9ic/HDD/Downloads/Movies")
        self.cards: list[DownloadCard] = []
        self.download_queue = PriorityQueue()
        self._active_threads = 0
        self._lock = threading.Lock()
        self._telegram_lock = threading.Lock()

        # Thread-safe counters
        self._done_count = 0
        self._fail_count = 0
        self._total_count = 0

        self._build_ui()

        # Pre-warm DNS in background on startup
        threading.Thread(target=prewarm_dns, daemon=True).start()

        # Start Modlist Background Poller
        self._start_modlist_poller()

        # Set default tab on startup
        self.after(50, lambda: self.tabview.set("Search Engine"))

    def _build_ui(self):
        self.grid_rowconfigure(1, weight=1)  # Tabview expands dynamically to occupy full vertical height
        self.grid_columnconfigure(0, weight=1)

        # ── Title bar ──
        title_frame = ctk.CTkFrame(self, fg_color="transparent")
        title_frame.grid(row=0, column=0, sticky="ew", padx=20, pady=(16, 4))

        ctk.CTkLabel(
            title_frame,
            text="DriveSeed Downloader",
            font=(FONT_FAMILY, 20, "bold"),
            text_color=TEXT,
        ).pack(side="left")

        ctk.CTkLabel(
            title_frame, text="v1.2", font=(FONT_FAMILY, 10), text_color=DIM
        ).pack(side="left", padx=(8, 0), pady=(6, 0))

        # ── Tabview ──
        self.tabview = ctk.CTkTabview(
            self,
            fg_color="transparent",
            corner_radius=10,
            segmented_button_fg_color="#1a1a24",
            segmented_button_selected_color=ACCENT,
            segmented_button_selected_hover_color="#6a5ad4",
            segmented_button_unselected_color="#2a2a3a",
            segmented_button_unselected_hover_color="#3a3a4a",
        )
        self.tabview.grid(row=1, column=0, sticky="nsew", padx=20, pady=(8, 4))

        self.tabview.add("Direct URL")
        self.tabview.add("Search Engine")

        # ── Tab 1: Direct URL ──
        tab_direct = self.tabview.tab("Direct URL")
        tab_direct.grid_columnconfigure(0, weight=1)
        tab_direct.grid_rowconfigure(1, weight=1)  # Scrollable active downloads list gets full expansion

        input_frame = ctk.CTkFrame(tab_direct, fg_color="transparent")
        input_frame.grid(row=0, column=0, sticky="ew", padx=0, pady=(0, 10))  # Sits anchored at the very top!
        input_frame.grid_columnconfigure(0, weight=1)

        self.url_entry = ctk.CTkEntry(
            input_frame,
            placeholder_text="Paste URL here (driveseed / modpro / animeflix / leechpro)…",
            height=40,
            corner_radius=10,
            font=(FONT_FAMILY, 13),
            fg_color="#1a1a24",
            border_color="#2a2a3a",
        )
        self.url_entry.grid(row=0, column=0, sticky="ew", padx=(0, 8))
        self.url_entry.bind("<Return>", lambda e: self._on_go())

        self.go_btn = ctk.CTkButton(
            input_frame,
            text="Go",
            width=60,
            height=40,
            corner_radius=10,
            font=(FONT_FAMILY, 13, "bold"),
            fg_color=ACCENT,
            hover_color="#6a5ad4",
            command=self._on_go,
        )
        self.go_btn.grid(row=0, column=1)

        # ── Tab 2: Search Engine ──
        tab_search = self.tabview.tab("Search Engine")
        tab_search.grid_columnconfigure(0, weight=1)
        tab_search.grid_rowconfigure(1, weight=1)  # Search results list gets full expansion

        self.search_input_frame = ctk.CTkFrame(tab_search, fg_color="transparent")
        self.search_input_frame.grid(row=0, column=0, sticky="ew", padx=0, pady=(0, 8))
        self.search_input_frame.grid_columnconfigure(0, weight=1)

        self.search_entry = ctk.CTkEntry(
            self.search_input_frame,
            placeholder_text="Enter movie or show name (e.g. The Boys, Singham)...",
            height=40,
            corner_radius=10,
            font=(FONT_FAMILY, 13),
            fg_color="#1a1a24",
            border_color="#2a2a3a",
        )
        self.search_entry.grid(row=0, column=0, sticky="ew", padx=(0, 8))
        self.search_entry.bind("<Return>", lambda e: self._on_search())
        self.search_entry.bind("<KeyRelease>", self._on_key_release)

        self.cat_select = ctk.CTkComboBox(
            self.search_input_frame,
            values=["All", "Hollywood", "Bollywood", "Anime"],
            width=110,
            height=40,
            corner_radius=10,
            font=(FONT_FAMILY, 12),
            fg_color="#1a1a24",
            border_color="#2a2a3a",
            button_color="#2a2a3a",
            button_hover_color="#3a3a4a",
            state="readonly",
        )
        self.cat_select.set("All")
        self.cat_select.grid(row=0, column=1, padx=0)
        self.cat_select.configure(command=lambda val: self._on_search()) # instantly trigger search on category select too!

        self.search_scroll = ctk.CTkScrollableFrame(
            tab_search,
            fg_color="transparent",
            corner_radius=0,
            border_width=0,
        )
        self.search_scroll.grid(row=1, column=0, sticky="nsew", padx=0, pady=0)
        self.search_scroll.grid_columnconfigure(0, weight=1)

        # ── Integrated Same-Page Options Detail View ──
        self.search_detail_frame = ctk.CTkFrame(tab_search, fg_color="transparent")
        self.search_detail_frame.grid_columnconfigure(0, weight=1)
        self.search_detail_frame.grid_rowconfigure(1, weight=1) # scroll list gets remaining vertical space

        header_frame = ctk.CTkFrame(self.search_detail_frame, fg_color="transparent")
        header_frame.grid(row=0, column=0, sticky="ew", padx=4, pady=(4, 12))
        header_frame.grid_columnconfigure(1, weight=1)

        self.detail_back_btn = ctk.CTkButton(
            header_frame,
            text="← Back to Search",
            width=130,
            height=36,
            corner_radius=8,
            font=(FONT_FAMILY, 12, "bold"),
            fg_color="#1a1a24",
            border_color="#2a2a3a",
            border_width=1,
            text_color=SUBTEXT,
            hover_color="#2a2a3a",
            command=self._show_search_results,
        )
        self.detail_back_btn.grid(row=0, column=0, sticky="w", padx=(0, 12))

        self.detail_title_lbl = ctk.CTkLabel(
            header_frame,
            text="",
            font=(FONT_FAMILY, 14, "bold"),
            text_color="white",
            anchor="w",
            justify="left",
            wraplength=340,
        )
        self.detail_title_lbl.grid(row=0, column=1, sticky="w")

        self.detail_scroll = ctk.CTkScrollableFrame(
            self.search_detail_frame,
            fg_color="transparent",
            corner_radius=0,
            border_width=0,
        )
        self.detail_scroll.grid(row=1, column=0, sticky="nsew", padx=0, pady=0)
        self.detail_scroll.grid_columnconfigure(0, weight=1)

        # ── Scrollable download list (Decoupled, child of tab_direct) ──
        self.scroll_frame = ctk.CTkScrollableFrame(
            tab_direct,
            fg_color="transparent",
            corner_radius=0,
        )
        self.scroll_frame.grid(row=1, column=0, sticky="nsew", padx=0, pady=0)
        self.scroll_frame.grid_columnconfigure(0, weight=1)

        # ── Status bar with retry button (Decoupled, child of tab_direct) ──
        self.status_frame = ctk.CTkFrame(tab_direct, fg_color="transparent")
        self.status_frame.grid(row=2, column=0, sticky="ew", padx=4, pady=(6, 0))
        self.status_frame.grid_columnconfigure(0, weight=1)

        self.status_lbl = ctk.CTkLabel(
            self.status_frame,
            text="",
            font=(FONT_FAMILY, 11),
            text_color=SUBTEXT,
            anchor="w",
        )
        self.status_lbl.grid(row=0, column=0, sticky="w")

        self.retry_btn = ctk.CTkButton(
            self.status_frame,
            text="Retry Failed",
            width=100,
            height=26,
            corner_radius=8,
            font=(FONT_FAMILY, 11),
            fg_color="#b45309",
            hover_color="#d97706",
            command=self._retry_failed,
        )
        self.retry_btn.grid(row=0, column=2, padx=(8, 0))
        self.retry_btn.grid_remove()

        self.telegram_ready_lbl = ctk.CTkLabel(
            self.status_frame,
            text="",
            font=(FONT_FAMILY, 11),
            anchor="e",
        )
        self.telegram_ready_lbl.grid(row=0, column=1, sticky="e", padx=(8, 0))
        self._refresh_telegram_ready_status()

        self._bind_scroll_events()

    def _get_telegram_ready_status(self):
        if not HAS_TELEGRAM_FALLBACK:
            return "Telegram: Not Ready", RED

        api_id = os.getenv("TELEGRAM_API_ID", "").strip()
        api_hash = os.getenv("TELEGRAM_API_HASH", "").strip()
        if not api_id or not api_hash:
            return "Telegram: Not Ready", AMBER

        session_name = os.getenv("TELEGRAM_SESSION", "tgseed_session").strip() or "tgseed_session"
        if not session_file_exists(session_name):
            return "Telegram: Login Needed", AMBER

        return "Telegram: Ready", GREEN

    def _refresh_telegram_ready_status(self):
        text, color = self._get_telegram_ready_status()
        self.telegram_ready_lbl.configure(text=text, text_color=color)

    def _bind_scroll_events(self):
        """Bind hover-based scroll routing for all scrollable frames in the application."""
        self._bind_scroll_to_canvas(self.scroll_frame, self.scroll_frame._parent_canvas)
        self._bind_scroll_to_canvas(self.search_scroll, self.search_scroll._parent_canvas)
        self._bind_scroll_to_canvas(self.detail_scroll, self.detail_scroll._parent_canvas)

    def _bind_scroll_to_canvas(self, widget, canvas):
        """Recursively bind hover-enter/leave to a widget and its children to scroll the target canvas."""
        def _on_mousewheel(event):
            if not canvas or not canvas.winfo_exists():
                return
            if event.num == 4:
                canvas.yview_scroll(-3, "units")
            elif event.num == 5:
                canvas.yview_scroll(3, "units")
            elif event.delta:
                canvas.yview_scroll(-1 * (event.delta // 120), "units")

        def _enter(e):
            widget.bind_all("<Button-4>", _on_mousewheel)
            widget.bind_all("<Button-5>", _on_mousewheel)
            widget.bind_all("<MouseWheel>", _on_mousewheel)

        def _leave(e):
            widget.unbind_all("<Button-4>")
            widget.unbind_all("<Button-5>")
            widget.unbind_all("<MouseWheel>")

        def _bind_recursive(w):
            w.bind("<Enter>", _enter, add="+")
            w.bind("<Leave>", _leave, add="+")
            for child in w.winfo_children():
                _bind_recursive(child)

        _bind_recursive(widget)

    @staticmethod
    def _telegram_watch_dirs() -> list[Path]:
        dirs = []
        raw = os.getenv("TELEGRAM_DESKTOP_DOWNLOAD_DIR", "").strip()
        tg_debug(f"TELEGRAM_DESKTOP_DOWNLOAD_DIR raw='{raw}'")
        if raw:
            for part in re.split(r"[:,]", raw):
                p = part.strip()
                if p:
                    dirs.append(Path(p).expanduser())
        dirs.append(Path("~/Downloads/Telegram Desktop").expanduser())
        dirs.append(Path("~/Downloads").expanduser())
        dirs.append(
            Path(
                "~/.var/app/org.telegram.desktop/data/TelegramDesktop/tdata/temp_data"
            ).expanduser()
        )
        dirs.append(Path("~/.var/app/org.telegram.desktop/data/TelegramDesktop").expanduser())

        unique = []
        seen = set()
        for p in dirs:
            key = str(p)
            if key not in seen:
                seen.add(key)
                unique.append(p)
        tg_debug("Watch directories resolved: " + ", ".join(str(p) for p in unique))
        return unique

    @staticmethod
    def _snapshot_files(paths: list[Path]) -> dict[str, tuple[int, float]]:
        snap = {}
        seen = set()
        for base in paths:
            if not base.is_dir():
                tg_debug(f"Snapshot skip missing dir: {base}")
                continue
            scanned = 0
            try:
                for root, _, files in os.walk(base):
                    for name in files:
                        f = Path(root) / name
                        key = str(f.resolve())
                        if key in seen:
                            continue
                        seen.add(key)
                        try:
                            st = f.stat()
                        except Exception:
                            continue
                        snap[key] = (st.st_size, st.st_mtime)
                        scanned += 1
            except Exception:
                continue
            tg_debug(f"Snapshot scanned {scanned} file(s) in {base}")
        tg_debug(f"Snapshot baseline size: {len(snap)} file(s)")
        return snap

    @staticmethod
    def _pick_dest_path(target_dir: str, src_name: str, preferred_name: str | None = None) -> str:
        os.makedirs(target_dir, exist_ok=True)
        final_name = (preferred_name or "").strip() or src_name

        src_ext = os.path.splitext(src_name)[1]
        pref_base, pref_ext = os.path.splitext(final_name)
        if src_ext and not pref_ext:
            final_name = f"{pref_base}{src_ext}"

        base, ext = os.path.splitext(final_name)
        candidate = os.path.join(target_dir, final_name)
        n = 1
        while os.path.exists(candidate):
            candidate = os.path.join(target_dir, f"{base} ({n}){ext}")
            n += 1
        return candidate

    @staticmethod
    def _telegram_deep_link(tgseed_url: str) -> str:
        normalized_url = unescape(tgseed_url).strip()
        parsed = urlparse(normalized_url)
        qs = parse_qs(parsed.query)
        start_param = qs.get("start", [""])[0].strip()
        bot_name = qs.get("bot", [""])[0].lstrip("@").strip()
        if start_param and bot_name:
            return f"tg://resolve?domain={bot_name}&start={start_param}"
        return normalized_url

    @staticmethod
    def _is_telegram_running() -> bool:
        """Check if Telegram Desktop is currently running."""
        try:
            result = subprocess.run(
                ["pgrep", "-f", "-i", "telegram"],
                capture_output=True,
                timeout=2,
            )
            return result.returncode == 0
        except Exception:
            return False

    @classmethod
    def _open_telegram_with_link(cls, deep_link: str) -> None:
        """
        Open Telegram with protocol link.

        Cold-start flow (Telegram not running):
        1) Open Telegram app only
        2) Wait 5 seconds
        3) Send deep-link to running app (with retries)

        Running flow (Telegram already running):
        - Send deep-link directly.
        """
        if cls._is_telegram_running():
            tg_debug(f"[TG-Protocol] Telegram already running, sending link directly")
            result = subprocess.run(
                ["xdg-open", deep_link],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode != 0:
                stderr_msg = result.stderr.strip() if result.stderr else "No error details"
                raise RuntimeError(
                    f"Failed to send link to running Telegram (rc={result.returncode}): {stderr_msg}"
                )
            tg_debug(f"[TG-Protocol] Link sent successfully to running Telegram")
            return

        tg_debug("[TG-Protocol] Telegram not running, cold-start sequence begin")

        launch_ok = False
        launch_err = ""

        # Try native binary first.
        try:
            proc = subprocess.Popen(
                ["telegram-desktop"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            tg_debug(f"[TG-Protocol] Stage 1: launched telegram-desktop (pid={proc.pid})")
            launch_ok = True
        except FileNotFoundError:
            tg_debug("[TG-Protocol] Stage 1: telegram-desktop not in PATH")
        except Exception as exc:
            launch_err = str(exc)
            tg_debug(f"[TG-Protocol] Stage 1: telegram-desktop launch failed: {exc}")

        # Flatpak fallback.
        if not launch_ok:
            try:
                proc = subprocess.Popen(
                    ["flatpak", "run", "org.telegram.desktop"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
                tg_debug(f"[TG-Protocol] Stage 1: launched flatpak Telegram (pid={proc.pid})")
                launch_ok = True
            except FileNotFoundError:
                tg_debug("[TG-Protocol] Stage 1: flatpak not found")
            except Exception as exc:
                launch_err = str(exc)
                tg_debug(f"[TG-Protocol] Stage 1: flatpak launch failed: {exc}")

        # Protocol bootstrap fallback.
        if not launch_ok:
            result = subprocess.run(
                ["xdg-open", "tg://"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode == 0:
                launch_ok = True
                tg_debug("[TG-Protocol] Stage 1: xdg-open tg:// launched Telegram")
            else:
                stderr_msg = result.stderr.strip() if result.stderr else "No error details"
                launch_err = stderr_msg

        if not launch_ok:
            raise RuntimeError(
                f"Failed to launch Telegram Desktop. {launch_err or 'No launcher worked.'}"
            )

        # User-requested behavior: open app first, then wait 5s, then send link.
        try:
            cold_start_delay = float(os.getenv("TELEGRAM_COLD_START_DELAY", "5").strip())
        except ValueError:
            cold_start_delay = 5.0
        cold_start_delay = min(30.0, max(1.0, cold_start_delay))

        tg_debug(
            f"[TG-Protocol] Stage 2: waiting {cold_start_delay:.1f}s before sending bot start link"
        )
        time.sleep(cold_start_delay)

        # Send link with retries; startup can be slow on cold boot.
        last_err = ""
        for attempt in range(1, 4):
            result = subprocess.run(
                ["xdg-open", deep_link],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode == 0:
                tg_debug(f"[TG-Protocol] Stage 3: link sent on attempt {attempt}")
                return

            last_err = result.stderr.strip() if result.stderr else "No error details"
            tg_debug(
                f"[TG-Protocol] Stage 3: attempt {attempt} failed (rc={result.returncode}): {last_err}"
            )
            if attempt < 3:
                time.sleep(2)

        raise RuntimeError(
            f"Telegram opened but failed to send bot start link after retries: {last_err}"
        )

    def _watch_for_telegram_file(
        self,
        watch_dirs: list[Path],
        baseline: dict[str, tuple[int, float]],
        watch_timeout: int | None,
        started_at: float,
        expected_name: str | None = None,
        expected_size_bytes: int | None = None,
        on_progress=None,
    ) -> Path | None:
        # Default is no timeout (watch until found). If timeout is set, enforce a 30s minimum.
        effective_timeout = None
        if watch_timeout is not None and watch_timeout > 0:
            effective_timeout = max(30, watch_timeout)
            if effective_timeout > watch_timeout:
                tg_debug(
                    f"Watcher: configured timeout {watch_timeout}s is low, using minimum {effective_timeout}s"
                )

        deadline = time.time() + effective_timeout if effective_timeout is not None else None
        tg_debug(
            "Watcher timing | "
            f"start={started_at:.0f} "
            f"deadline={'none' if deadline is None else f'{deadline:.0f}'} "
            f"timeout={'disabled' if effective_timeout is None else f'{effective_timeout}s'}"
        )
        
        stable = {}
        temp_suffixes = (
            ".part",
            ".tmp",
            ".download",
            ".crdownload",
            ".td",
            ".partial",
            ".filepart",
        )
        expected_lc = (expected_name or "").strip().lower()
        expected_ext = os.path.splitext(expected_lc)[1]
        episode_tokens = re.findall(r"s\d{1,2}e\d{1,2}", expected_lc)
        expected_size = expected_size_bytes if expected_size_bytes and expected_size_bytes > 0 else None
        try:
            min_pick_mb = float(os.getenv("TELEGRAM_MIN_PICK_MB", "5").strip())
        except ValueError:
            min_pick_mb = 5.0
        min_pick_bytes = int(max(0.5, min_pick_mb) * 1024 * 1024)
        try:
            expected_ratio = float(os.getenv("TELEGRAM_EXPECTED_SIZE_RATIO", "0.95").strip())
        except ValueError:
            expected_ratio = 0.95
        expected_ratio = min(1.0, max(0.5, expected_ratio))
        try:
            stability_near_mb = float(os.getenv("TELEGRAM_STABILITY_NEAR_MB", "10").strip())
        except ValueError:
            stability_near_mb = 10.0
        stability_near_bytes = int(max(1.0, stability_near_mb) * 1000 * 1000)
        try:
            stable_seconds = int(os.getenv("TELEGRAM_STABLE_SECONDS", "15").strip())
        except ValueError:
            stable_seconds = 15
        stable_seconds = max(3, min(300, stable_seconds))
        tg_debug(
            "Watcher start | "
            f"expected='{expected_name}' expected_ext='{expected_ext}' "
            f"episode_tokens={episode_tokens} "
            f"expected_size={fmt_bytes(expected_size)} expected_ratio={expected_ratio} "
            f"stability_near_mb={stability_near_mb} "
            f"timeout={'disabled' if effective_timeout is None else f'{effective_timeout}s'} "
            f"min_pick_mb={min_pick_mb} "
            f"stable_seconds={stable_seconds}s started_at={started_at:.0f}"
        )

        poll = 0

        while True:
            if deadline is not None and time.time() >= deadline:
                break
            poll += 1
            poll_now = time.time()
            ready_candidates = []
            best_preview = None
            scanned_total = 0
            considered_total = 0
            stable_ready_total = 0
            episode_match_total = 0
            seen_files_this_poll = set()
            for base in watch_dirs:
                if not base.is_dir():
                    tg_debug(f"Poll {poll}: skip missing watch dir {base}")
                    continue
                try:
                    files = []
                    for root, _, names in os.walk(base):
                        for name in names:
                            files.append(Path(root) / name)
                except Exception:
                    tg_debug(f"Poll {poll}: failed walk for {base}")
                    continue

                tg_debug(f"Poll {poll}: scanned root {base} with {len(files)} file(s)")

                for f in files:
                    key = str(f.resolve())
                    if key in seen_files_this_poll:
                        continue
                    seen_files_this_poll.add(key)
                    scanned_total += 1
                    if not f.is_file() or f.name.lower().endswith(temp_suffixes):
                        if expected_lc and expected_lc in f.name.lower():
                            tg_debug(
                                f"Poll {poll}: expected-like file skipped as temp/non-file: {f.name}"
                            )
                        continue
                    try:
                        st = f.stat()
                    except Exception:
                        continue

                    name_lc = f.name.lower()
                    name_match_exact = bool(expected_lc and name_lc == expected_lc)
                    name_match_contains = bool(expected_lc and expected_lc in name_lc)
                    ext_match = bool(expected_ext and name_lc.endswith(expected_ext))
                    episode_match = bool(episode_tokens and any(t in name_lc for t in episode_tokens))
                    if episode_match:
                        episode_match_total += 1

                    if name_match_exact:
                        name_rank = 4
                    elif name_match_contains:
                        name_rank = 3
                    elif episode_match and ext_match:
                        name_rank = 2
                    elif episode_match or ext_match:
                        name_rank = 1
                    else:
                        name_rank = 0

                    old = baseline.get(key)
                    changed_since_baseline = not (
                        old and st.st_size == old[0] and st.st_mtime <= old[1] + 0.5
                    )
                    is_recent = st.st_mtime >= started_at - 900
                    # If exact expected filename exists, allow it through even if
                    # unchanged versus baseline. Some Telegram builds finalize very fast
                    # and can look unchanged by the time polling catches up.
                    if not (
                        changed_since_baseline
                        or is_recent
                        or name_match_exact
                        or name_match_contains
                        or episode_match
                    ):
                        if name_match_exact or name_match_contains:
                            tg_debug(
                                "Poll "
                                f"{poll}: expected-like ignored by freshness gate: {f.name} "
                                f"size={st.st_size} mtime={st.st_mtime:.0f}"
                            )
                        continue

                    considered_total += 1

                    prev = stable.get(key)
                    if prev and prev[0] == st.st_size and prev[1] == st.st_mtime:
                        last_change_at = prev[2]
                        first_seen_at = prev[3]
                    else:
                        last_change_at = poll_now
                        first_seen_at = prev[3] if prev else poll_now

                    stable[key] = (st.st_size, st.st_mtime, last_change_at, first_seen_at)
                    quiet_for = poll_now - last_change_at

                    near_limit_reached = True
                    if expected_size is not None:
                        near_threshold = max(0, expected_size - stability_near_bytes)
                        near_limit_reached = st.st_size >= near_threshold

                    preview_key = (name_rank, st.st_size)
                    if best_preview is None or preview_key > (best_preview[0], best_preview[1]):
                        best_preview = (name_rank, st.st_size, quiet_for, f)

                    if not near_limit_reached:
                        if name_match_exact or name_match_contains or episode_match or ext_match:
                            tg_debug(
                                f"Poll {poll}: candidate not near expected limit yet {f.name} "
                                f"size={st.st_size} < near_threshold={near_threshold}"
                            )
                        continue

                    if quiet_for >= stable_seconds and st.st_size > 0:
                        if expected_size is not None:
                            min_expected_bytes = int(expected_size * expected_ratio)
                            if st.st_size < min_expected_bytes:
                                if name_match_exact or name_match_contains or episode_match or ext_match:
                                    tg_debug(
                                        f"Poll {poll}: candidate below expected size {f.name} "
                                        f"size={st.st_size} < min_expected={min_expected_bytes} "
                                        f"({expected_ratio:.2f} x {expected_size})"
                                    )
                                continue

                        # Avoid selecting tiny Telegram side-files unless exact name matches.
                        if st.st_size < min_pick_bytes and not name_match_exact:
                            if name_match_exact or name_match_contains or episode_match or ext_match:
                                tg_debug(
                                    f"Poll {poll}: reject small candidate {f.name} "
                                    f"size={st.st_size} < min={min_pick_bytes}"
                                )
                            continue

                        # Prefer files whose name resembles expected filename.
                        ready_candidates.append(
                            (
                                name_rank,
                                st.st_size,
                                st.st_mtime,
                                f,
                            )
                        )
                        stable_ready_total += 1
                        if name_match_exact or name_match_contains or episode_match or ext_match:
                            tg_debug(
                                f"Poll {poll}: ready candidate {f.name} "
                                f"rank={name_rank} size={st.st_size} quiet_for={quiet_for:.1f}s"
                            )
                    else:
                        if name_match_exact or name_match_contains or episode_match or ext_match:
                            tg_debug(
                                f"Poll {poll}: candidate not stable yet {f.name} "
                                f"quiet_for={quiet_for:.1f}s/{stable_seconds}s"
                            )

            if on_progress:
                if best_preview:
                    _, lsize, quiet_for, lf = best_preview
                    if expected_size:
                        pct = min(1.0, lsize / expected_size)
                        on_progress(
                            "Watching… "
                            f"{fmt_bytes(lsize)} / {fmt_bytes(expected_size)} ({pct * 100:.1f}%), "
                            f"quiet {quiet_for:.0f}/{stable_seconds}s | last: {lf.name}",
                            pct,
                        )
                    else:
                        on_progress(
                            "Watching… "
                            f"last: {lf.name} ({fmt_bytes(lsize)}, quiet {quiet_for:.0f}/{stable_seconds}s)",
                            None,
                        )
                else:
                    on_progress(
                        "Watching… no candidates yet",
                        None,
                    )

            tg_debug(
                f"Poll {poll} summary: scanned={scanned_total} considered={considered_total} "
                f"episode_matches={episode_match_total} ready={stable_ready_total}"
            )

            if ready_candidates:
                ready_candidates.sort(key=lambda x: (x[0], x[1], x[2]), reverse=True)
                picked = ready_candidates[0][3]
                tg_debug(
                    "Picked candidate: "
                    f"{picked} rank={ready_candidates[0][0]} size={ready_candidates[0][1]} "
                    f"mtime={ready_candidates[0][2]:.0f}"
                )
                return picked

            time.sleep(1)

        if deadline is None:
            tg_debug("Watcher exited with no timeout and no candidate (unexpected)")
        else:
            tg_debug("Watcher timeout reached with no candidate")
        return None

    def _prepare_telegram_manual_card(self, idx, item):
        if idx >= len(self.cards):
            return
        card = self.cards[idx]
        card.name_lbl.configure(text=item["filename"])
        card.set_method(item.get("method", "TELEGRAM"))
        card.set_status("Manual Telegram", AMBER)
        expected_size = item.get("expected_size_bytes")
        if expected_size:
            card.set_detail(
                f"Click Download to open Telegram Desktop. Expected: {fmt_bytes(expected_size)}"
            )
        else:
            card.set_detail("Click Download to open Telegram Desktop.")
        card.item_data = item
        card.set_action("Download", lambda i=idx: self._start_telegram_manual_download(i, is_retry=False))

    @staticmethod
    def _sanitize_filename(name: str) -> str:
        return re.sub(r'[<>:"/\\|?*]', "_", (name or "").strip())

    def _refresh_telegram_item_for_click(self, idx, item):
        """
        Re-resolve a Telegram item at click time to get a fresh tgseed token.
        This is called INSIDE the telegram_lock to prevent race conditions.
        
        OPTIMIZATION: Skip refresh if token was refreshed < 1 minute ago (tokens don't expire that fast)
        """
        current_time = time.time()
        last_refresh_time = item.get("_token_refresh_time", 0)
        
        # If refreshed recently (< 60 seconds ago), skip refresh
        if current_time - last_refresh_time < 60:
            tg_debug(f"[Telegram Refresh] Token still fresh (refreshed {current_time - last_refresh_time:.0f}s ago), skipping refresh")
            return item
        
        source_link = item.get("source_link")
        source_index = item.get("source_index", idx)
        source_name = item.get("source_name_hint") or item.get("filename") or ""
        source_driveseed_url = item.get("source_driveseed_url")

        tg_debug(f"[Telegram Refresh] idx={idx} source_link={bool(source_link)} driveseed={bool(source_driveseed_url)}")

        resolved_url = None
        if source_link:
            try:
                _, fresh_name, resolved_url = resolve_link(source_index, source_link, session=SESSION)
                if fresh_name:
                    source_name = fresh_name
                    item["source_name_hint"] = fresh_name
                tg_debug(f"[Telegram Refresh] Resolved link: {resolved_url[:80] if resolved_url else 'None'}...")
            except Exception as e:
                tg_debug(f"[Telegram Refresh] resolve_link failed: {e}")
                resolved_url = None
        elif source_driveseed_url:
            resolved_url = source_driveseed_url
            tg_debug(f"[Telegram Refresh] Using cached driveseed URL")
        else:
            resolved_url = item.get("download_url", "")
            tg_debug(f"[Telegram Refresh] Using cached download_url")

        if not resolved_url:
            raise RuntimeError(
                "No source link or download URL available for refresh. "
                "Episode may have been resolved improperly."
            )

        if resolved_url and "driveseed.org" in resolved_url:
            item["source_driveseed_url"] = resolved_url
            try:
                meta_fname, meta_size = get_driveseed_file_metadata(resolved_url)
                dl_url, dl_fname, method = get_driveseed_download_url(resolved_url)
                tg_debug(
                    f"[Telegram Refresh] DriveSeed: method={method} size={fmt_bytes(meta_size)} "
                    f"url_prefix={dl_url[:50] if dl_url else 'None'}..."
                )

                if method != "TELEGRAM" and "tgseed.link" not in dl_url:
                    raise RuntimeError(
                        f"Episode resolved to {method}, not Telegram. "
                        f"Try another Telegram link if available."
                    )

                item["download_url"] = dl_url

                fresh_name = dl_fname or meta_fname
                if fresh_name:
                    item["filename"] = self._sanitize_filename(fresh_name)

                if meta_size:
                    item["expected_size_bytes"] = meta_size
                elif not item.get("expected_size_bytes"):
                    item["expected_size_bytes"] = parse_size_hint_bytes(source_name)
            except Exception as e:
                tg_debug(f"[Telegram Refresh] DriveSeed metadata fetch failed: {e}")
                # Don't fail the entire refresh, just log it
                # The old download_url might still work
                if not item.get("download_url"):
                    raise

        # Mark refresh time
        item["_token_refresh_time"] = current_time
        tg_debug(f"[Telegram Refresh] Token refresh complete, marked at {current_time:.0f}")
        return item

        if resolved_url and "tgseed.link" in resolved_url:
            item["download_url"] = resolved_url
            if not item.get("expected_size_bytes"):
                item["expected_size_bytes"] = parse_size_hint_bytes(source_name)

            if source_name and item.get("filename") in (None, "", "Telegram file"):
                item["filename"] = self._sanitize_filename(source_name)

            tg_debug(
                f"Refresh[{idx}] tgseed expected_size={fmt_bytes(item.get('expected_size_bytes'))}"
            )
            return item

        raise RuntimeError("Unable to refresh Telegram token for this episode")

    def _cleanup_telegram_retry_artifacts(self, item) -> int:
        """Remove likely partial files before retrying a Telegram manual download."""
        removed = 0
        expected_name = self._sanitize_filename(item.get("filename", ""))
        expected_lc = expected_name.lower()
        expected_size = item.get("expected_size_bytes")
        safe_complete_ratio = 0.98
        temp_suffixes = (
            ".part",
            ".tmp",
            ".download",
            ".crdownload",
            ".td",
            ".partial",
            ".filepart",
        )

        def _is_incomplete_file(path_obj: Path) -> bool:
            if not path_obj.exists() or not path_obj.is_file():
                return False
            name_lc = path_obj.name.lower()
            if name_lc.endswith(temp_suffixes):
                return True

            if expected_size and expected_size > 0:
                try:
                    return path_obj.stat().st_size < int(expected_size * safe_complete_ratio)
                except Exception:
                    return False

            return False

        candidates = []
        seen = set()

        def _add_candidate(path_obj: Path):
            key = str(path_obj)
            if key not in seen:
                seen.add(key)
                candidates.append(path_obj)

        target_dir = Path(item.get("target_dir", self.output_dir))
        if expected_name:
            _add_candidate(target_dir / expected_name)
            _add_candidate(target_dir / f"{expected_name}.part")

        for key in ("last_source_file", "last_moved_path"):
            p = item.get(key)
            if p:
                _add_candidate(Path(p))

        if expected_name:
            stem_lc = Path(expected_name).stem.lower()
            ext_lc = Path(expected_name).suffix.lower()
            for base in self._telegram_watch_dirs():
                if not base.is_dir():
                    continue
                for root, _, files in os.walk(base):
                    for fname in files:
                        fname_lc = fname.lower()
                        match = False
                        if fname_lc == expected_lc:
                            match = True
                        elif stem_lc and stem_lc in fname_lc and (
                            fname_lc.endswith(temp_suffixes)
                            or (ext_lc and fname_lc.endswith(ext_lc))
                        ):
                            match = True
                        if match:
                            _add_candidate(Path(root) / fname)

        for path_obj in candidates:
            try:
                if _is_incomplete_file(path_obj):
                    path_obj.unlink()
                    removed += 1
                    tg_debug(f"Retry cleanup removed: {path_obj}")
            except Exception as exc:
                tg_debug(f"Retry cleanup failed for {path_obj}: {exc}")

        return removed

    def _start_telegram_manual_download(self, idx, is_retry=False):
        if idx >= len(self.cards):
            return
        card = self.cards[idx]
        item = card.item_data or {}
        btn_text = card.action_btn.cget("text")
        if btn_text in ("Waiting…", "Watching…", "Retrying…"):
            tg_debug(f"[Telegram] Ignoring click on idx={idx}, button is already {btn_text}")
            return

        tg_debug(f"[Telegram] Click start: idx={idx}, is_retry={is_retry}, btn={btn_text}")
        
        if is_retry:
            removed = self._cleanup_telegram_retry_artifacts(item)
            card.set_detail(f"Retrying… cleaned {removed} partial file(s).")
            card.set_progress(0)
            card.set_action("Retrying…", lambda: None, state="disabled")
            tg_debug(f"[Telegram] Retry: cleaned {removed} files for idx={idx}")
        else:
            card.set_action("Waiting…", lambda: None, state="disabled")

        item["attempted_manual"] = True
        threading.Thread(
            target=self._run_telegram_manual_download,
            args=(idx, item, is_retry),
            daemon=True,
        ).start()

    def _run_telegram_manual_download(self, idx, item, is_retry=False):
        card = self.cards[idx]
        target_dir = item.get("target_dir", self.output_dir)
        watch_timeout_raw = os.getenv("TELEGRAM_WATCH_TIMEOUT", "").strip()
        if not watch_timeout_raw:
            watch_timeout = 0
        else:
            try:
                watch_timeout = max(0, int(watch_timeout_raw))
            except ValueError:
                watch_timeout = 0

        with self._lock:
            self._active_threads += 1
        self.after(0, lambda: self._refresh_status())

        try:
            watch_dirs = self._telegram_watch_dirs()
            existing_watch_dirs = [p for p in watch_dirs if p.is_dir()]
            if not existing_watch_dirs:
                raise FileNotFoundError(
                    "No Telegram Desktop download folder found. Set TELEGRAM_DESKTOP_DOWNLOAD_DIR in .env"
                )

            baseline = self._snapshot_files(watch_dirs)
            started_at = time.time()
            
            # == CRITICAL FIX: Do ALL refresh and setup inside the lock ==
            # This prevents race conditions when user clicks multiple Telegram episodes
            
            if self._telegram_lock.locked():
                self.after(
                    0,
                    lambda c=card: c.set_detail(
                        "Waiting for previous Telegram request to finish…"
                    ),
                )

            with self._telegram_lock:
                tg_debug(f"[Telegram Lock] Acquired for idx={idx}")
                
                # Show "Refreshing token..." status to user
                self.after(
                    0,
                    lambda c=card: (
                        c.set_status("Refreshing token…", AMBER),
                        c.set_action("Refreshing…", lambda: None, state="disabled"),
                    ),
                )
                
                # Make a LOCAL call to refresh to get fresh token + metadata
                # but ONLY store it in local variables, not shared item dict yet
                local_item = self._refresh_telegram_item_for_click(idx, item)
                
                url = local_item.get("download_url", "")
                expected_size = local_item.get("expected_size_bytes")
                filename = local_item.get("filename", "Telegram file")
                
                tg_debug(
                    "Manual Telegram download started | "
                    f"url={url[:100]}... filename={filename} target_dir={target_dir} "
                    f"expected_size={fmt_bytes(expected_size)} "
                    f"watch_timeout={'disabled' if watch_timeout == 0 else f'{watch_timeout}s'}"
                )
                
                # Validate deep_link before proceeding
                if not url or not url.strip():
                    raise RuntimeError("Failed to get valid download URL for Telegram episode")
                
                deep_link = self._telegram_deep_link(url)
                if not deep_link or not deep_link.strip():
                    raise RuntimeError(
                        f"Failed to convert URL to valid Telegram deep link: {url[:50]}"
                    )
                
                tg_debug(f"[Telegram Lock] Generated deep_link: {deep_link}")
                
                # Now update the SHARED item dict with confirmed working values
                card.item_data = local_item
                
                self.after(
                    0,
                    lambda c=card, n=filename: (
                        c.set_status("Open Telegram", ACCENT2),
                        c.name_lbl.configure(text=n),
                        c.set_progress(0),
                        c.set_detail(
                            "Telegram Desktop opened."
                            + (f" Expected size: {fmt_bytes(expected_size)}." if expected_size else "")
                            + " | Watching: "
                            + ", ".join(str(p) for p in existing_watch_dirs[:2])
                        ),
                        c.set_action("Watching…", lambda: None, state="disabled"),
                    ),
                )
                
                # Call xdg-open with 2-stage approach if Telegram not running
                try:
                    self._open_telegram_with_link(deep_link)
                    tg_debug(f"[Telegram Lock] Telegram opened and link sent: {deep_link}")
                except RuntimeError as e:
                    tg_debug(f"[Telegram Lock] Telegram open FAILED: {e}")
                    raise
                
                # Store resolved values for retry if needed
                final_filename = local_item.get("filename", "Telegram file")
                final_expected_size = local_item.get("expected_size_bytes")
            
            # == END OF CRITICAL LOCK SECTION ==
            # Now the lock is released, and we wait for the file to appear
            
            src_file = self._watch_for_telegram_file(
                watch_dirs,
                baseline,
                watch_timeout,
                started_at,
                expected_name=final_filename,
                expected_size_bytes=final_expected_size,
                on_progress=lambda text, prog: self.after(
                    0,
                    lambda c=card, t=text, p=prog: (
                        c.set_detail(t),
                        c.set_progress(p if p is not None else 0),
                    ),
                ),
            )
            if not src_file:
                raise TimeoutError(
                    "Timed out waiting for Telegram Desktop download"
                    if watch_timeout > 0
                    else "Watcher stopped without finding a Telegram download"
                )

            tg_debug(f"Source file detected for move: {src_file}")
            item["last_source_file"] = str(src_file)

            dest = self._pick_dest_path(
                target_dir,
                src_file.name,
                preferred_name=final_filename,
            )
            shutil.move(str(src_file), dest)
            item["last_moved_path"] = dest
            tg_debug(f"Moved Telegram file to: {dest}")

            moved_size_mb = os.path.getsize(dest) / (1024 * 1024)
            with self._lock:
                self._done_count += 1
            self.after(
                0,
                lambda c=card, s=moved_size_mb, d=dest: (
                    c.mark_done(),
                    c.set_detail(f"Moved from Telegram Desktop ({s:.1f} MB)"),
                    c.name_lbl.configure(text=os.path.basename(d)),
                ),
            )
        except Exception as e:
            error_str = str(e)
            tg_debug(f"[Telegram Download] FAILED: {error_str}")
            
            # Provide more helpful error messages based on error type
            if "Telegram Desktop" in error_str or "xdg-open" in error_str:
                detailed_err = (
                    f"⚠ Telegram Issue: {error_str}\n\n"
                    f"Troubleshooting:\n"
                    f"• Ensure Telegram Desktop is installed\n"
                    f"• Check you have tgseed: protocol handler configured\n"
                    f"• Try manually opening Telegram and retrying"
                )
            elif "Timed out" in error_str or "stopped without finding" in error_str:
                detailed_err = (
                    f"⚠ Download Timeout: {error_str}\n\n"
                    f"Troubleshooting:\n"
                    f"• Check Telegram Desktop downloaded the file\n"
                    f"• File may still be downloading - wait longer\n"
                    f"• Check watched folders: {', '.join(str(p) for p in watch_dirs[:2])}\n"
                    f"• Increase TELEGRAM_WATCH_TIMEOUT in .env if needed"
                )
            else:
                detailed_err = error_str
            
            with self._lock:
                self._fail_count += 1
            self.after(
                0,
                lambda c=card, err=detailed_err: (
                    c.mark_failed(err),
                    c.set_action("Retry", lambda i=idx: self._start_telegram_manual_download(i, is_retry=True)),
                ),
            )
        finally:
            with self._lock:
                self._active_threads -= 1
            self.after(0, lambda: self._refresh_status())

    @staticmethod
    def _ask_directory(initial_dir):
        """Open native file manager to pick a folder."""
        try:
            result = subprocess.run(
                [
                    "zenity",
                    "--file-selection",
                    "--directory",
                    "--title=Select download folder",
                    f"--filename={initial_dir}/",
                ],
                capture_output=True,
                text=True,
                timeout=120,
            )
            if result.returncode == 0:
                return result.stdout.strip()
        except FileNotFoundError:
            try:
                result = subprocess.run(
                    ["kdialog", "--getexistingdirectory", initial_dir],
                    capture_output=True,
                    text=True,
                    timeout=120,
                )
                if result.returncode == 0:
                    return result.stdout.strip()
            except FileNotFoundError:
                pass
        return None

    def _on_go(self):
        url = self.url_entry.get().strip()
        if not url:
            return

        self.url_entry.delete(0, "end")
        self.go_btn.configure(state="disabled")

        self._start_download_pipeline(url)

    def _start_download_pipeline(self, url):
        # Prompt user where to save the files!
        path = self._ask_directory(self.output_dir)
        if not path:
            # User cancelled, abort the download!
            self._update_status("Download cancelled (no folder selected).")
            self._enable_go()
            return
            
        # Update self.output_dir to the chosen directory
        self.output_dir = path

        # Reset counters
        with self._lock:
            self._done_count = 0
            self._fail_count = 0
            self._total_count = 0
            self._active_threads = 0

        for card in self.cards:
            card.destroy()
        self.cards.clear()

        # Empty the queue
        while not self.download_queue.empty():
            try:
                self.download_queue.get_nowait()
            except Empty:
                break

        # Pre-warm DNS before resolving
        threading.Thread(target=prewarm_dns, daemon=True).start()

        threading.Thread(
            target=self._resolve_pipeline, args=(url,), daemon=True
        ).start()

    # ── Search Engine Methods ───────────────────────────────────────────

    def _on_key_release(self, event):
        # Debounce key release for instant search
        if hasattr(self, "_search_timer") and self._search_timer:
            self.after_cancel(self._search_timer)
            self._search_timer = None

        query = self.search_entry.get().strip()
        if len(query) >= 2:
            self._search_timer = self.after(500, self._on_debounce_search)

    def _on_debounce_search(self):
        self._search_timer = None
        query = self.search_entry.get().strip()
        category = self.cat_select.get()
        
        state = (query, category)
        if hasattr(self, "_last_search_state") and self._last_search_state == state:
            return  # Query is identical, abort redundant search (prevents window focus triggering)
            
        self._last_search_state = state
        self._on_search()

    def _start_modlist_poller(self):
        def _poll():
            print("[*] Launching modlist background domain poller...")
            while True:
                try:
                    from movie_search import resolve_search_domains
                    resolve_search_domains(force_refresh=True)
                except Exception as e:
                    print(f"[-] Modlist poller error: {e}")
                time.sleep(30)
                
        threading.Thread(target=_poll, daemon=True).start()



    def _get_search_sort_key(self, item, query):
        """Calculate relevance and priority sort key for an individual search card."""
        title = item.get("title", "").lower()
        q = query.lower()
        
        # 1. Relevance Score (lower is better/more relevant)
        if title == q:
            relevance = 0  # Exact match
        elif title.startswith(q + " ") or title.startswith(q + "{") or title.startswith(q + "("):
            relevance = 1  # Starts with query term (with separator/brackets)
        elif title.startswith(q):
            relevance = 2  # Starts with query substring
        elif f" {q} " in f" {title} " or title.endswith(" " + q):
            relevance = 3  # Full query word match in title
        elif q in title:
            relevance = 4  # Substring match
        else:
            relevance = 5  # Loose fallback match

        # 2. Category Priority (lower is better)
        cat = item.get("category", "").upper()
        cat_priority = {
            "HOLLYWOOD": 0,
            "BOLLYWOOD": 1,
            "ANIMEFLIX": 2,
            "ANIME": 2
        }.get(cat, 3)

        return (relevance, cat_priority, title)

    def _on_search(self):
        query = self.search_entry.get().strip()
        if not query:
            return

        # Initialize search cards list
        self.search_cards = []

        # Clear previous results
        for widget in self.search_scroll.winfo_children():
            widget.destroy()

        # Add search status label
        self.search_status = ctk.CTkLabel(
            self.search_scroll,
            text="Searching on ModList domains...",
            font=(FONT_FAMILY, 12, "italic"),
            text_color=SUBTEXT,
        )
        self.search_status.pack(pady=40)

        # Spawn background thread for searching
        category = self.cat_select.get()
        self._last_search_state = (query, category)
        threading.Thread(
            target=self._async_search_worker,
            args=(query, category),
            daemon=True,
        ).start()

    def _async_search_worker(self, query, category):
        # Prioritize hollywood searches first by placing it first in the list
        cats = ["hollywood", "bollywood", "animeflix"]
        if category != "All":
            cats = [category.lower()]
            if category.lower() == "anime":
                cats = ["animeflix"]

        active_searches = len(cats)
        lock = threading.Lock()

        # Callback triggered immediately when an individual article is parsed
        def render_single_item(item):
            # Clear search loading status label if still present
            if hasattr(self, 'search_status') and self.search_status.winfo_exists():
                self.search_status.destroy()
            
            # Create card
            card = SearchResultCard(self.search_scroll, item, self._on_search_card_click)
            self.search_cards.append(card)
            
            # Sort active cards list by relevance and category priority in real time
            self.search_cards.sort(key=lambda c: self._get_search_sort_key(c.item, query))
            
            # Re-position only the cards whose row has actually changed (completely eradicates micro-flickering!)
            for idx, c in enumerate(self.search_cards):
                if not hasattr(c, "current_row") or c.current_row != idx:
                    c.grid(row=idx, column=0, sticky="ew", padx=10, pady=6)
                    c.current_row = idx

        def search_cat_thread(cat):
            nonlocal active_searches
            try:
                # Query this specific category individually, streaming results
                from movie_search import search_movies
                search_movies(query, [cat], on_result_callback=render_single_item)
                
                def check_completion():
                    # Update search counter safely
                    with lock:
                        nonlocal active_searches
                        active_searches -= 1
                        if active_searches == 0:
                            # Remove loading label if no results found
                            if len(self.search_scroll.winfo_children()) == 0:
                                if hasattr(self, 'search_status') and self.search_status.winfo_exists():
                                    self.search_status.destroy()
                                self.search_status = ctk.CTkLabel(
                                    self.search_scroll,
                                    text=f"No results found for '{query}'. Try a different name.",
                                    font=(FONT_FAMILY, 12, "bold"),
                                    text_color=SUBTEXT,
                                )
                                self.search_status.pack(pady=40)
                self.after(0, check_completion)
            except Exception as e:
                print(f"[-] Category search failed: {cat} - {e}")
                with lock:
                    active_searches -= 1
                    if active_searches == 0:
                        def finish_controls():
                            if len(self.search_scroll.winfo_children()) == 0:
                                if hasattr(self, 'search_status') and self.search_status.winfo_exists():
                                    self.search_status.destroy()
                                self.search_status = ctk.CTkLabel(
                                    self.search_scroll,
                                    text=f"No results found or search failed: {e}",
                                    font=(FONT_FAMILY, 12, "bold"),
                                    text_color=RED,
                                )
                                self.search_status.pack(pady=40)
                        self.after(0, finish_controls)

        # Launch independent concurrent thread for each category (Hollywood is launched first!)
        for cat in cats:
            threading.Thread(target=search_cat_thread, args=(cat,), daemon=True).start()

    def _on_search_card_click(self, item):
        self._update_status(f"Fetching download options for '{item['title']}'...")
        
        threading.Thread(
            target=self._async_options_worker,
            args=(item,),
            daemon=True,
        ).start()

    def _async_options_worker(self, item):
        try:
            options = extract_download_options(item["url"])
            
            def show_options_inline():
                self._update_status("")
                if not options:
                    self._update_status("No download options/qualities found on page")
                    return
                self._show_movie_detail(item, options)

            self.after(0, show_options_inline)
        except Exception as e:
            self.after(0, lambda: self._update_status(f"Error fetching options: {e}"))

    def _show_search_results(self):
        # Hide detail frame
        self.search_detail_frame.grid_forget()
        
        # Show search input and scroll list
        self.search_input_frame.grid(row=0, column=0, sticky="ew", padx=16, pady=(16, 8))
        self.search_scroll.grid(row=1, column=0, sticky="nsew", padx=0, pady=0)

    def _show_movie_detail(self, movie_item, options):
        # Hide search input and scroll list
        self.search_input_frame.grid_forget()
        self.search_scroll.grid_forget()
        
        # Update detail title
        self.detail_title_lbl.configure(text=movie_item["title"])
        
        # Clear previous detail items
        for w in self.detail_scroll.winfo_children():
            w.destroy()
            
        # Bind scroll events to new container
        if hasattr(self, '_bind_scroll_events_to_widget'):
            self._bind_scroll_events_to_widget(self.detail_scroll, self.detail_scroll._parent_canvas)
            
        # Render the options dynamically
        for opt in options:
            opt_frame = ctk.CTkFrame(
                self.detail_scroll,
                fg_color="#1a1a24",
                corner_radius=8,
                border_width=1,
                border_color="#2a2a3a"
            )
            opt_frame.pack(fill="x", pady=5, ipady=6, padx=10)
            opt_frame.grid_columnconfigure(0, weight=1)
            
            lbl = ctk.CTkLabel(
                opt_frame,
                text=opt["quality"],
                font=(FONT_FAMILY, 12, "bold"),
                text_color="white",
                anchor="w",
                justify="left",
                wraplength=340,
            )
            lbl.grid(row=0, column=0, sticky="w", padx=14, pady=8)
            
            # Action button to start download directly!
            btn = ctk.CTkButton(
                opt_frame,
                text=opt["button_text"],
                width=120,
                height=32,
                corner_radius=6,
                font=(FONT_FAMILY, 12, "bold"),
                fg_color=ACCENT,
                hover_color="#6a5ad4",
                command=lambda url=opt["url"]: self._start_download_from_search(url)
            )
            btn.grid(row=0, column=1, padx=14, pady=8)
            
            # Bind scroll inside options
            for w in [opt_frame, lbl, btn]:
                if hasattr(self, '_bind_scroll_events_to_widget'):
                    self._bind_scroll_events_to_widget(w, self.detail_scroll._parent_canvas)
        
        # Show detail frame inside tabview search tab
        self.search_detail_frame.grid(row=0, rowspan=2, column=0, sticky="nsew", padx=0, pady=0)

    def _start_download_from_search(self, url):
        self._update_status("Preparing download from search...")
        try:
            self.tabview.set("Direct URL")
        except Exception:
            pass
        self.url_entry.delete(0, "end")
        self.url_entry.insert(0, url)
        self.go_btn.configure(state="disabled")
        
        self._start_download_pipeline(url)

    # ── Pipeline: resolve → download overlap ────────────────────────────

    def _resolve_pipeline(self, url):
        """Resolve links and feed them to the download queue as they become ready.
        Downloads start as soon as the first link resolves — no waiting for all."""
        try:
            if "tgseed.link" in url:
                self._update_status("Using Telegram fallback link…")
                self._resolve_single_telegram_url(url)
                return

            if ".r2.dev/" in url:
                self._update_status("Using Cloud Download link…")
                self._resolve_single_cloud_url(url)
                return

            if "driveseed.org" in url:
                self._update_status("Extracting download URL…")
                self._resolve_single_driveseed(url, 0)
                return

            if not HAS_BATCH:
                self._update_status("Batch episodes module missing")
                self._enable_go()
                return

            self._update_status("Scraping page for links…")
            links = []
            try:
                links = scrape_links(url)
            except Exception as e:
                self._update_status(f"Error: {e}")
                self._enable_go()
                return

            if not links:
                self._update_status("No download links found on page")
                self._enable_go()
                return

            total = len(links)
            with self._lock:
                self._total_count = total
            self._update_status(f"Found {total} links — resolving…")

            # Create placeholder cards immediately
            self.after(0, lambda: self._create_placeholder_cards(total))
            time.sleep(0.1)  # let UI render

            # Check which files already exist
            existing = set()
            if os.path.isdir(self.output_dir):
                existing = set(os.listdir(self.output_dir))

            # Workers started flag
            workers_started = [0]
            workers_lock = threading.Lock()

            def _fused_resolve(i, link):
                """Resolve shortener + extract download URL in one shot."""
                _, name, ds_url = resolve_link(i, link, session=SESSION)
                source_link = dict(link) if isinstance(link, dict) else None
                size_hint = parse_size_hint_bytes(name)
                origin_url = link.get("url", "") if isinstance(link, dict) else ""
                origin_meta_size = None
                if "driveseed.org" in origin_url:
                    _, origin_meta_size = get_driveseed_file_metadata(origin_url)

                expected_size = origin_meta_size or size_hint

                if ds_url and "tgseed.link" in ds_url:
                    fname = name or f"Link {i + 1}"
                    fname = re.sub(r'[<>:"/\\|?*]', "_", fname)
                    tg_debug(
                        f"Resolve[{i}] TELEGRAM name='{fname}' expected_size={fmt_bytes(expected_size)} "
                        f"(origin_meta={fmt_bytes(origin_meta_size)} size_hint={fmt_bytes(size_hint)})"
                    )
                    return i, {
                        "filename": fname,
                        "download_url": ds_url,
                        "method": "TELEGRAM",
                        "target_dir": self.output_dir,
                        "expected_size_bytes": expected_size,
                        "source_link": source_link,
                        "source_index": i,
                        "source_name_hint": name,
                        "source_driveseed_url": None,
                    }

                if ds_url and ".r2.dev/" in ds_url:
                    fname = os.path.basename(urlparse(ds_url).path) or name or f"download_{i + 1}"
                    fname = re.sub(r'[<>:"/\\|?*]', "_", fname)
                    tg_debug(
                        f"Resolve[{i}] CLOUD name='{fname}' expected_size={fmt_bytes(expected_size)} "
                        f"(origin_meta={fmt_bytes(origin_meta_size)} size_hint={fmt_bytes(size_hint)})"
                    )
                    return i, {
                        "filename": fname,
                        "download_url": ds_url,
                        "method": "CLOUD",
                        "target_dir": self.output_dir,
                        "expected_size_bytes": expected_size,
                        "source_link": source_link,
                        "source_index": i,
                        "source_name_hint": name,
                        "source_driveseed_url": None,
                    }

                if not ds_url or "driveseed.org" not in ds_url:
                    return i, {
                        "filename": name or f"Link {i + 1}",
                        "download_url": None,
                        "method": "",
                        "error": "Not a driveseed link",
                    }

                meta_fname, raw_meta_size = get_driveseed_file_metadata(ds_url)
                meta_size = raw_meta_size or expected_size
                dl_url, fname, method = get_driveseed_download_url(ds_url)
                if not fname:
                    fname = (
                        meta_fname
                        or os.path.basename(urlparse(dl_url).path)
                        or name
                        or f"download_{i + 1}"
                    )
                fname = re.sub(r'[<>:"/\\|?*]', "_", fname)
                tg_debug(
                    f"Resolve[{i}] {method} name='{fname}' expected_size={fmt_bytes(meta_size)} "
                    f"(metadata={fmt_bytes(raw_meta_size)} origin_meta={fmt_bytes(origin_meta_size)} "
                    f"size_hint={fmt_bytes(size_hint)})"
                )
                return i, {
                    "filename": fname,
                    "download_url": dl_url,
                    "method": method,
                    "target_dir": self.output_dir,
                    "expected_size_bytes": meta_size,
                    "source_link": source_link,
                    "source_index": i,
                    "source_name_hint": name,
                    "source_driveseed_url": ds_url,
                }

            done_count = 0

            with ThreadPoolExecutor(max_workers=10) as pool:
                futures = {
                    pool.submit(_fused_resolve, i, lnk): i
                    for i, lnk in enumerate(links)
                }

                for future in as_completed(futures):
                    idx = futures[future]
                    try:
                        idx, item = future.result()
                    except Exception as e:
                        item = {
                            "filename": f"Failed link {idx + 1}",
                            "download_url": None,
                            "method": "",
                            "error": str(e),
                        }

                    done_count += 1
                    self._update_status(f"Ready {done_count}/{total}")

                    # Update the card and immediately feed to download queue
                    if item.get("download_url"):
                        # Check for already-downloaded file (smart skip)
                        fname = item["filename"]
                        if fname in existing:
                            self.after(
                                0,
                                lambda i=idx, it=item: self._update_card_done_skip(
                                    i, it
                                ),
                            )
                            with self._lock:
                                self._done_count += 1
                        else:
                            self.after(
                                0, lambda i=idx, it=item: self._update_card_ready(i, it)
                            )
                            if item.get("method") == "TELEGRAM":
                                self.after(
                                    0,
                                    lambda i=idx, it=item: self._prepare_telegram_manual_card(i, it),
                                )
                            else:
                                self.download_queue.put((idx, item))

                                # Start download workers as soon as first item is ready
                                with workers_lock:
                                    while workers_started[0] < MAX_CONCURRENT:
                                        workers_started[0] += 1
                                        threading.Thread(
                                            target=self._download_worker, daemon=True
                                        ).start()
                    else:
                        self.after(
                            0, lambda i=idx, it=item: self._update_card_failed(i, it)
                        )
                        with self._lock:
                            self._fail_count += 1

            # Final refresh
            self.after(100, lambda: self._refresh_status())

        except Exception as e:
            self._update_status(f"Error: {e}")
            self._enable_go()

    def _resolve_single_driveseed(self, url, idx):
        """Resolve a single driveseed URL and start downloading."""
        with self._lock:
            self._total_count = 1

        try:
            meta_fname, meta_size = get_driveseed_file_metadata(url)
            dl_url, fname, method = get_driveseed_download_url(url)
            if not fname:
                fname = meta_fname or os.path.basename(urlparse(dl_url).path) or "download_1"
            fname = re.sub(r'[<>:"/\\|?*]', "_", fname)
            item = {
                "filename": fname,
                "download_url": dl_url,
                "method": method,
                "target_dir": self.output_dir,
                "expected_size_bytes": meta_size,
                "source_link": None,
                "source_index": idx,
                "source_name_hint": fname,
                "source_driveseed_url": url,
            }

            # Check if already exists
            if os.path.isdir(self.output_dir) and fname in os.listdir(self.output_dir):
                self.after(0, lambda: self._create_single_card_done(item))
                with self._lock:
                    self._done_count += 1
                self.after(100, lambda: self._refresh_status())
                return

            self.after(0, lambda: self._create_single_card_and_start(item))
        except Exception as e:
            item = {
                "filename": "Failed link",
                "download_url": None,
                "method": "",
                "error": str(e),
            }
            self.after(0, lambda: self._create_single_card_failed(item))
            with self._lock:
                self._fail_count += 1
            self.after(100, lambda: self._refresh_status())

    def _resolve_single_cloud_url(self, url):
        """Queue a direct cloud URL (r2.dev) for download."""
        with self._lock:
            self._total_count = 1

        try:
            fname = os.path.basename(urlparse(url).path) or "download_1"
            fname = re.sub(r'[<>:"/\\|?*]', "_", fname)
            item = {
                "filename": fname,
                "download_url": url,
                "method": "CLOUD",
                "target_dir": self.output_dir,
                "expected_size_bytes": None,
                "source_link": None,
                "source_index": 0,
                "source_name_hint": fname,
                "source_driveseed_url": None,
            }

            if os.path.isdir(self.output_dir) and fname in os.listdir(self.output_dir):
                self.after(0, lambda: self._create_single_card_done(item))
                with self._lock:
                    self._done_count += 1
                self.after(100, lambda: self._refresh_status())
                return

            self.after(0, lambda: self._create_single_card_and_start(item))
        except Exception as e:
            item = {
                "filename": "Failed cloud link",
                "download_url": None,
                "method": "CLOUD",
                "error": str(e),
            }
            self.after(0, lambda: self._create_single_card_failed(item))
            with self._lock:
                self._fail_count += 1
            self.after(100, lambda: self._refresh_status())

    def _resolve_single_telegram_url(self, url):
        """Queue a Telegram fallback URL (tgseed.link)."""
        with self._lock:
            self._total_count = 1

        try:
            item = {
                "filename": "Telegram file",
                "download_url": url,
                "method": "TELEGRAM",
                "target_dir": self.output_dir,
                "expected_size_bytes": None,
                "source_link": None,
                "source_index": 0,
                "source_name_hint": "Telegram file",
                "source_driveseed_url": None,
            }
            self.after(0, lambda: self._create_single_card_telegram_manual(item))
        except Exception as e:
            item = {
                "filename": "Failed telegram link",
                "download_url": None,
                "method": "TELEGRAM",
                "error": str(e),
            }
            self.after(0, lambda: self._create_single_card_failed(item))
            with self._lock:
                self._fail_count += 1
            self.after(100, lambda: self._refresh_status())

    def _create_placeholder_cards(self, count):
        """Create placeholder cards with a 'resolving' state."""
        for i in range(count):
            card = DownloadCard(self.scroll_frame, i + 1, "Resolving…", method="…")
            card.grid(row=i, column=0, sticky="ew", pady=(0, 6))
            card.set_status("Resolving…", ACCENT)
            self.cards.append(card)

    def _update_card_ready(self, idx, item):
        """Update a placeholder card with resolved info and mark it pending."""
        if idx < len(self.cards):
            card = self.cards[idx]
            card.name_lbl.configure(text=item["filename"])
            card.set_method(item.get("method", ""))
            if item.get("method") == "TELEGRAM":
                card.set_status("Manual Telegram", AMBER)
                expected_size = item.get("expected_size_bytes")
                if expected_size:
                    card.set_detail(
                        f"Click Download to open Telegram Desktop. Expected: {fmt_bytes(expected_size)}"
                    )
                else:
                    card.set_detail("Click Download to open Telegram Desktop.")
            else:
                card.set_status("Queued", SUBTEXT)
            card.item_data = item

    def _update_card_done_skip(self, idx, item):
        """Mark a card as done (already exists)."""
        if idx < len(self.cards):
            card = self.cards[idx]
            card.name_lbl.configure(text=item["filename"])
            card.set_method(item.get("method", ""))
            card.item_data = item
            card.set_detail("Already downloaded")
            card.mark_done()

    def _update_card_failed(self, idx, item):
        """Mark a card as failed."""
        if idx < len(self.cards):
            card = self.cards[idx]
            card.name_lbl.configure(text=item["filename"])
            card.set_method(item.get("method", ""))
            card.item_data = item
            card.mark_failed(item.get("error", "Could not resolve"))

    def _create_single_card_and_start(self, item):
        """Create a card for a single driveseed URL and start download."""
        card = DownloadCard(
            self.scroll_frame, 1, item["filename"], method=item.get("method", "")
        )
        card.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        card.item_data = item
        self.cards.append(card)
        self.download_queue.put((0, item))
        threading.Thread(target=self._download_worker, daemon=True).start()

    def _create_single_card_telegram_manual(self, item):
        card = DownloadCard(
            self.scroll_frame, 1, item["filename"], method=item.get("method", "")
        )
        card.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        card.item_data = item
        card.set_status("Manual Telegram", AMBER)
        expected_size = item.get("expected_size_bytes")
        if expected_size:
            card.set_detail(
                f"Click Download to open Telegram Desktop. Expected: {fmt_bytes(expected_size)}"
            )
        else:
            card.set_detail("Click Download to open Telegram Desktop.")
        card.set_action("Download", lambda: self._start_telegram_manual_download(0))
        self.cards.append(card)

    def _create_single_card_done(self, item):
        card = DownloadCard(
            self.scroll_frame, 1, item["filename"], method=item.get("method", "")
        )
        card.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        card.item_data = item
        card.set_detail("Already downloaded")
        card.mark_done()
        self.cards.append(card)

    def _create_single_card_failed(self, item):
        card = DownloadCard(
            self.scroll_frame, 1, item["filename"], method=item.get("method", "")
        )
        card.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        card.item_data = item
        card.mark_failed(item.get("error", ""))
        self.cards.append(card)

    # ── Download worker ─────────────────────────────────────────────────

    def _download_worker(self):
        """Worker thread that downloads files from the queue."""
        while True:
            try:
                idx, item = self.download_queue.get(timeout=5)
            except Empty:
                break

            with self._lock:
                self._active_threads += 1
            self.after(0, lambda: self._refresh_status())

            card = self.cards[idx]
            self.after(0, card.mark_downloading)

            try:
                url = item["download_url"]
                method = item.get("method", "")
                filename = item["filename"]
                target_dir = item.get("target_dir", self.output_dir)
                filepath = os.path.join(target_dir, filename)
                part_path = filepath + ".part"

                if method == "TELEGRAM" or "tgseed.link" in url:
                    self.after(
                        0,
                        lambda i=idx, it=item: self._prepare_telegram_manual_card(i, it),
                    )
                    continue

                # Smart file-exists: check full file AND .part for resume
                if os.path.exists(filepath):
                    file_size = os.path.getsize(filepath)
                    with self._lock:
                        self._done_count += 1
                        self._active_threads -= 1
                    self.after(
                        0,
                        lambda c=card, s=file_size: (
                            c.set_detail(
                                f"Already downloaded ({s / (1024 * 1024):.1f} MB)"
                            ),
                            c.mark_done(),
                        ),
                    )
                    self.after(0, lambda: self._refresh_status())
                    continue

                # Resume support: check for .part file
                resume_from = 0
                if os.path.exists(part_path):
                    resume_from = os.path.getsize(part_path)

                headers = {}
                if resume_from > 0:
                    headers["Range"] = f"bytes={resume_from}-"
                    self.after(
                        0,
                        lambda c=card, r=resume_from: c.set_detail(
                            f"Resuming from {r / (1024 * 1024):.1f} MB…"
                        ),
                    )

                resp = SESSION.get(url, stream=True, timeout=300, headers=headers)

                # If server doesn't support Range, restart from 0
                if resp.status_code == 200 and resume_from > 0:
                    resume_from = 0
                elif resp.status_code not in (200, 206):
                    resp.raise_for_status()

                total = int(resp.headers.get("content-length", 0)) + resume_from
                downloaded = resume_from
                start_time = time.time()
                last_update = 0

                mode = "ab" if resume_from > 0 and resp.status_code == 206 else "wb"

                with open(part_path, mode) as f:
                    for chunk in resp.iter_content(chunk_size=CHUNK_SIZE):
                        if chunk:
                            f.write(chunk)
                            downloaded += len(chunk)
                            now = time.time()

                            if now - last_update < 0.25:
                                continue
                            last_update = now

                            elapsed = now - start_time
                            speed = (
                                (downloaded - resume_from) / elapsed
                                if elapsed > 0
                                else 0
                            )
                            pct = downloaded / total if total else 0

                            mb_down = downloaded / (1024 * 1024)
                            speed_str = self._fmt_speed(speed)
                            if total:
                                mb_total = total / (1024 * 1024)
                                remaining = (
                                    (total - downloaded) / speed if speed > 0 else 0
                                )
                                eta_str = self._fmt_time(remaining)
                                detail = f"{mb_down:.1f}/{mb_total:.1f} MB  •  {speed_str}  •  ETA {eta_str}"
                            else:
                                detail = f"{mb_down:.1f} MB  •  {speed_str}"

                            self.after(
                                0,
                                lambda c=card, p=pct, d=detail: (
                                    c.set_progress(p),
                                    c.set_detail(d),
                                ),
                            )

                os.rename(part_path, filepath)

                total_mb = (total or downloaded) / (1024 * 1024)
                elapsed = time.time() - start_time
                net_down = downloaded - resume_from
                avg = self._fmt_speed(net_down / elapsed) if elapsed > 0 else "–"

                with self._lock:
                    self._done_count += 1
                self.after(
                    0,
                    lambda c=card, t=total_mb, a=avg: (
                        c.mark_done(),
                        c.set_detail(f"{t:.1f} MB  •  avg {a}"),
                    ),
                )

            except Exception as e:
                with self._lock:
                    self._fail_count += 1
                self.after(0, lambda c=card, err=str(e): c.mark_failed(err))

            finally:
                with self._lock:
                    self._active_threads -= 1
                self.after(0, lambda: self._refresh_status())

        self.after(0, lambda: self._refresh_status())

    # ── Status ──────────────────────────────────────────────────────────

    def _refresh_status(self):
        with self._lock:
            done = self._done_count
            failed = self._fail_count
            active = self._active_threads
            total = self._total_count

        pending = total - done - failed - active

        if active == 0 and pending <= 0 and total > 0:
            msg = f"Complete — {done} done"
            if failed:
                msg += f", {failed} failed"
                self.retry_btn.grid()
            else:
                self.retry_btn.grid_remove()
            self.status_lbl.configure(text=msg)
            self.go_btn.configure(state="normal")
        else:
            self.retry_btn.grid_remove()
            self.status_lbl.configure(
                text=f"{done} done  •  {active} active  •  {max(0, pending)} pending"
                + (f"  •  {failed} failed" if failed else "")
            )

    @staticmethod
    def _fmt_speed(bps):
        if bps >= 1024 * 1024:
            return f"{bps / (1024 * 1024):.1f} MB/s"
        elif bps >= 1024:
            return f"{bps / 1024:.0f} KB/s"
        return f"{bps:.0f} B/s"

    @staticmethod
    def _fmt_time(seconds):
        if seconds < 60:
            return f"{seconds:.0f}s"
        m, s = divmod(int(seconds), 60)
        return f"{m}m {s}s"

    def _update_status(self, text):
        self.after(0, lambda: self.status_lbl.configure(text=text))

    def _enable_go(self):
        self.after(0, lambda: self.go_btn.configure(state="normal"))

    def _retry_failed(self):
        """Re-queue all failed downloads."""
        self.retry_btn.grid_remove()
        retry_http = 0
        retry_manual = 0

        for i, card in enumerate(self.cards):
            if card.state == DownloadCard.STATE_FAILED:
                item = getattr(card, "item_data", None)
                if item and item.get("download_url"):
                    card.mark_pending()
                    if item.get("method") == "TELEGRAM":
                        self._prepare_telegram_manual_card(i, item)
                        retry_manual += 1
                    else:
                        self.download_queue.put((i, item))
                        retry_http += 1
                    with self._lock:
                        self._fail_count -= 1
                else:
                    card.set_detail("Cannot retry unresolved link")

        if retry_http or retry_manual:
            msg = ""
            if retry_http:
                msg = f"Retrying {retry_http} failed download(s)…"
            if retry_manual:
                manual_msg = f"{retry_manual} Telegram item(s) ready for manual download"
                msg = f"{msg}  •  {manual_msg}" if msg else manual_msg
            self._update_status(msg)

        if retry_http:
            for _ in range(min(MAX_CONCURRENT, retry_http)):
                threading.Thread(target=self._download_worker, daemon=True).start()
        elif not retry_manual:
            self._update_status("No retryable downloads (links failed to resolve)")


if __name__ == "__main__":
    app = DownloaderApp()
    app.mainloop()
