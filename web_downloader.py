#!/usr/bin/env python3
"""
MoviesCrackd Web Downloader Backend Server.
Provides a premium, responsive local API server for the index.html frontend.
"""

import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
from html import unescape
from pathlib import Path
from queue import Empty, PriorityQueue
from urllib.parse import parse_qs, urlparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import webbrowser

from http.server import HTTPServer, BaseHTTPRequestHandler
import socketserver
# Try importing tkinter for native folder picker support
try:
    import tkinter as tk
    from tkinter import filedialog
    HAS_TKINTER = True
except ImportError:
    HAS_TKINTER = False

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from env_loader import load_env_file, session_file_exists

# Load environments
load_env_file()

START_TIME = time.time()

# Shared server-side IMDb suggestion LRU cache (OrderedDict for efficient eviction)
import hashlib
from collections import OrderedDict
IMDB_SUGGEST_CACHE = OrderedDict()
IMDB_SUGGEST_CACHE_MAX = 50000

# Persistent Disk Cache configuration
IMDB_SUGGEST_CACHE_FILE = os.path.join('static', 'imdb_suggest_cache.json')
IMG_PROXY_DIR = os.path.join('static', 'cached_posters')

def load_imdb_suggest_cache_from_file():
    """Load cached IMDb autocomplete suggestions from persistent disk on startup."""
    global IMDB_SUGGEST_CACHE
    try:
        if os.path.exists(IMDB_SUGGEST_CACHE_FILE):
            with open(IMDB_SUGGEST_CACHE_FILE, 'r', encoding='utf-8') as f:
                loaded = json.load(f)
                if isinstance(loaded, dict):
                    IMDB_SUGGEST_CACHE.clear()
                    for k, v in loaded.items():
                        IMDB_SUGGEST_CACHE[k] = v
                    print(f"[+] Loaded {len(IMDB_SUGGEST_CACHE)} IMDb suggestion cache entries from persistent disk file.", flush=True)
    except Exception as e:
        print(f"[-] Failed to load persistent IMDb suggestion cache: {e}", flush=True)

def save_imdb_suggest_cache_to_file():
    """Save in-memory IMDb autocomplete suggestions cache to persistent disk."""
    try:
        os.makedirs(os.path.dirname(IMDB_SUGGEST_CACHE_FILE), exist_ok=True)
        with open(IMDB_SUGGEST_CACHE_FILE, 'w', encoding='utf-8') as f:
            json.dump(dict(IMDB_SUGGEST_CACHE), f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"[-] Failed to save IMDb suggestion cache to disk: {e}", flush=True)

# Search Logging Configuration
SEARCH_LOGS_FILE = os.path.join('static', 'search_logs.txt')
LOG_LOCK = threading.Lock()

DEBOUNCE_TIMER = None
DEBOUNCE_QUERY = None
DEBOUNCE_LOCK = threading.Lock()

def get_ist_timestamp():
    import datetime
    utc_now = datetime.datetime.utcnow()
    ist_offset = datetime.timedelta(hours=5, minutes=30)
    ist_now = utc_now + ist_offset
    return ist_now.strftime("%Y-%m-%d %I:%M:%S %p")

def clean_log_title(title):
    """Strip bracket tags, resolutions, and size descriptors to produce beautiful clean movie titles."""
    if not title:
        return "Direct URL Input"
    cleaned = title
    # Successively split common format qualifiers to isolate the true title name
    for sep in [' ||', ' [', ' {', ' (']:
        if sep in cleaned:
            cleaned = cleaned.split(sep)[0]
    return cleaned.strip()

def write_debounced_log():
    """Timer callback to write the final stabilized query to the search logs file."""
    global DEBOUNCE_QUERY
    try:
        with DEBOUNCE_LOCK:
            query_to_write = DEBOUNCE_QUERY
            DEBOUNCE_QUERY = None
            
        if query_to_write:
            os.makedirs(os.path.dirname(SEARCH_LOGS_FILE), exist_ok=True)
            timestamp = get_ist_timestamp()
            log_line = f"[{timestamp} IST] {query_to_write}\n"
            
            with LOG_LOCK:
                with open(SEARCH_LOGS_FILE, 'a', encoding='utf-8') as f:
                    f.write(log_line)
    except Exception as e:
        print(f"[-] Error writing search logs: {e}", flush=True)

def log_search_query(query, client_id=None):
    """Log search query with an IST timestamp using a thread-safe debounce timer to filter typing sequence clutter."""
    global DEBOUNCE_TIMER, DEBOUNCE_QUERY
    if not query or len(query.strip()) < 2:
        return
        
    query_str = query.strip()
    uid = client_id if client_id else "anonymous"
    formatted_msg = f"👤 {uid:<10} | 🔍 SEARCHED  | \"{query_str}\""
    
    with DEBOUNCE_LOCK:
        # Cancel any pending log timer
        if DEBOUNCE_TIMER is not None:
            DEBOUNCE_TIMER.cancel()
            
        DEBOUNCE_QUERY = formatted_msg
        
        # Start a new timer for 1.8 seconds (giving the user plenty of time to finish typing)
        DEBOUNCE_TIMER = threading.Timer(1.8, write_debounced_log)
        DEBOUNCE_TIMER.start()

def log_instant_event(event_message):
    """Write an instant event with an IST timestamp to the logs file immediately and thread-safely."""
    try:
        os.makedirs(os.path.dirname(SEARCH_LOGS_FILE), exist_ok=True)
        timestamp = get_ist_timestamp()
        
        # Format multi-line logs perfectly so they align under the starting columns (26 character prefix)
        prefix = f"[{timestamp} IST] "
        indent = " " * len(prefix)
        formatted_message = event_message.replace("\n", f"\n{indent}")
        log_line = f"{prefix}{formatted_message}\n"
        
        with LOG_LOCK:
            with open(SEARCH_LOGS_FILE, 'a', encoding='utf-8') as f:
                f.write(log_line)
    except Exception as e:
        print(f"[-] Error writing instant event logs: {e}", flush=True)

# Core Scraper and Resolver imports
try:
    from batch_episodes import resolve_link, scrape_links
    HAS_BATCH = True
except ImportError:
    HAS_BATCH = False

from direct_downloader import (
    HEADERS,
    get_driveseed_download_url,
    get_driveseed_file_metadata,
    resolve_driveseed,
)

try:
    from telegram_fallback import TelegramDownloadError
    HAS_TELEGRAM_FALLBACK = True
except ImportError:
    HAS_TELEGRAM_FALLBACK = False
    class TelegramDownloadError(Exception):
        pass

try:
    from movie_search import search_movies, extract_download_options, resolve_search_domains
    from imdb_scraper import get_imdb_id
except ImportError:
    def get_imdb_id(q): return None


# Shared requests session
def make_session():
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

# DNS Pre-warming
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

# Import the shortener's last known domain for DNS pre-warming
try:
    from skip_shortener import LAST_WORKING_DOMAIN as _SHORTENER_DOMAIN
    if _SHORTENER_DOMAIN and _SHORTENER_DOMAIN not in DNS_DOMAINS:
        DNS_DOMAINS.append(_SHORTENER_DOMAIN)
except ImportError:
    pass

_dns_ready = threading.Event()

def prewarm_dns():
    def _resolve(host):
        try:
            socket.getaddrinfo(host, 443)
        except Exception:
            pass
    with ThreadPoolExecutor(max_workers=len(DNS_DOMAINS)) as pool:
        pool.map(_resolve, DNS_DOMAINS)
    _dns_ready.set()

MAX_CONCURRENT = 2
CHUNK_SIZE = 2 * 1024 * 1024  # 2 MB
MAX_AUTO_RETRIES = 2  # Reduced from 3: show manual Retry button faster to save CPU on Render free tier

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


# ── Virtual State Card Manager ───────────────────────────────────────────

class VirtualDownloadCard:
    """A thread-safe model representation of a single download card state."""
    def __init__(self, index, filename, method=""):
        self.index = index
        self.filename = filename
        self.method = method
        self.state = 0  # 0=Pending, 1=Downloading, 2=Done, 3=Failed
        self.progress = 0.0
        self.detail = "Pending"
        self.status = "Pending"
        self.action_text = ""
        self.action_state = "normal"
        self.item_data = None
        self.url = ""
        self.retry_count = 0  # Track how many times this card has been retried

    def set_method(self, method):
        self.method = method

    def set_status(self, status):
        self.status = status

    def set_progress(self, val):
        self.progress = val

    def set_detail(self, detail):
        self.detail = detail

    def set_action(self, text, command=None, state="normal"):
        self.action_text = text
        self.action_state = state

    def hide_action(self):
        self.action_text = ""

    def mark_done(self):
        self.state = 2
        self.status = "✓ Done"
        self.progress = 1.0
        if not self.detail or self.detail == "Pending":
            self.detail = "Already downloaded"
        self.hide_action()

    def mark_failed(self, reason=""):
        self.state = 3
        self.status = "✗ Failed"
        self.progress = 0.0
        if reason:
            self.detail = reason
        self.set_action("Retry")

    def mark_downloading(self):
        self.state = 1
        self.status = "Downloading…"

    def mark_pending(self):
        self.state = 0
        self.status = "Pending"
        self.detail = "Retrying…"

    def to_json(self):
        size_str = ""
        if self.item_data and self.item_data.get("expected_size_bytes"):
            exp_bytes = self.item_data.get("expected_size_bytes")
            if exp_bytes and exp_bytes > 1024 * 100:
                size_str = fmt_bytes(exp_bytes)
                if size_str == "unknown":
                    size_str = ""
        return {
            "index": self.index,
            "filename": self.filename,
            "method": self.method,
            "state": self.state,
            "progress": self.progress,
            "detail": self.detail,
            "status": self.status,
            "action_text": self.action_text,
            "action_state": self.action_state,
            "resolved_url": self.url,
            "size": size_str,
        }


class ClientState:
    def __init__(self, client_id, output_dir):
        self.client_id = client_id
        self.cards: list[VirtualDownloadCard] = []
        self.done_count = 0
        self.fail_count = 0
        self.total_count = 0
        self.active_threads = 0
        self.output_dir = output_dir
        self.active_title = "Direct URL Input"
        self.generation = 0


# ── Standalone Download Manager Core ─────────────────────────────────────

class DownloaderBackend:
    def __init__(self):
        self.output_dir = os.path.expanduser("/media/so9ic/HDD/Downloads/Movies")
        self.cloud_mode = os.getenv("CLOUD_MODE", "false").lower() == "true" or "DISPLAY" not in os.environ
        self.client_states: dict[str, ClientState] = {}
        self._states_lock = threading.Lock()
        self.download_queue = PriorityQueue()
        self._lock = threading.Lock()
        self._telegram_lock = threading.Lock()
        
        # Obsolete global counters (maintained as properties for backward compatibility if needed, but not used internally)
        self._done_count = 0
        self._fail_count = 0
        self._total_count = 0
        self._active_threads = 0
        
        self.client_id = None
        self.active_title = 'Direct URL Input'

    def get_client_state(self, client_id: str | None) -> ClientState:
        cid = client_id or "anonymous"
        with self._states_lock:
            if cid not in self.client_states:
                self.client_states[cid] = ClientState(cid, self.output_dir)
            return self.client_states[cid]

    def clear_client_state(self, client_id: str | None) -> None:
        cid = client_id or "anonymous"
        with self._states_lock:
            if cid in self.client_states:
                del self.client_states[cid]

        # Start MODLIST polling loop
        self._launch_modlist_poller()

    def _launch_modlist_poller(self):
        def _poll():
            print("[*] Launching modlist background domain poller...", flush=True)
            while True:
                try:
                    resolve_search_domains(force_refresh=True)
                except Exception as e:
                    print(f"[-] Modlist poller error: {e}", flush=True)
                time.sleep(1800)  # Poll every 30 minutes
        threading.Thread(target=_poll, daemon=True).start()

    def _get_telegram_ready_status(self):
        if not HAS_TELEGRAM_FALLBACK:
            return "Telegram: Not Ready", "red"

        api_id = os.getenv("TELEGRAM_API_ID", "").strip()
        api_hash = os.getenv("TELEGRAM_API_HASH", "").strip()
        if not api_id or not api_hash:
            return "Telegram: Config Missing", "amber"

        session_name = os.getenv("TELEGRAM_SESSION", "tgseed_session").strip() or "tgseed_session"
        if not session_file_exists(session_name):
            return "Telegram: Login Needed", "amber"

        return "Telegram: Ready", "green"

    # ── Folder picker native dialog ──
    def ask_directory(self) -> str:
        if not HAS_TKINTER:
            print("[-] ask_directory bypassed: Tkinter is not installed (running headless).", flush=True)
            return ""
        try:
            root = tk.Tk()
            root.withdraw()
            root.attributes("-topmost", True)
            folder = filedialog.askdirectory(
                initialdir=self.output_dir,
                title="Select Save Directory"
            )
            root.destroy()
            return folder
        except Exception as e:
            print(f"[-] Native dialog failed: {e}", flush=True)
            return ""

    def start_pipeline(self, url, output_dir, client_id=None):
        state = self.get_client_state(client_id)
        state.output_dir = output_dir

        with self._lock:
            state.generation += 1
            state.done_count = 0
            state.fail_count = 0
            state.total_count = 0
            state.active_threads = 0
            state.cards.clear()

        # Do not clear the queue as it contains other clients' downloads!

        threading.Thread(target=prewarm_dns, daemon=True).start()
        # Wait briefly for DNS pre-warming so the first shortener call benefits
        _dns_ready.wait(timeout=2.0)
        threading.Thread(target=self._resolve_pipeline, args=(url, state.client_id, state.generation), daemon=True).start()

    def _resolve_pipeline(self, url, client_id, generation):
        state = self.get_client_state(client_id)
        if state.generation != generation:
            return
        try:
            if "tgseed.link" in url:
                self._resolve_single_telegram_url(url, client_id, generation)
                return

            if ".r2.dev/" in url:
                self._resolve_single_cloud_url(url, client_id, generation)
                return

            if "driveseed.org" in url:
                self._resolve_single_driveseed(url, 0, client_id, generation)
                return

            if not HAS_BATCH:
                print("[-] Batch module missing")
                card = VirtualDownloadCard(1, "Error", "ERROR")
                card.mark_failed("Batch module missing on server")
                with self._lock:
                    if state.generation == generation:
                        state.cards.append(card)
                return

            links = scrape_links(url)
            if not links:
                print("[-] No download links found on page")
                card = VirtualDownloadCard(1, "No links found", "ERROR")
                card.mark_failed("No download links found on page")
                with self._lock:
                    if state.generation == generation:
                        state.cards.append(card)
                return

            total = len(links)
            with self._lock:
                if state.generation != generation:
                    return
                state.total_count = total

            # Create placeholder cards
            for i in range(total):
                card = VirtualDownloadCard(i + 1, "Resolving…", "…")
                card.set_status("Resolving…")
                card.set_progress(0.15)
                with self._lock:
                    if state.generation == generation:
                        state.cards.append(card)

            existing = set()
            if os.path.isdir(state.output_dir):
                existing = set(os.listdir(state.output_dir))

            workers_started = [0]
            workers_lock = threading.Lock()

            def _resolve_single_card(i, link):
                """
                Fire-and-forget worker: independently resolves one link and
                updates its card immediately — no futures/as_completed blocking.
                """
                state_inner = self.get_client_state(client_id)
                if state_inner.generation != generation:
                    return
                if i >= len(state_inner.cards):
                    return
                card = state_inner.cards[i]
                last_error = "Unknown error"
                best_name = None  # Preserve the best filename across retries

                for attempt in range(MAX_AUTO_RETRIES):
                    try:
                        if state_inner.generation != generation:
                            return
                        if attempt > 0:
                            # Brief pause between auto-retries, then reset animation
                            time.sleep(0.5)
                            if state_inner.generation != generation:
                                return
                            card.set_status("Resolving…")
                            card.set_progress(0.15)

                        # Phase 1: Shortener bypass → get name + driveseed URL
                        card.set_status("Bypassing shortener…")
                        card.set_progress(0.15)
                        _, name, ds_url = resolve_link(i, link, session=SESSION)
                        
                        # Smooth animation for Phase 1 (Shortener bypass)
                        import random
                        p1_dur = 0.5 + random.uniform(-0.15, 0.15)
                        p1_steps = 5
                        for s in range(p1_steps):
                            if state_inner.generation != generation:
                                return
                            t = (s + 1) / p1_steps
                            val = 0.15 + (0.45 - 0.15) * t + random.uniform(-0.03, 0.03)
                            card.set_progress(min(0.48, max(0.15, val)))
                            time.sleep(p1_dur / p1_steps)
                        
                        source_link = dict(link) if isinstance(link, dict) else None

                        if state_inner.generation != generation:
                            return

                        # Show filename on card immediately (visible in next poll)
                        if name:
                            card.filename = name
                            best_name = name

                        size_hint = parse_size_hint_bytes(name)
                        origin_url = link.get("url", "") if isinstance(link, dict) else ""
                        origin_meta_size = None
                        if "driveseed.org" in origin_url:
                            try:
                                _, origin_meta_size = get_driveseed_file_metadata(origin_url)
                            except Exception:
                                pass

                        expected_size = origin_meta_size or size_hint

                        # ── Telegram link ──
                        if ds_url and "tgseed.link" in ds_url:
                            fname = name or f"Link {i + 1}"
                            fname = re.sub(r'[<>:"/\\|?*]', "_", fname)
                            item = {
                                "client_id": client_id,
                                "generation": generation,
                                "filename": fname,
                                "download_url": ds_url,
                                "method": "TELEGRAM",
                                "target_dir": state_inner.output_dir,
                                "expected_size_bytes": expected_size,
                                "source_link": source_link,
                                "source_index": i,
                                "source_name_hint": name,
                                "source_driveseed_url": None,
                            }
                            # Smooth final fill animation before finalizing
                            p3_dur = 0.4 + random.uniform(-0.1, 0.1)
                            p3_steps = 4
                            start_p = card.progress or 0.45
                            for s in range(p3_steps):
                                if state_inner.generation != generation:
                                    return
                                t = (s + 1) / p3_steps
                                val = start_p + (0.98 - start_p) * t + random.uniform(-0.02, 0.02)
                                card.set_progress(min(0.99, max(start_p, val)))
                                time.sleep(p3_dur / p3_steps)
                            self._finalize_card(card, i, item, existing, workers_started, workers_lock, client_id, generation)
                            return  # Success!

                        # ── Direct Cloud R2 link ──
                        if ds_url and ".r2.dev/" in ds_url:
                            fname = os.path.basename(urlparse(ds_url).path) or name or f"download_{i + 1}"
                            fname = re.sub(r'[<>:"/\\|?*]', "_", fname)
                            item = {
                                "client_id": client_id,
                                "generation": generation,
                                "filename": fname,
                                "download_url": ds_url,
                                "method": "CLOUD",
                                "target_dir": state_inner.output_dir,
                                "expected_size_bytes": expected_size,
                                "source_link": source_link,
                                "source_index": i,
                                "source_name_hint": name,
                                "source_driveseed_url": None,
                            }
                            # Smooth final fill animation before finalizing
                            p3_dur = 0.4 + random.uniform(-0.1, 0.1)
                            p3_steps = 4
                            start_p = card.progress or 0.45
                            for s in range(p3_steps):
                                if state_inner.generation != generation:
                                    return
                                t = (s + 1) / p3_steps
                                val = start_p + (0.98 - start_p) * t + random.uniform(-0.02, 0.02)
                                card.set_progress(min(0.99, max(start_p, val)))
                                time.sleep(p3_dur / p3_steps)
                            self._finalize_card(card, i, item, existing, workers_started, workers_lock, client_id, generation)
                            return  # Success!

                        # ── Not a valid link — auto-retry ──
                        if not ds_url or "driveseed.org" not in ds_url:
                            last_error = "Not a driveseed link"
                            continue  # Auto-retry

                        # Phase 2: Driveseed resolution → unified single-fetch for metadata + download URL
                        card.set_status("Resolving driveseed…")
                        dl_url, ds_fname, ds_size, method = resolve_driveseed(ds_url)
                        
                        # Smooth animation for Phase 2
                        p2_dur = 0.5 + random.uniform(-0.15, 0.15)
                        p2_steps = 5
                        start_p = card.progress or 0.45
                        for s in range(p2_steps):
                            if state_inner.generation != generation:
                                return
                            t = (s + 1) / p2_steps
                            val = start_p + (0.80 - start_p) * t + random.uniform(-0.03, 0.03)
                            card.set_progress(min(0.83, max(start_p, val)))
                            time.sleep(p2_dur / p2_steps)

                        meta_size = ds_size or expected_size
                        fname = ds_fname or name or os.path.basename(urlparse(dl_url).path) or f"download_{i + 1}"
                        fname = re.sub(r'[<>:"/\\|?*]', "_", fname)

                        item = {
                            "client_id": client_id,
                            "generation": generation,
                            "filename": fname,
                            "download_url": dl_url,
                            "method": method,
                            "target_dir": state_inner.output_dir,
                            "expected_size_bytes": meta_size,
                            "source_link": source_link,
                            "source_index": i,
                            "source_name_hint": name,
                            "source_driveseed_url": ds_url,
                        }
                        
                        # Phase 3: Final fill animation before finalizing
                        p3_dur = 0.4 + random.uniform(-0.1, 0.1)
                        p3_steps = 4
                        start_p = card.progress or 0.80
                        for s in range(p3_steps):
                            if state_inner.generation != generation:
                                return
                            t = (s + 1) / p3_steps
                            val = start_p + (0.98 - start_p) * t + random.uniform(-0.02, 0.02)
                            card.set_progress(min(0.99, max(start_p, val)))
                            time.sleep(p3_dur / p3_steps)

                        self._finalize_card(card, i, item, existing, workers_started, workers_lock, client_id, generation)
                        return  # Success!

                    except Exception as e:
                        last_error = str(e)
                        continue  # Auto-retry

                # All auto-retries exhausted — mark as failed with manual Retry button
                if state_inner.generation != generation:
                    return
                source_link = dict(link) if isinstance(link, dict) else None
                display_name = best_name or (link.get("name") if isinstance(link, dict) else None) or f"Failed link {i + 1}"
                card.filename = display_name
                card.item_data = {
                    "client_id": client_id,
                    "generation": generation,
                    "source_link": source_link,
                    "source_index": i,
                    "source_name_hint": display_name,
                }
                card.mark_failed(last_error)
                with self._lock:
                    if state_inner.generation == generation:
                        state_inner.fail_count += 1

            # Fire all resolve threads independently
            for i, lnk in enumerate(links):
                threading.Thread(
                    target=_resolve_single_card,
                    args=(i, lnk),
                    daemon=True
                ).start()

        except Exception as e:
            print(f"[-] Resolve error: {e}", flush=True)
            card = VirtualDownloadCard(1, "Error", "ERROR")
            card.mark_failed(f"Resolution error: {str(e)}")
            with self._lock:
                if state.generation == generation:
                    state.cards.append(card)

    def _finalize_card(self, card, idx, item, existing, workers_started, workers_lock, client_id, generation):
        """
        Atomically finalize a resolved card: set metadata, url, and decide
        whether to queue for download. Called from independent worker threads.
        """
        state = self.get_client_state(client_id)
        if state.generation != generation:
            return

        fname = item["filename"]
        card.filename = fname
        card.set_method(item.get("method", ""))
        card.item_data = item
        card.url = item["download_url"]

        if fname in existing:
            card.set_detail("Already downloaded")
            card.mark_done()
            with self._lock:
                if state.generation == generation:
                    state.done_count += 1
        elif item.get("method") == "TELEGRAM":
            card.set_status("Manual Telegram")
            exp = item.get("expected_size_bytes")
            card.set_detail(
                f"Click Download to open Telegram Desktop. Expected: {fmt_bytes(exp)}"
                if exp else "Click Download to open Telegram Desktop."
            )
            card.set_action("Download", lambda index=idx: self.start_telegram_manual(index, client_id=client_id))
        elif self.cloud_mode:
            card.status = "✓ Ready"
            card.state = 2
            card.progress = 1.0
            card.detail = "Direct link resolved! Click 'Download to Device' below."
            with self._lock:
                if state.generation == generation:
                    state.done_count += 1
        else:
            card.set_status("Queued")
            self.download_queue.put((idx, item))
            with workers_lock:
                while workers_started[0] < MAX_CONCURRENT:
                    workers_started[0] += 1
                    threading.Thread(target=self._download_worker, daemon=True).start()

    def _resolve_single_driveseed(self, url, idx, client_id, generation):
        state = self.get_client_state(client_id)
        with self._lock:
            if state.generation == generation:
                state.total_count = 1
        card = VirtualDownloadCard(1, "Resolving…", "…")
        with self._lock:
            if state.generation == generation:
                state.cards.append(card)

        try:
            dl_url, fname, meta_size, method = resolve_driveseed(url)
            if not fname:
                fname = os.path.basename(urlparse(dl_url).path) or "download_1"
            fname = re.sub(r'[<>:"/\\|?*]', "_", fname)
            
            # Smooth animation for direct driveseed url resolution (Phase 2 & 3)
            import random
            p2_dur = 0.8 + random.uniform(-0.2, 0.2)
            p2_steps = 8
            for s in range(p2_steps):
                if state.generation == generation:
                    t = (s + 1) / p2_steps
                    val = 0.15 + (0.98 - 0.15) * t + random.uniform(-0.03, 0.03)
                    card.set_progress(min(0.99, max(0.15, val)))
                    time.sleep(p2_dur / p2_steps)
            
            item = {
                "client_id": client_id,
                "generation": generation,
                "filename": fname,
                "download_url": dl_url,
                "method": method,
                "target_dir": state.output_dir,
                "expected_size_bytes": meta_size,
                "source_link": None,
                "source_index": 0,
                "source_name_hint": fname,
                "source_driveseed_url": url,
            }
            card.filename = fname
            card.set_method(method)
            card.item_data = item
            card.url = dl_url

            existing = set(os.listdir(state.output_dir)) if os.path.isdir(state.output_dir) else set()
            if fname in existing:
                card.mark_done()
                with self._lock:
                    if state.generation == generation:
                        state.done_count += 1
                return

            if self.cloud_mode:
                card.status = "✓ Ready"
                card.state = 2
                card.progress = 1.0
                card.detail = "Direct link resolved! Click 'Download to Device' below."
                with self._lock:
                    if state.generation == generation:
                        state.done_count += 1
                return

            self.download_queue.put((0, item))
            threading.Thread(target=self._download_worker, daemon=True).start()

        except Exception as e:
            card.mark_failed(str(e))
            with self._lock:
                if state.generation == generation:
                    state.fail_count += 1

    def _resolve_single_cloud_url(self, url, client_id, generation):
        state = self.get_client_state(client_id)
        with self._lock:
            if state.generation == generation:
                state.total_count = 1
        fname = os.path.basename(urlparse(url).path) or "download_1"
        fname = re.sub(r'[<>:"/\\|?*]', "_", fname)
        card = VirtualDownloadCard(1, fname, "CLOUD")
        with self._lock:
            if state.generation == generation:
                state.cards.append(card)

        item = {
            "client_id": client_id,
            "generation": generation,
            "filename": fname,
            "download_url": url,
            "method": "CLOUD",
            "target_dir": state.output_dir,
            "expected_size_bytes": None,
            "source_link": None,
            "source_index": 0,
            "source_name_hint": fname,
            "source_driveseed_url": None,
        }
        card.item_data = item
        card.url = url

        existing = set(os.listdir(state.output_dir)) if os.path.isdir(state.output_dir) else set()
        if fname in existing:
            card.mark_done()
            with self._lock:
                if state.generation == generation:
                    state.done_count += 1
            return

        if self.cloud_mode:
            card.status = "✓ Ready"
            card.state = 2
            card.progress = 1.0
            card.detail = "Direct link resolved! Click 'Download to Device' below."
            with self._lock:
                if state.generation == generation:
                    state.done_count += 1
            return

        self.download_queue.put((0, item))
        threading.Thread(target=self._download_worker, daemon=True).start()

    def _resolve_single_telegram_url(self, url, client_id, generation):
        state = self.get_client_state(client_id)
        with self._lock:
            if state.generation == generation:
                state.total_count = 1
        card = VirtualDownloadCard(1, "Telegram file", "TELEGRAM")
        with self._lock:
            if state.generation == generation:
                state.cards.append(card)

        item = {
            "client_id": client_id,
            "generation": generation,
            "filename": "Telegram file",
            "download_url": url,
            "method": "TELEGRAM",
            "target_dir": state.output_dir,
            "expected_size_bytes": None,
            "source_link": None,
            "source_index": 0,
            "source_name_hint": "Telegram file",
            "source_driveseed_url": None,
        }
        card.item_data = item
        card.url = url
        card.set_status("Manual Telegram")
        card.set_detail("Click Download to open Telegram Desktop.")
        card.set_action("Download", lambda: self.start_telegram_manual(0, client_id=client_id))

    def _download_worker(self):
        while True:
            try:
                idx, item = self.download_queue.get(timeout=5)
            except Empty:
                break

            client_id = item.get("client_id", "anonymous")
            generation = item.get("generation", 0)
            state = self.get_client_state(client_id)
            
            if state.generation != generation:
                continue

            with self._lock:
                state.active_threads += 1

            if idx >= len(state.cards):
                with self._lock:
                    state.active_threads -= 1
                continue
            card = state.cards[idx]
            card.mark_downloading()

            try:
                url = item["download_url"]
                method = item.get("method", "")
                filename = item["filename"]
                target_dir = item.get("target_dir", state.output_dir)
                filepath = os.path.join(target_dir, filename)
                part_path = filepath + ".part"

                if method == "TELEGRAM" or "tgseed.link" in url:
                    self.start_telegram_manual(idx, client_id=client_id)
                    continue

                if os.path.exists(filepath):
                    file_size = os.path.getsize(filepath)
                    with self._lock:
                        if state.generation == generation:
                            state.done_count += 1
                            state.active_threads -= 1
                    card.set_detail(f"Already downloaded ({file_size / (1024 * 1024):.1f} MB)")
                    card.mark_done()
                    continue

                resume_from = 0
                if os.path.exists(part_path):
                    resume_from = os.path.getsize(part_path)

                headers = {}
                if resume_from > 0:
                    headers["Range"] = f"bytes={resume_from}-"
                    card.set_detail(f"Resuming from {resume_from / (1024 * 1024):.1f} MB…")

                resp = SESSION.get(url, stream=True, timeout=300, headers=headers)

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
                            speed = (downloaded - resume_from) / elapsed if elapsed > 0 else 0
                            pct = downloaded / total if total else 0

                            mb_down = downloaded / (1024 * 1024)
                            speed_str = self._fmt_speed(speed)
                            if total:
                                mb_total = total / (1024 * 1024)
                                remaining = (total - downloaded) / speed if speed > 0 else 0
                                eta_str = self._fmt_time(remaining)
                                detail = f"{mb_down:.1f}/{mb_total:.1f} MB  •  {speed_str}  •  ETA {eta_str}"
                            else:
                                detail = f"{mb_down:.1f} MB  •  {speed_str}"

                            card.set_progress(pct)
                            card.set_detail(detail)

                os.rename(part_path, filepath)

                total_mb = (total or downloaded) / (1024 * 1024)
                elapsed = time.time() - start_time
                avg = self._fmt_speed((downloaded - resume_from) / elapsed) if elapsed > 0 else "–"

                with self._lock:
                    if state.generation == generation:
                        state.done_count += 1
                card.mark_done()
                card.set_detail(f"{total_mb:.1f} MB  •  avg {avg}")

            except Exception as e:
                with self._lock:
                    if state.generation == generation:
                        state.fail_count += 1
                card.mark_failed(str(e))

            finally:
                with self._lock:
                    state.active_threads -= 1

    def _fmt_speed(self, bytes_per_sec: float) -> str:
        if bytes_per_sec <= 0:
            return "0 B/s"
        for unit in ("B/s", "KB/s", "MB/s", "GB/s"):
            if bytes_per_sec < 1000 or unit == "GB/s":
                return f"{bytes_per_sec:.1f} {unit}"
            bytes_per_sec /= 1000
        return f"{bytes_per_sec:.1f} B/s"

    def _fmt_time(self, seconds: float) -> str:
        if seconds <= 0:
            return "0s"
        s = int(seconds)
        h = s // 3600
        s %= 3600
        m = s // 60
        s %= 60
        if h > 0:
            return f"{h}h {m}m"
        if m > 0:
            return f"{m}m {s}s"
        return f"{s}s"

    def resolve_card_retry(self, idx, link, client_id=None):
        """Re-resolve a single link that failed during the initial resolution phase (manual retry, unlimited)."""
        state = self.get_client_state(client_id)
        if idx >= len(state.cards):
            return
        card = state.cards[idx]
        
        # Decrement fail_count since we're transitioning from failed to retrying
        if card.state == 3:
            with self._lock:
                state.fail_count = max(0, state.fail_count - 1)
        
        card.retry_count += 1
        card.mark_pending()
        card.set_status("Re-resolving…")
        card.set_progress(0.15)
        
        generation = state.generation
        
        def _task():
            try:
                card.set_status("Bypassing shortener…")
                card.set_progress(0.15)
                _, name, ds_url = resolve_link(idx, link, session=SESSION)
                
                # Smooth animation for Phase 1 (Shortener bypass)
                import random
                p1_dur = 0.5 + random.uniform(-0.15, 0.15)
                p1_steps = 5
                for s in range(p1_steps):
                    if state.generation != generation:
                        return
                    t = (s + 1) / p1_steps
                    val = 0.15 + (0.45 - 0.15) * t + random.uniform(-0.03, 0.03)
                    card.set_progress(min(0.48, max(0.15, val)))
                    time.sleep(p1_dur / p1_steps)
                
                source_link = dict(link) if isinstance(link, dict) else None
                size_hint = parse_size_hint_bytes(name)
                origin_url = link.get("url", "") if isinstance(link, dict) else ""
                origin_meta_size = None
                if "driveseed.org" in origin_url:
                    _, origin_meta_size = get_driveseed_file_metadata(origin_url)

                expected_size = origin_meta_size or size_hint

                if ds_url and "tgseed.link" in ds_url:
                    fname = name or f"Link {idx + 1}"
                    fname = re.sub(r'[<>:"/\\|?*]', "_", fname)
                    item = {
                        "client_id": state.client_id,
                        "generation": generation,
                        "filename": fname,
                        "download_url": ds_url,
                        "method": "TELEGRAM",
                        "target_dir": state.output_dir,
                        "expected_size_bytes": expected_size,
                        "source_link": source_link,
                        "source_index": idx,
                        "source_name_hint": name,
                        "source_driveseed_url": None,
                    }
                    # Smooth final fill animation before finalizing
                    p3_dur = 0.4 + random.uniform(-0.1, 0.1)
                    p3_steps = 4
                    start_p = card.progress or 0.45
                    for s in range(p3_steps):
                        if state.generation != generation:
                            return
                        t = (s + 1) / p3_steps
                        val = start_p + (0.98 - start_p) * t + random.uniform(-0.02, 0.02)
                        card.set_progress(min(0.99, max(start_p, val)))
                        time.sleep(p3_dur / p3_steps)
                elif ds_url and ".r2.dev/" in ds_url:
                    fname = os.path.basename(urlparse(ds_url).path) or name or f"download_{idx + 1}"
                    fname = re.sub(r'[<>:"/\\|?*]', "_", fname)
                    item = {
                        "client_id": state.client_id,
                        "generation": generation,
                        "filename": fname,
                        "download_url": ds_url,
                        "method": "CLOUD",
                        "target_dir": state.output_dir,
                        "expected_size_bytes": expected_size,
                        "source_link": source_link,
                        "source_index": idx,
                        "source_name_hint": name,
                        "source_driveseed_url": None,
                    }
                    # Smooth final fill animation before finalizing
                    p3_dur = 0.4 + random.uniform(-0.1, 0.1)
                    p3_steps = 4
                    start_p = card.progress or 0.45
                    for s in range(p3_steps):
                        if state.generation != generation:
                            return
                        t = (s + 1) / p3_steps
                        val = start_p + (0.98 - start_p) * t + random.uniform(-0.02, 0.02)
                        card.set_progress(min(0.99, max(start_p, val)))
                        time.sleep(p3_dur / p3_steps)
                elif not ds_url or "driveseed.org" not in ds_url:
                    raise ValueError("Not a driveseed link")
                else:
                    card.set_status("Resolving driveseed…")
                    
                    # Smooth animation for Phase 2
                    p2_dur = 0.5 + random.uniform(-0.15, 0.15)
                    p2_steps = 5
                    start_p = card.progress or 0.45
                    for s in range(p2_steps):
                        if state.generation != generation:
                            return
                        t = (s + 1) / p2_steps
                        val = start_p + (0.80 - start_p) * t + random.uniform(-0.03, 0.03)
                        card.set_progress(min(0.83, max(start_p, val)))
                        time.sleep(p2_dur / p2_steps)

                    dl_url, ds_fname, ds_size, method = resolve_driveseed(ds_url)
                    meta_size = ds_size or expected_size
                    fname = ds_fname or name or os.path.basename(urlparse(dl_url).path) or f"download_{idx + 1}"
                    fname = re.sub(r'[<>:"/\\|?*]', "_", fname)
                    item = {
                        "client_id": state.client_id,
                        "generation": generation,
                        "filename": fname,
                        "download_url": dl_url,
                        "method": method,
                        "target_dir": state.output_dir,
                        "expected_size_bytes": meta_size,
                        "source_link": source_link,
                        "source_index": idx,
                        "source_name_hint": name,
                        "source_driveseed_url": ds_url,
                    }
                    
                    # Phase 3: Final fill animation before finalizing
                    p3_dur = 0.4 + random.uniform(-0.1, 0.1)
                    p3_steps = 4
                    start_p = card.progress or 0.80
                    for s in range(p3_steps):
                        if state.generation != generation:
                            return
                        t = (s + 1) / p3_steps
                        val = start_p + (0.98 - start_p) * t + random.uniform(-0.02, 0.02)
                        card.set_progress(min(0.99, max(start_p, val)))
                        time.sleep(p3_dur / p3_steps)

                card.filename = item["filename"]
                card.set_method(item.get("method", ""))
                card.item_data = item
                card.url = item.get("download_url", "")

                existing = set(os.listdir(state.output_dir)) if os.path.isdir(state.output_dir) else set()
                if item["filename"] in existing:
                    card.set_detail("Already downloaded")
                    card.mark_done()
                    with self._lock:
                        if state.generation == generation:
                            state.done_count += 1
                else:
                    if item.get("method") == "TELEGRAM":
                        card.set_status("Manual Telegram")
                        exp = item.get("expected_size_bytes")
                        card.set_detail(f"Click Download to open Telegram Desktop. Expected: {fmt_bytes(exp)}" if exp else "Click Download to open Telegram Desktop.")
                        card.set_action("Download", lambda index=idx: self.start_telegram_manual(index, client_id=state.client_id))
                    else:
                        if self.cloud_mode:
                            card.status = "✓ Ready"
                            card.state = 2
                            card.progress = 1.0
                            card.detail = "Direct link resolved! Click 'Download to Device' below."
                            with self._lock:
                                if state.generation == generation:
                                    state.done_count += 1
                        else:
                            card.set_status("Queued")
                            self.download_queue.put((idx, item))
                            with self._lock:
                                if state.active_threads < MAX_CONCURRENT:
                                    threading.Thread(target=self._download_worker, daemon=True).start()
            except Exception as e:
                item = {
                    "client_id": state.client_id,
                    "generation": generation,
                    "filename": link.get("name") if isinstance(link, dict) else f"Failed link {idx + 1}",
                    "download_url": None,
                    "method": "",
                    "error": str(e),
                    "source_link": link,
                    "source_index": idx,
                }
                card.filename = item["filename"]
                card.set_method(item.get("method", ""))
                card.item_data = item
                card.mark_failed(str(e))
                with self._lock:
                    if state.generation == generation:
                        state.fail_count += 1
        
        threading.Thread(target=_task, daemon=True).start()

    # ── Advanced Telegram Manual Download logic ──
    def start_telegram_manual(self, idx, is_retry=False, client_id=None):
        state = self.get_client_state(client_id)
        if idx >= len(state.cards):
            return
        card = state.cards[idx]
        item = card.item_data or {}
        
        item["client_id"] = state.client_id
        item["generation"] = state.generation
        
        if is_retry:
            removed = self._cleanup_telegram_retry_artifacts(item)
            card.set_detail(f"Retrying… cleaned {removed} partial file(s).")
            card.set_progress(0)
            card.set_action("Retrying…")
        else:
            card.set_action("Waiting…")

        # Log when the manual Telegram download is clicked and triggered
        uid = state.client_id if state.client_id else "anonymous"
        active_title = getattr(self, 'active_title', 'Direct URL Input')
        clean_title = clean_log_title(active_title)
        fname = card.filename
        dl_url = item.get("download_url", "")
        
        event_msg = (
            f"👤 {uid:<10} | 🚀 TELEGRAM  | \"{clean_title}\"\n"
            f"├─► Episode: \"{fname}\"\n"
            f"└─► Telegram Link: {dl_url}"
        )
        log_instant_event(event_msg)

        item["attempted_manual"] = True
        threading.Thread(
            target=self._run_telegram_manual_download,
            args=(idx, item, is_retry),
            daemon=True,
        ).start()

    def _run_telegram_manual_download(self, idx, item, is_retry=False):
        client_id = item.get("client_id", "anonymous")
        generation = item.get("generation", 0)
        state = self.get_client_state(client_id)
        
        if state.generation != generation:
            return
            
        card = state.cards[idx]
        target_dir = item.get("target_dir", state.output_dir)
        watch_timeout_raw = os.getenv("TELEGRAM_WATCH_TIMEOUT", "").strip()
        watch_timeout = max(0, int(watch_timeout_raw)) if watch_timeout_raw else 0

        with self._lock:
            state.active_threads += 1

        try:
            watch_dirs = self._telegram_watch_dirs()
            existing_watch_dirs = [p for p in watch_dirs if p.is_dir()]
            if not existing_watch_dirs:
                raise FileNotFoundError("No Telegram Desktop download folder found.")

            baseline = self._snapshot_files(watch_dirs)
            started_at = time.time()

            if self._telegram_lock.locked():
                card.set_detail("Waiting for previous Telegram request to finish…")

            with self._telegram_lock:
                card.set_detail("Resolving Telegram deep protocol link…")
                from telegram_fallback import get_telegram_client_and_bot
                
                # Fetch Telegram credentials safely
                client, bot_username = get_telegram_client_and_bot()
                
                # Fetch target bot and start token
                url = item["download_url"]
                parsed = urlparse(unescape(url).strip())
                qs = parse_qs(parsed.query)
                start_param = qs.get("start", [""])[0].strip()
                bot_name = qs.get("bot", [bot_username])[0].lstrip("@").strip()

                if not start_param:
                    raise TelegramDownloadError("Invalid fallback url start param")

                deep_link = f"tg://resolve?domain={bot_name}&start={start_param}"
                card.set_detail("Launching Telegram app…")
                self._open_telegram_with_link(deep_link)

                card.set_action("Watching…")
                card.set_detail("Telegram opened. Click Start in your Telegram client…")

                # Progress loop looking for new file
                new_file = None
                size_hint = item.get("expected_size_bytes")
                
                while True:
                    if watch_timeout > 0 and (time.time() - started_at > watch_timeout):
                        raise TimeoutError(f"Timeout: click Start in Telegram within {watch_timeout}s")

                    current = self._snapshot_files(watch_dirs)
                    candidates = []
                    
                    for fpath, (sz, mt) in current.items():
                        if fpath not in baseline:
                            candidates.append((fpath, sz, mt))
                        else:
                            old_sz, _ = baseline[fpath]
                            if sz > old_sz:
                                candidates.append((fpath, sz, mt))

                    # Filter partial files (.part)
                    active_parts = [c for c in candidates if c[0].endswith(".part")]
                    if active_parts:
                        new_file = Path(active_parts[0][0])
                        break

                    # Check for quick complete files
                    completed_files = [c for c in candidates if not c[0].endswith(".part") and not c[0].endswith(".download")]
                    if completed_files:
                        new_file = Path(completed_files[0][0])
                        break

                    time.sleep(0.5)

                # Monitor download progress
                tg_debug(f"[Telegram Monitor] Found active download: {new_file}")
                is_part = new_file.suffix == ".part"
                actual_file = new_file.with_suffix("") if is_part else new_file
                
                last_size = 0
                last_time = time.time()

                while True:
                    if not new_file.exists() and not actual_file.exists():
                        raise FileNotFoundError("Downloading file vanished")

                    cur_size = 0
                    if new_file.exists():
                        cur_size = new_file.stat().st_size
                    elif actual_file.exists():
                        cur_size = actual_file.stat().st_size
                        if not is_part or (is_part and cur_size >= (size_hint or 0)):
                            break

                    now = time.time()
                    elapsed = now - last_time
                    speed = (cur_size - last_size) / elapsed if elapsed > 0 else 0
                    last_size = cur_size
                    last_time = now

                    if size_hint:
                        pct = min(0.99, cur_size / size_hint)
                        card.set_progress(pct)
                        card.set_detail(f"Downloading: {cur_size/(1024*1024):.1f}/{size_hint/(1024*1024):.1f} MB  •  {self._fmt_speed(speed)}")
                    else:
                        card.set_detail(f"Downloading: {cur_size/(1024*1024):.1f} MB  •  {self._fmt_speed(speed)}")

                    time.sleep(0.5)

                card.set_progress(0.99)
                card.set_detail("Copying completed Telegram file to downloads directory…")

                # Move downloaded file to target
                dest_path = self._pick_dest_path(target_dir, actual_file.name, item.get("source_name_hint"))
                shutil.copy2(actual_file, dest_path)
                
                try:
                    actual_file.unlink()
                except Exception:
                    pass

                with self._lock:
                    if state.generation == generation:
                        state.done_count += 1
                card.mark_done()
                card.set_detail(f"{os.path.getsize(dest_path)/(1024*1024):.1f} MB  •  avg Telegram")

        except Exception as e:
            with self._lock:
                if state.generation == generation:
                    state.fail_count += 1
            card.mark_failed(str(e))
            card.set_action("Retry", lambda index=idx: self.start_telegram_manual(index, is_retry=True, client_id=client_id))

        finally:
            with self._lock:
                state.active_threads -= 1

    def _cleanup_telegram_retry_artifacts(self, item) -> int:
        watch_dirs = self._telegram_watch_dirs()
        name_hint = item.get("source_name_hint", "")
        if not name_hint:
            return 0
        
        base_name, _ = os.path.splitext(name_hint)
        removed = 0
        
        for w in watch_dirs:
            if not w.is_dir():
                continue
            for entry in w.iterdir():
                if entry.is_file() and base_name in entry.name:
                    if entry.suffix in (".part", ".download") or entry.name == name_hint:
                        try:
                            entry.unlink()
                            removed += 1
                        except Exception:
                            pass
        return removed

    @staticmethod
    def _telegram_watch_dirs() -> list[Path]:
        dirs = []
        raw = os.getenv("TELEGRAM_DESKTOP_DOWNLOAD_DIR", "").strip()
        if raw:
            for part in re.split(r"[:,]", raw):
                p = part.strip()
                if p:
                    dirs.append(Path(p).expanduser())
        dirs.append(Path("~/Downloads/Telegram Desktop").expanduser())
        dirs.append(Path("~/Downloads").expanduser())
        dirs.append(Path("~/.var/app/org.telegram.desktop/data/TelegramDesktop/tdata/temp_data").expanduser())
        dirs.append(Path("~/.var/app/org.telegram.desktop/data/TelegramDesktop").expanduser())

        unique = []
        seen = set()
        for p in dirs:
            key = str(p)
            if key not in seen:
                seen.add(key)
                unique.append(p)
        return unique

    @staticmethod
    def _snapshot_files(paths: list[Path]) -> dict[str, tuple[int, float]]:
        snap = {}
        seen = set()
        for base in paths:
            if not base.is_dir():
                continue
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
            except Exception:
                continue
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
    def _is_telegram_running() -> bool:
        try:
            result = subprocess.run(["pgrep", "-f", "-i", "telegram"], capture_output=True, timeout=2)
            return result.returncode == 0
        except Exception:
            return False

    @classmethod
    def _open_telegram_with_link(cls, deep_link: str) -> None:
        if cls._is_telegram_running():
            subprocess.run(["xdg-open", deep_link], capture_output=True, timeout=5)
        else:
            try:
                subprocess.Popen(["telegram-desktop"])
            except Exception:
                subprocess.Popen(["telegram"])
            time.sleep(5)
            subprocess.run(["xdg-open", deep_link], capture_output=True, timeout=5)


# Global Download Manager reference
DOWNLOAD_MGR = DownloaderBackend()

TRENDING_CACHE_FILE = os.path.join('static', 'trending_cache.json')
TRENDING_CACHE = {
    "data": None,
    "timestamp": 0
}

def load_trending_cache_from_file():
    """Load cached trending movies from the local JSON file on startup."""
    global TRENDING_CACHE
    try:
        if os.path.exists(TRENDING_CACHE_FILE):
            with open(TRENDING_CACHE_FILE, 'r', encoding='utf-8') as f:
                loaded = json.load(f)
                if isinstance(loaded, dict) and "data" in loaded:
                    TRENDING_CACHE["data"] = loaded["data"]
                    TRENDING_CACHE["timestamp"] = loaded.get("timestamp", 0)
                    print(f"[+] Loaded {len(TRENDING_CACHE['data'])} trending movies from persistent local cache file.", flush=True)
    except Exception as e:
        print(f"[-] Failed to load local trending cache: {e}", flush=True)

def save_trending_cache_to_file():
    """Save the current in-memory cache to the local JSON file."""
    try:
        os.makedirs(os.path.dirname(TRENDING_CACHE_FILE), exist_ok=True)
        with open(TRENDING_CACHE_FILE, 'w', encoding='utf-8') as f:
            json.dump(TRENDING_CACHE, f, indent=2, ensure_ascii=False)
        print("[+] Saved trending movies cache to persistent local cache file.", flush=True)
    except Exception as e:
        print(f"[-] Failed to save trending cache to file: {e}", flush=True)

def force_refresh_trending_cache():
    """Fetch new trending movies from sources, optimize/cache their thumbnails, and update the cache."""
    try:
        from movie_search import fetch_trending_movies
        print("[*] Performing scheduled refresh of trending movies showcase...", flush=True)
        data = fetch_trending_movies()
        if data:
            TRENDING_CACHE["data"] = data
            TRENDING_CACHE["timestamp"] = time.time()
            save_trending_cache_to_file()
            print("[+] Showcase cache successfully updated & persisted!", flush=True)
            
            # Pre-cache & optimize thumbnails in the background so they are ready before the first user load!
            def pre_cache_thumbnails():
                print("[*] Pre-caching & optimizing movie thumbnails in the background...", flush=True)
                for item in data:
                    thumb_url = item.get("thumbnail")
                    if not thumb_url:
                        continue
                    try:
                        url_hash = hashlib.md5(thumb_url.encode('utf-8')).hexdigest()
                        cache_path = os.path.join('static', 'thumbnail_cache', f"{url_hash}.webp")
                        # If not already cached, pre-fetch and optimize it!
                        if not os.path.exists(cache_path):
                            from PIL import Image
                            import requests
                            import io
                            headers = {'User-Agent': 'Mozilla/5.0'}
                            resp = requests.get(thumb_url, headers=headers, timeout=8)
                            if resp.status_code == 200:
                                os.makedirs(os.path.dirname(cache_path), exist_ok=True)
                                img = Image.open(io.BytesIO(resp.content))
                                if img.mode not in ('RGB', 'RGBA'):
                                    img = img.convert('RGB')
                                max_width = 320
                                if img.width > max_width:
                                    ratio = max_width / float(img.width)
                                    new_height = int(float(img.height) * float(ratio))
                                    img = img.resize((max_width, new_height), Image.Resampling.LANCZOS)
                                img.save(cache_path, 'WEBP', quality=80)
                    except Exception as te:
                        print(f"[-] Failed to pre-cache thumbnail {thumb_url}: {te}", flush=True)
                print("[+] Thumbnails pre-caching finished successfully!", flush=True)
            
            threading.Thread(target=pre_cache_thumbnails, daemon=True).start()
            
    except Exception as e:
        print(f"[-] Showcase automatic refresh failed: {e}", flush=True)

def start_cache_scheduler():
    """Start a background scheduler thread that refreshes the cache every midnight (IST/local)."""
    import datetime
    # 1. Load caches from file if they exist
    load_trending_cache_from_file()
    load_imdb_suggest_cache_from_file()

    # 2. If no cache data is present or cache is older than 1 day, trigger initial fetch immediately
    now = time.time()
    if not TRENDING_CACHE["data"] or (now - TRENDING_CACHE["timestamp"] > 86400):
        print("[*] Cache missing or older than 1 day. Triggering initial fetch...", flush=True)
        threading.Thread(target=force_refresh_trending_cache, daemon=True).start()

    # 3. Scheduler loop targeting midnight
    def scheduler_loop():
        import datetime
        while True:
            # Calculate seconds until next midnight
            now_dt = datetime.datetime.now()
            tomorrow_dt = now_dt + datetime.timedelta(days=1)
            midnight_dt = datetime.datetime(tomorrow_dt.year, tomorrow_dt.month, tomorrow_dt.day, 0, 0, 10) # 10 seconds past midnight
            seconds_until_midnight = (midnight_dt - now_dt).total_seconds()
            
            print(f"[+] Cache Scheduler: Next midnight refresh in {seconds_until_midnight:.1f} seconds (~{seconds_until_midnight/3600:.2f} hours).", flush=True)
            
            # Sleep until midnight (or max 1 hour at a time to keep thread responsive)
            sleep_duration = min(seconds_until_midnight, 3600)
            time.sleep(sleep_duration)
            
            # Re-check if we reached midnight
            now_now = datetime.datetime.now()
            if now_now.hour == 0 and now_now.minute == 0:
                print("[*] Midnight reached! Purging cache and starting fresh scheduled fetch...", flush=True)
                force_refresh_trending_cache()
                time.sleep(65) # make sure we don't double trigger in the same minute

    threading.Thread(target=scheduler_loop, daemon=True).start()

def get_cached_trending():
    # If we have cached data, serve it instantly! (0ms response time)
    if TRENDING_CACHE["data"]:
        return TRENDING_CACHE["data"]
        
    # Return placeholder items as fallback so UI renders immediately
    return [
        {
            "title": "Deadpool & Wolverine (2024) [Multi-Audio] [1080p]",
            "url": "https://moviesmod.money",
            "thumbnail": "https://image.tmdb.org/t/p/w500/8cd70bC3gwYZ2nseXPRw6786IEy.jpg",
            "category": "HOLLYWOOD"
        },
        {
            "title": "The Boys - Season 4 [Dual-Audio] [720p]",
            "url": "https://moviesmod.money",
            "thumbnail": "https://image.tmdb.org/t/p/w500/29n7mq4Hn76IR65U5gB49vH7GQR.jpg",
            "category": "HOLLYWOOD"
        },
        {
            "title": "Kalki 2898 AD (2024) [Hindi-DD5.1] [1080p]",
            "url": "https://moviesleech.rodeo",
            "thumbnail": "https://image.tmdb.org/t/p/w500/czhy5HnS691Vj6SjFfC7lS4N93f.jpg",
            "category": "BOLLYWOOD"
        },
        {
            "title": "Demon Slayer: Hashira Training Arc [Dual-Audio] [1080p]",
            "url": "https://animeflix.dad",
            "thumbnail": "https://image.tmdb.org/t/p/w500/xOMo8NETf7Phlx636EvVNs8fgZ0.jpg",
            "category": "ANIMEFLIX"
        }
    ]



# ── Threaded HTTP Request Handler & API Router ───────────────────────────

ADMIN_SESSIONS = set()

def parse_log_events(lines):
    events = []
    current_event = None
    for line in lines:
        if line.startswith('['):
            if current_event:
                events.append(current_event)
            m = re.match(r'^\[(.*?)\]\s*(.*)', line)
            if m:
                ts = m.group(1)
                first_line = m.group(2)
            else:
                ts = ""
                first_line = line
            current_event = {
                "timestamp": ts,
                "first_line": first_line,
                "sublines": []
            }
        else:
            if current_event:
                current_event["sublines"].append(line.strip())
    if current_event:
        events.append(current_event)
        
    parsed_events = []
    for ev in events:
        first_line = ev["first_line"]
        # Split by "|"
        parts = [p.strip() for p in first_line.split('|')]
        
        username = "anonymous"
        action = "SYSTEM"
        title = ""
        
        if len(parts) >= 3:
            username = parts[0].replace('👤', '').strip()
            action = parts[1].strip()
            title = parts[2].strip()
            if title.startswith('"') and title.endswith('"'):
                title = title[1:-1]
        elif len(parts) == 2:
            username = parts[0].replace('👤', '').strip()
            action = parts[1].strip()
        else:
            title = first_line
            
        # Clean sublines (remove leading tab and tree drawing arrows like ├─►, └─►)
        cleaned_sublines = []
        for sl in ev["sublines"]:
            cleaned = sl.strip()
            cleaned = re.sub(r'^[├└]─►\s*', '', cleaned)
            if cleaned:
                cleaned_sublines.append(cleaned)
                
        parsed_events.append({
            "timestamp": ev["timestamp"],
            "username": username,
            "action": action,
            "title": title,
            "sublines": cleaned_sublines
        })
    # Reverse so latest events are at the top
    parsed_events.reverse()
    return parsed_events

class APIRequestHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        # Silence annoying standard console access logs
        pass

    def send_json(self, data, status=200):
        try:
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(json.dumps(data).encode('utf-8'))
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass  # Client closed the connection early (e.g. AbortController) — safe to ignore

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.end_headers()

    def serve_login_page(self, redirect_to):
        html_code = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
    <title>MoviesCrackd - Admin Login</title>
    <link href="https://fonts.googleapis.com/css2?family=Outfit:wght@300;400;600;700&display=swap" rel="stylesheet">
    <style>
        :root {{
            --bg-color: #0b0f19;
            --panel-bg: rgba(17, 24, 39, 0.7);
            --border-color: rgba(255, 255, 255, 0.08);
            --accent-glow: linear-gradient(135deg, #a855f7, #6366f1);
            --text-main: #f3f4f6;
            --text-sub: #9ca3af;
        }}
        * {{
            box-sizing: border-box;
            margin: 0;
            padding: 0;
            font-family: 'Outfit', sans-serif;
            -webkit-user-select: none;
            user-select: none;
        }}
        html, body {{
            height: 100%;
            overflow: hidden;
            background-color: var(--bg-color);
            display: flex;
            align-items: center;
            justify-content: center;
        }}
        .ambient-glow {{
            position: absolute;
            width: 350px;
            height: 350px;
            border-radius: 50%;
            background: radial-gradient(circle, rgba(168, 85, 247, 0.15) 0%, rgba(99, 102, 241, 0.05) 70%, transparent 100%);
            filter: blur(50px);
            z-index: 0;
        }}
        .login-card {{
            position: relative;
            z-index: 10;
            width: 90%;
            max-width: 400px;
            padding: 40px 30px;
            background: var(--panel-bg);
            border: 1px solid var(--border-color);
            border-radius: 24px;
            box-shadow: 0 20px 40px rgba(0,0,0,0.4);
            backdrop-filter: blur(16px);
            -webkit-backdrop-filter: blur(16px);
            text-align: center;
            animation: fadeIn 0.6s cubic-bezier(0.16, 1, 0.3, 1);
        }}
        @keyframes fadeIn {{
            from {{ opacity: 0; transform: translateY(20px); }}
            to {{ opacity: 1; transform: translateY(0); }}
        }}
        .logo-mark {{
            font-size: 40px;
            margin-bottom: 15px;
            display: inline-block;
        }}
        h2 {{
            color: var(--text-main);
            font-weight: 700;
            font-size: 24px;
            margin-bottom: 8px;
        }}
        .subtitle {{
            color: var(--text-sub);
            font-size: 14px;
            margin-bottom: 30px;
        }}
        .form-group {{
            text-align: left;
            margin-bottom: 20px;
        }}
        label {{
            display: block;
            color: var(--text-sub);
            font-size: 12px;
            font-weight: 600;
            text-transform: uppercase;
            letter-spacing: 0.05em;
            margin-bottom: 8px;
        }}
        input {{
            width: 100%;
            padding: 14px 16px;
            background: rgba(255,255,255,0.03);
            border: 1px solid var(--border-color);
            border-radius: 12px;
            color: var(--text-main);
            font-size: 15px;
            outline: none;
            transition: all 0.3s ease;
            -webkit-user-select: text;
            user-select: text;
        }}
        input:focus {{
            border-color: #a855f7;
            background: rgba(255,255,255,0.05);
            box-shadow: 0 0 10px rgba(168, 85, 247, 0.2);
        }}
        .login-btn {{
            width: 100%;
            padding: 14px;
            margin-top: 15px;
            background: var(--accent-glow);
            border: none;
            border-radius: 12px;
            color: #ffffff;
            font-size: 16px;
            font-weight: 700;
            cursor: pointer;
            transition: transform 0.2s ease, opacity 0.2s ease;
            box-shadow: 0 4px 15px rgba(168, 85, 247, 0.3);
        }}
        .login-btn:active {{
            transform: scale(0.98);
        }}
        .login-btn:hover {{
            opacity: 0.95;
        }}
        .error-banner {{
            display: none;
            background: rgba(239, 68, 68, 0.15);
            border: 1px solid rgba(239, 68, 68, 0.3);
            color: #fca5a5;
            padding: 12px;
            border-radius: 12px;
            font-size: 13px;
            margin-bottom: 20px;
            animation: shake 0.3s ease;
        }}
        @keyframes shake {{
            0%, 100% {{ transform: translateX(0); }}
            25% {{ transform: translateX(-5px); }}
            75% {{ transform: translateX(5px); }}
        }}
    </style>
</head>
<body>
    <div class="ambient-glow"></div>
    <div class="login-card">
        <svg class="lock-icon" viewBox="0 0 24 24" width="48" height="48" fill="none" stroke="url(#lock-grad)" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" style="margin-bottom: 20px; display: inline-block;">
            <defs>
                <linearGradient id="lock-grad" x1="0%" y1="0%" x2="100%" y2="100%">
                    <stop offset="0%" stop-color="#a855f7" />
                    <stop offset="100%" stop-color="#6366f1" />
                </linearGradient>
            </defs>
            <rect x="3" y="11" width="18" height="11" rx="2" ry="2" />
            <path d="M7 11V7a5 5 0 0 1 10 0v4" />
        </svg>
        <h2>Admin Authentication</h2>
        <p class="subtitle">Access is restricted to system administrators</p>
        
        <div id="error-banner" class="error-banner">Invalid username or password.</div>
        
        <form id="login-form">
            <div class="form-group">
                <label for="username">Username</label>
                <input type="text" id="username" required autocomplete="username">
            </div>
            <div class="form-group">
                <label for="password">Password</label>
                <input type="password" id="password" required autocomplete="current-password">
            </div>
            <button type="submit" class="login-btn">Secure Login</button>
        </form>
    </div>

    <script>
        document.getElementById('login-form').addEventListener('submit', function(e) {{
            e.preventDefault();
            const user = document.getElementById('username').value;
            const pass = document.getElementById('password').value;
            const banner = document.getElementById('error-banner');
            
            banner.style.display = 'none';
            
            fetch('/api/admin/login', {{
                method: 'POST',
                headers: {{ 'Content-Type': 'application/json' }},
                body: JSON.stringify({{ username: user, password: pass }})
            }})
            .then(res => res.json())
            .then(data => {{
                if (data.status === 'success') {{
                    window.location.href = "{redirect_to}";
                }} else {{
                    banner.style.display = 'block';
                }}
            }})
            .catch(() => {{
                banner.style.display = 'block';
            }});
        }});
    </script>
</body>
</html>"""
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Cache-Control', 'no-cache, no-store, must-revalidate')
        self.end_headers()
        self.wfile.write(html_code.encode('utf-8'))

    def serve_stats_page(self):
        self.send_response(302)
        self.send_header('Location', '/admin')
        self.end_headers()
        return
        html_code = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
    <title>MoviesCrackd - Premium Diagnostics</title>
    <link href="https://fonts.googleapis.com/css2?family=Outfit:wght@300;400;500;600;700&display=swap" rel="stylesheet">
    <style>
        :root {
            --bg-color: #0b0f19;
            --panel-bg: rgba(17, 24, 39, 0.7);
            --border-color: rgba(255, 255, 255, 0.08);
            --accent-glow: linear-gradient(135deg, #a855f7, #6366f1);
            --text-main: #f3f4f6;
            --text-sub: #9ca3af;
            --blue: #3b82f6;
            --crimson: #ef4444;
            --emerald: #10b981;
        }
        * {
            box-sizing: border-box;
            margin: 0;
            padding: 0;
            font-family: 'Outfit', sans-serif;
            -webkit-user-select: none;
            user-select: none;
        }
        html, body {
            height: 100%;
            overflow: hidden;
            background-color: var(--bg-color);
            display: flex;
            align-items: center;
            justify-content: center;
        }
        .ambient-glow {
            position: absolute;
            width: 450px;
            height: 450px;
            border-radius: 50%;
            background: radial-gradient(circle, rgba(168, 85, 247, 0.12) 0%, rgba(99, 102, 241, 0.04) 70%, transparent 100%);
            filter: blur(60px);
            z-index: 0;
        }
        .stats-card {
            position: relative;
            z-index: 10;
            width: 90%;
            max-width: 550px;
            padding: 35px 30px;
            background: var(--panel-bg);
            border: 1px solid var(--border-color);
            border-radius: 24px;
            box-shadow: 0 20px 45px rgba(0,0,0,0.5);
            backdrop-filter: blur(20px);
            -webkit-backdrop-filter: blur(20px);
            animation: scaleIn 0.5s cubic-bezier(0.16, 1, 0.3, 1);
        }
        @keyframes scaleIn {
            from { opacity: 0; transform: scale(0.95); }
            to { opacity: 1; transform: scale(1); }
        }
        .stats-header {
            display: flex;
            align-items: center;
            justify-content: space-between;
            margin-bottom: 25px;
            border-bottom: 1px solid var(--border-color);
            padding-bottom: 15px;
        }
        .stats-header h2 {
            color: var(--text-main);
            font-size: 22px;
            font-weight: 700;
            display: flex;
            align-items: center;
            gap: 10px;
        }
        .close-btn {
            background: rgba(255,255,255,0.05);
            border: 1px solid var(--border-color);
            color: var(--text-sub);
            padding: 6px 12px;
            border-radius: 8px;
            cursor: pointer;
            font-size: 13px;
            font-weight: 600;
            transition: all 0.2s ease;
        }
        .close-btn:hover {
            background: rgba(255,255,255,0.1);
            color: var(--text-main);
        }
        .stats-grid {
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 16px;
            margin-bottom: 25px;
        }
        .stat-item {
            background: rgba(255, 255, 255, 0.02);
            border: 1px solid var(--border-color);
            padding: 15px;
            border-radius: 16px;
            display: flex;
            flex-direction: column;
            gap: 6px;
        }
        .stat-label {
            color: var(--text-sub);
            font-size: 12px;
            font-weight: 600;
            text-transform: uppercase;
            letter-spacing: 0.05em;
        }
        .stat-val {
            color: var(--text-main);
            font-size: 17px;
            font-weight: 700;
        }
        .stat-val.active-color {
            color: #a855f7;
        }
        .actions-row {
            display: flex;
            gap: 12px;
            margin-bottom: 15px;
        }
        .action-btn {
            flex: 1;
            padding: 12px;
            border: 1px solid var(--border-color);
            border-radius: 12px;
            font-size: 14px;
            font-weight: 600;
            cursor: pointer;
            display: flex;
            align-items: center;
            justify-content: center;
            gap: 8px;
            transition: all 0.2s ease;
        }
        .action-btn.server-btn {
            background: rgba(239, 68, 68, 0.1);
            border-color: rgba(239, 68, 68, 0.2);
            color: #fca5a5;
        }
        .action-btn.server-btn:hover:not(:disabled) {
            background: rgba(239, 68, 68, 0.2);
        }
        .action-btn.client-btn {
            background: rgba(59, 130, 246, 0.1);
            border-color: rgba(59, 130, 246, 0.2);
            color: #93c5fd;
        }
        .action-btn.client-btn:hover:not(:disabled) {
            background: rgba(59, 130, 246, 0.2);
        }
        .action-btn:active {
            transform: scale(0.98);
        }
        .action-btn:disabled {
            opacity: 0.5;
            cursor: not-allowed;
        }
        .view-logs-btn {
            width: 100%;
            padding: 14px;
            background: var(--accent-glow);
            border: none;
            border-radius: 12px;
            color: #ffffff;
            font-size: 15px;
            font-weight: 700;
            cursor: pointer;
            display: flex;
            align-items: center;
            justify-content: center;
            gap: 8px;
            transition: all 0.2s ease;
            box-shadow: 0 4px 15px rgba(168, 85, 247, 0.2);
        }
        .view-logs-btn:hover {
            opacity: 0.95;
        }
        .view-logs-btn:active {
            transform: scale(0.98);
        }
    </style>
</head>
<body>
    <div class="ambient-glow"></div>
    <div class="stats-card">
        <div class="stats-header">
            <h2>📊 System Stats & Diagnostics</h2>
            <button class="close-btn" onclick="window.location.href='/'">Go Home</button>
        </div>
        
        <div class="stats-grid">
            <div class="stat-item">
                <span class="stat-label">Project Storage (SSD)</span>
                <span class="stat-val" id="diag-project-size">Calculating...</span>
            </div>
            <div class="stat-item">
                <span class="stat-label">Server Autocomplete Cache</span>
                <span class="stat-val" id="diag-server-suggest">Calculating...</span>
            </div>
            <div class="stat-item">
                <span class="stat-label">Cached Poster Thumbnails</span>
                <span class="stat-val" id="diag-server-posters">Calculating...</span>
            </div>
            <div class="stat-item">
                <span class="stat-label">Showcase Trending Cache</span>
                <span class="stat-val" id="diag-trending-cache">Calculating...</span>
            </div>
            <div class="stat-item">
                <span class="stat-label">Client Suggestion Cache</span>
                <span class="stat-val" id="diag-client-suggest">Calculating...</span>
            </div>
            <div class="stat-item">
                <span class="stat-label">Client Browser Storage</span>
                <span class="stat-val" id="diag-client-size">Calculating...</span>
            </div>
            <div class="stat-item" style="grid-column: 1 / -1;">
                <span class="stat-label">Server Date & Time (Local)</span>
                <span class="stat-val active-color" id="diag-server-time">Calculating...</span>
            </div>
        </div>
        
        <div class="actions-row">
            <button class="action-btn server-btn" id="server-btn" onclick="clearServerCache()">🧹 Clear Server SSD</button>
            <button class="action-btn client-btn" onclick="clearClientCache()">🗑 Clear Client Cache</button>
        </div>
        
        <button class="view-logs-btn" onclick="window.location.href='/admin'">📋 Open System Activity Logs</button>
    </div>

    <script>
        function loadStats() {
            fetch('/api/storage-stats')
                .then(res => {
                    if (res.status === 401) {
                        window.location.reload();
                        return;
                    }
                    return res.json();
                })
                .then(data => {
                    if (!data) return;
                    document.getElementById('diag-project-size').innerText = data.total_project_size;
                    document.getElementById('diag-server-suggest').innerText = `${data.imdb_suggest_cache_size} (${data.imdb_suggest_cache_count} q)`;
                    document.getElementById('diag-server-posters').innerText = `${data.cached_posters_size} (${data.cached_posters_count} posters)`;
                    document.getElementById('diag-trending-cache').innerText = data.trending_cache_size;
                    document.getElementById('diag-server-time').innerText = data.server_time;
                })
                .catch(err => console.error(err));

            try {
                const IMDB_SUGGESTIONS_CACHE_KEY = 'mcrackd_imdb_suggestions';
                const saved = localStorage.getItem(IMDB_SUGGESTIONS_CACHE_KEY);
                let suggestionsClientCache = saved ? JSON.parse(saved) : {};
                const clientSuggestCount = Object.keys(suggestionsClientCache).length;
                const serialized = JSON.stringify(suggestionsClientCache);
                const clientSuggestBytes = new TextEncoder().encode(serialized).length;
                
                let clientSuggestSize = `${(clientSuggestBytes / 1024).toFixed(2)} KB`;
                if (clientSuggestBytes >= 1048576) {
                    clientSuggestSize = `${(clientSuggestBytes / 1048576).toFixed(2)} MB`;
                }
                document.getElementById('diag-client-suggest').innerText = `${clientSuggestSize} (${clientSuggestCount} q)`;

                let totalLocalStorageBytes = 0;
                for (let i = 0; i < localStorage.length; i++) {
                    const key = localStorage.key(i);
                    const val = localStorage.getItem(key);
                    totalLocalStorageBytes += new TextEncoder().encode(key + val).length;
                }
                let totalLocalStorageSize = `${(totalLocalStorageBytes / 1024).toFixed(2)} KB`;
                if (totalLocalStorageBytes >= 1048576) {
                    totalLocalStorageSize = `${(totalLocalStorageBytes / 1048576).toFixed(2)} MB`;
                }
                document.getElementById('diag-client-size').innerText = totalLocalStorageSize;
            } catch (e) {
                document.getElementById('diag-client-suggest').innerText = '0 B';
                document.getElementById('diag-client-size').innerText = '0 B';
            }
        }

        function clearServerCache() {
            if (!confirm("Are you sure you want to permanently clear the server-side suggestions cache and delete all cached posters from the SSD?")) return;
            
            const btn = document.getElementById('server-btn');
            btn.innerText = "🧹 Clearing...";
            btn.disabled = true;

            fetch('/api/clear-server-cache')
                .then(res => res.json())
                .then(data => {
                    alert(data.message || "Server cache cleared successfully!");
                    loadStats();
                })
                .catch(err => alert("Failed to clear server cache: " + err))
                .finally(() => {
                    btn.innerText = "🧹 Clear Server SSD";
                    btn.disabled = false;
                });
        }

        function clearClientCache() {
            if (!confirm("Are you sure you want to delete all client-side browser suggestion caches?")) return;
            localStorage.removeItem('mcrackd_imdb_suggestions');
            alert("Client-side suggestions cache cleared successfully!");
            loadStats();
        }

        window.onload = loadStats;
    </script>
</body>
</html>"""
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Cache-Control', 'no-cache, no-store, must-revalidate')
        self.end_headers()
        self.wfile.write(html_code.encode('utf-8'))

    def serve_admin_page(self, logs_json):
        html_code = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
    <title>MoviesCrackd - Admin Panel</title>
    <link href="https://fonts.googleapis.com/css2?family=Outfit:wght@300;400;500;600;700&display=swap" rel="stylesheet">
    <style>
        :root {{
            --bg-color: #080b11;
            --panel-bg: rgba(17, 24, 39, 0.7);
            --border-color: rgba(255, 255, 255, 0.08);
            --accent-glow: linear-gradient(135deg, #a855f7, #6366f1);
            --text-main: #f3f4f6;
            --text-sub: #9ca3af;
        }}
        * {{
            box-sizing: border-box;
            margin: 0;
            padding: 0;
            font-family: 'Outfit', sans-serif;
            -webkit-user-select: none;
            user-select: none;
        }}
        html, body {{
            height: 100%;
            overflow: hidden;
            background-color: var(--bg-color);
            color: var(--text-main);
            display: flex;
            flex-direction: column;
        }}
        .ambient-glow {{
            position: absolute;
            width: 600px;
            height: 600px;
            border-radius: 50%;
            background: radial-gradient(circle, rgba(168, 85, 247, 0.08) 0%, rgba(99, 102, 241, 0.02) 70%, transparent 100%);
            filter: blur(80px);
            top: -200px;
            left: -200px;
            z-index: 0;
            pointer-events: none;
        }}
        .ambient-glow-2 {{
            position: absolute;
            width: 600px;
            height: 600px;
            border-radius: 50%;
            background: radial-gradient(circle, rgba(99, 102, 241, 0.08) 0%, rgba(168, 85, 247, 0.02) 70%, transparent 100%);
            filter: blur(80px);
            bottom: -200px;
            right: -200px;
            z-index: 0;
            pointer-events: none;
        }}
        header {{
            position: relative;
            z-index: 10;
            background: rgba(17, 24, 39, 0.75);
            border-bottom: 1px solid var(--border-color);
            padding: 15px 40px;
            display: flex;
            align-items: center;
            justify-content: space-between;
            backdrop-filter: blur(12px);
            -webkit-backdrop-filter: blur(12px);
            flex-shrink: 0;
        }}
        .header-left {{
            display: flex;
            align-items: center;
            gap: 15px;
        }}
        .header-left h1 {{
            font-size: 20px;
            font-weight: 700;
            display: flex;
            align-items: center;
            gap: 10px;
        }}
        .header-left .count-badge {{
            background: rgba(255,255,255,0.06);
            border: 1px solid var(--border-color);
            padding: 4px 10px;
            border-radius: 20px;
            font-size: 12px;
            font-weight: 600;
            color: var(--text-sub);
        }}
        .search-container {{
            flex: 1;
            max-width: 450px;
            margin: 0 30px;
            position: relative;
        }}
        .search-container input {{
            width: 100%;
            padding: 10px 16px 10px 40px;
            background: rgba(255,255,255,0.03);
            border: 1px solid var(--border-color);
            border-radius: 10px;
            color: var(--text-main);
            font-size: 14px;
            outline: none;
            transition: all 0.3s ease;
            -webkit-user-select: text;
            user-select: text;
        }}
        .search-container input:focus {{
            border-color: #a855f7;
            background: rgba(255,255,255,0.06);
            box-shadow: 0 0 10px rgba(168, 85, 247, 0.15);
        }}
        .search-container svg {{
            position: absolute;
            left: 14px;
            top: 50%;
            transform: translateY(-50%);
            width: 16px;
            height: 16px;
            fill: var(--text-sub);
            pointer-events: none;
        }}
        .header-right {{
            display: flex;
            align-items: center;
            gap: 12px;
        }}
        .btn {{
            background: rgba(255,255,255,0.05);
            border: 1px solid var(--border-color);
            color: var(--text-sub);
            padding: 8px 16px;
            border-radius: 8px;
            cursor: pointer;
            font-size: 13px;
            font-weight: 600;
            transition: all 0.2s ease;
            display: flex;
            align-items: center;
            gap: 6px;
        }}
        .btn:hover {{
            background: rgba(255,255,255,0.1);
            color: var(--text-main);
        }}
        .btn.accent {{
            background: var(--accent-glow);
            border: none;
            color: #ffffff;
            box-shadow: 0 4px 12px rgba(168, 85, 247, 0.2);
        }}
        .btn.accent:hover {{
            opacity: 0.95;
        }}
        .btn:active {{
            transform: scale(0.98);
        }}
        .filter-bar {{
            position: relative;
            z-index: 10;
            background: rgba(17, 24, 39, 0.4);
            border-bottom: 1px solid var(--border-color);
            padding: 10px 40px;
            display: flex;
            align-items: center;
            gap: 10px;
            flex-shrink: 0;
        }}
        .filter-pill {{
            padding: 6px 14px;
            border-radius: 20px;
            font-size: 12px;
            font-weight: 600;
            cursor: pointer;
            background: rgba(255,255,255,0.03);
            border: 1px solid var(--border-color);
            color: var(--text-sub);
            transition: all 0.2s ease;
        }}
        .filter-pill:hover {{
            background: rgba(255,255,255,0.06);
            color: var(--text-main);
        }}
        .filter-pill.active {{
            background: rgba(168, 85, 247, 0.15);
            border-color: rgba(168, 85, 247, 0.4);
            color: #f5f3ff;
        }}
        main {{
            flex: 1;
            overflow-y: auto;
            position: relative;
            z-index: 10;
            padding: 30px 40px;
        }}
        .logs-wrapper {{
            max-width: 1400px;
            margin: 0 auto;
            display: flex;
            flex-direction: column;
            gap: 14px;
        }}
        .log-card {{
            background: rgba(17, 24, 39, 0.45);
            border: 1px solid var(--border-color);
            border-radius: 16px;
            padding: 18px 24px;
            display: flex;
            align-items: flex-start;
            gap: 20px;
            transition: all 0.3s cubic-bezier(0.16, 1, 0.3, 1);
            backdrop-filter: blur(8px);
            -webkit-backdrop-filter: blur(8px);
        }}
        .log-card:hover {{
            transform: translateY(-1px);
            border-color: rgba(168, 85, 247, 0.25);
            box-shadow: 0 10px 25px rgba(0,0,0,0.3);
            background: rgba(17, 24, 39, 0.6);
        }}
        .action-badge {{
            padding: 8px 14px;
            border-radius: 10px;
            font-size: 11px;
            font-weight: 700;
            text-transform: uppercase;
            letter-spacing: 0.05em;
            display: flex;
            align-items: center;
            gap: 6px;
            flex-shrink: 0;
            min-width: 130px;
            justify-content: center;
        }}
        .action-badge.search {{
            background: rgba(16, 185, 129, 0.1);
            border: 1px solid rgba(16, 185, 129, 0.2);
            color: #34d399;
        }}
        .action-badge.download {{
            background: rgba(59, 130, 246, 0.1);
            border: 1px solid rgba(59, 130, 246, 0.2);
            color: #60a5fa;
        }}
        .action-badge.cloud {{
            background: rgba(139, 92, 246, 0.1);
            border: 1px solid rgba(139, 92, 246, 0.2);
            color: #a78bfa;
        }}
        .action-badge.details {{
            background: rgba(245, 158, 11, 0.1);
            border: 1px solid rgba(245, 158, 11, 0.2);
            color: #fbbf24;
        }}
        .action-badge.system {{
            background: rgba(107, 114, 128, 0.1);
            border: 1px solid rgba(107, 114, 128, 0.2);
            color: #9ca3af;
        }}
        .log-content {{
            flex: 1;
            display: flex;
            flex-direction: column;
            gap: 8px;
        }}
        .log-meta {{
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 20px;
        }}
        .user-container {{
            display: flex;
            align-items: center;
            gap: 10px;
        }}
        .user-badge {{
            padding: 3px 10px;
            border-radius: 6px;
            font-size: 12px;
            font-weight: 600;
        }}
        .timestamp {{
            font-size: 12px;
            color: var(--text-sub);
        }}
        .log-title {{
            font-size: 16px;
            font-weight: 600;
            color: var(--text-main);
            line-height: 1.4;
            word-break: break-word;
            -webkit-user-select: text;
            user-select: text;
        }}
        .meta-rows {{
            display: flex;
            flex-direction: column;
            gap: 6px;
            margin-top: 6px;
        }}
        .meta-row {{
            background: rgba(0,0,0,0.2);
            border: 1px solid rgba(255,255,255,0.03);
            border-radius: 8px;
            padding: 8px 14px;
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 20px;
            font-size: 13px;
        }}
        .meta-label {{
            color: var(--text-sub);
            font-weight: 500;
            font-size: 12px;
            text-transform: uppercase;
            letter-spacing: 0.05em;
        }}
        .meta-val {{
            font-family: monospace;
            color: var(--text-main);
            word-break: break-all;
            -webkit-user-select: text;
            user-select: text;
        }}
        .meta-val.link-style {{
            color: #3b82f6;
            text-decoration: none;
            font-weight: 600;
            transition: color 0.2s ease;
        }}
        .meta-val.link-style:hover {{
            color: #60a5fa;
            text-decoration: underline;
        }}
        .no-logs {{
            text-align: center;
            padding: 60px 20px;
            color: var(--text-sub);
            font-size: 15px;
            background: rgba(17, 24, 39, 0.3);
            border: 1px solid var(--border-color);
            border-radius: 16px;
        }}
        main::-webkit-scrollbar {{
            width: 8px;
        }}
        main::-webkit-scrollbar-track {{
            background: transparent;
        }}
        main::-webkit-scrollbar-thumb {{
            background: rgba(255,255,255,0.08);
            border-radius: 4px;
        }}
        main::-webkit-scrollbar-thumb:hover {{
            background: rgba(255,255,255,0.15);
        }}

        /* Modal Overlay CSS styling */
        .modal-backdrop {{
            position: fixed;
            top: 0;
            left: 0;
            width: 100%;
            height: 100%;
            background: rgba(8, 11, 17, 0.75);
            backdrop-filter: blur(15px);
            -webkit-backdrop-filter: blur(15px);
            z-index: 100;
            display: none;
            align-items: center;
            justify-content: center;
            animation: fadeIn 0.25s ease;
        }}
        @keyframes fadeIn {{
            from {{ opacity: 0; }}
            to {{ opacity: 1; }}
        }}
        .modal-card {{
            background: rgba(17, 24, 39, 0.85);
            border: 1px solid var(--border-color);
            border-radius: 24px;
            padding: 35px;
            width: 90%;
            max-width: 580px;
            box-shadow: 0 25px 50px -12px rgba(0, 0, 0, 0.6);
            position: relative;
            text-align: center;
            animation: scaleIn 0.35s cubic-bezier(0.16, 1, 0.3, 1);
        }}
        @keyframes scaleIn {{
            from {{ opacity: 0; transform: scale(0.95); }}
            to {{ opacity: 1; transform: scale(1); }}
        }}
        .modal-close {{
            position: absolute;
            top: 20px;
            right: 24px;
            background: transparent;
            border: none;
            color: var(--text-sub);
            font-size: 28px;
            cursor: pointer;
            line-height: 1;
            transition: color 0.2s ease;
        }}
        .modal-close:hover {{
            color: var(--text-main);
        }}
        .modal-header {{
            margin-bottom: 25px;
            text-align: left;
        }}
        .modal-header h3 {{
            font-size: 20px;
            font-weight: 700;
            color: var(--text-main);
            margin-bottom: 4px;
        }}
        .modal-header p {{
            font-size: 13px;
            color: var(--text-sub);
        }}
        .stats-grid {{
            display: grid;
            grid-template-columns: repeat(2, 1fr);
            gap: 16px;
            margin-bottom: 25px;
        }}
        .stat-box {{
            background: rgba(255, 255, 255, 0.02);
            border: 1px solid rgba(255, 255, 255, 0.04);
            border-radius: 14px;
            padding: 14px;
            display: flex;
            flex-direction: column;
            align-items: flex-start;
            gap: 6px;
            text-align: left;
        }}
        .stat-label {{
            font-size: 11px;
            font-weight: 600;
            color: var(--text-sub);
            text-transform: uppercase;
            letter-spacing: 0.05em;
        }}
        .stat-value {{
            font-size: 14px;
            font-weight: 600;
            color: var(--text-main);
        }}
        .modal-actions {{
            display: flex;
            gap: 12px;
            margin-top: 10px;
        }}
        .action-btn {{
            flex: 1;
            padding: 12px;
            border-radius: 12px;
            font-size: 13px;
            font-weight: 600;
            cursor: pointer;
            transition: all 0.2s ease;
            border: 1px solid var(--border-color);
        }}
        .action-btn.server-btn {{
            background: rgba(168, 85, 247, 0.1);
            border-color: rgba(168, 85, 247, 0.3);
            color: #c084fc;
        }}
        .action-btn.server-btn:hover {{
            background: rgba(168, 85, 247, 0.18);
        }}
        .action-btn.client-btn {{
            background: rgba(255, 255, 255, 0.03);
            color: var(--text-sub);
        }}
        .action-btn.client-btn:hover {{
            background: rgba(255, 255, 255, 0.08);
            color: var(--text-main);
        }}

        /* PREMIUM MOBILE RESPONSIVENESS OVERRIDES */
        @media (max-width: 820px) {{
            header {{
                flex-direction: column;
                padding: 15px 20px;
                gap: 12px;
                align-items: stretch;
            }}
            .header-left {{
                justify-content: space-between;
                width: 100%;
            }}
            .header-left h1 {{
                font-size: 18px;
            }}
            .search-container {{
                margin: 0;
                max-width: 100%;
                width: 100%;
            }}
            .header-right {{
                justify-content: space-between;
                width: 100%;
                gap: 8px;
            }}
            .header-right .btn {{
                flex: 1;
                justify-content: center;
                font-size: 12px;
                padding: 8px 10px;
            }}
            .filter-bar {{
                padding: 10px 20px;
                overflow-x: auto;
                white-space: nowrap;
                gap: 8px;
                -webkit-overflow-scrolling: touch;
            }}
            .filter-bar::-webkit-scrollbar {{
                display: none;
            }}
            .filter-pill {{
                flex-shrink: 0;
                padding: 6px 12px;
                font-size: 11px;
            }}
            main {{
                padding: 15px 20px;
            }}
            .log-card {{
                flex-direction: column;
                gap: 12px;
                padding: 16px;
                border-radius: 12px;
            }}
            .action-badge {{
                width: 100%;
                min-width: 0;
                justify-content: center;
                padding: 6px 12px;
            }}
            .log-content {{
                width: 100%;
            }}
            .log-meta {{
                flex-direction: row;
                justify-content: space-between;
                align-items: center;
                gap: 10px;
                width: 100%;
            }}
            .log-title {{
                font-size: 14px;
            }}
            .meta-row {{
                flex-direction: column;
                align-items: flex-start;
                gap: 4px;
                padding: 8px 12px;
            }}
            .meta-val {{
                word-break: break-all;
                text-align: left;
                width: 100%;
            }}
            /* Modal improvements on mobile */
            .modal-card {{
                padding: 24px 20px 20px 20px;
                border-radius: 20px;
                width: 92%;
                margin: 10px;
                max-height: 90%;
                display: flex;
                flex-direction: column;
            }}
            .modal-header {{
                margin-bottom: 16px;
            }}
            .modal-header h3 {{
                font-size: 18px;
            }}
            .stats-grid {{
                grid-template-columns: 1fr;
                gap: 10px;
                margin-bottom: 20px;
                overflow-y: auto;
                max-height: 50vh;
                padding-right: 4px;
            }}
            .stat-box {{
                padding: 10px 12px;
                gap: 4px;
            }}
            .stat-value {{
                font-size: 13px;
                word-break: break-all;
            }}
            .modal-actions {{
                flex-direction: column;
                gap: 10px;
            }}
            .action-btn {{
                padding: 10px;
                font-size: 12px;
                width: 100%;
            }}
        }}
    </style>
</head>
<body>
    <div class="ambient-glow"></div>
    <div class="ambient-glow-2"></div>

    <header>
        <div class="header-left">
            <h1>📋 System Activity Logs</h1>
            <span class="count-badge" id="count-label">0 events</span>
        </div>
        <div class="search-container">
            <svg viewBox="0 0 24 24"><path d="M15.5 14h-.79l-.28-.27A6.471 6.471 0 0 0 16 9.5 6.5 6.5 0 1 0 9.5 16c1.61 0 3.09-.59 4.23-1.57l.27.28v.79l5 4.99L20.49 19l-4.99-5zm-6 0C7.01 14 5 11.99 5 9.5S7.01 5 9.5 5 14 7.01 14 9.5 11.99 14 9.5 14z"/></svg>
            <input type="text" id="search-input" placeholder="Search by username or title...">
        </div>
        <div class="header-right">
            <button class="btn" onclick="window.location.reload()">🔄 Refresh</button>
            <button class="btn" onclick="window.showStatsModal()">📊 Stats</button>
            <button class="btn accent" onclick="window.location.href='/'">Go Home</button>
        </div>
    </header>

    <div class="filter-bar">
        <div class="filter-pill active" data-filter="all">All Events</div>
        <div class="filter-pill" data-filter="search">🔍 Searches</div>
        <div class="filter-pill" data-filter="download">🚀 Downloads</div>
        <div class="filter-pill" data-filter="cloud">☁️ Cloud DLs</div>
        <div class="filter-pill" data-filter="details">ℹ️ Details</div>
    </div>

    <main>
        <div class="logs-wrapper" id="logs-container"></div>
    </main>

    <!-- Verbose Diagnostics Stat Modal -->
    <div class="modal-backdrop" id="stats-modal">
        <div class="modal-card">
            <button class="modal-close" onclick="window.hideStatsModal()">&times;</button>
            <div class="modal-header">
                <h3>📊 Server Diagnostics</h3>
                <p>Real-time system telemetry and storage footprint</p>
            </div>
            <div class="stats-grid">
                <div class="stat-box">
                    <span class="stat-label">Server Time</span>
                    <span class="stat-value" id="stat-server-time">-</span>
                </div>
                <div class="stat-box">
                    <span class="stat-label">Uptime</span>
                    <span class="stat-value" id="stat-uptime">-</span>
                </div>
                <div class="stat-box">
                    <span class="stat-label">Active Sessions</span>
                    <span class="stat-value" id="stat-sessions">-</span>
                </div>
                <div class="stat-box">
                    <span class="stat-label">Process ID</span>
                    <span class="stat-value" id="stat-pid">-</span>
                </div>
                <div class="stat-box">
                    <span class="stat-label">Total Log Events</span>
                    <span class="stat-value" id="stat-events">-</span>
                </div>
                <div class="stat-box">
                    <span class="stat-label">Cached Posters</span>
                    <span class="stat-value" id="stat-posters">-</span>
                </div>
                <div class="stat-box">
                    <span class="stat-label">Autocomplete Db</span>
                    <span class="stat-value" id="stat-autodb">-</span>
                </div>
                <div class="stat-box">
                    <span class="stat-label">System Environment</span>
                    <span class="stat-value" id="stat-env" style="font-size: 11px;">-</span>
                </div>
            </div>
            <div class="modal-actions">
                <button class="action-btn server-btn" id="server-btn" onclick="clearServerCache()">Wipe Server Cache</button>
                <button class="action-btn client-btn" onclick="clearClientCache()">Wipe Client Cache</button>
            </div>
        </div>
    </div>

    <script>
        const LOGS_DATA = {logs_json};

        function getUsernameColor(username) {{
            if (!username || username === 'anonymous' || username === 'system') {{
                return {{
                    bg: 'rgba(156, 163, 175, 0.08)',
                    border: 'rgba(156, 163, 175, 0.18)',
                    text: '#9ca3af'
                }};
            }}
            let hash = 0;
            for (let i = 0; i < username.length; i++) {{
                hash = username.charCodeAt(i) + ((hash << 5) - hash);
            }}
            const hue = Math.abs(hash % 360);
            return {{
                bg: `hsla(${{hue}}, 55%, 45%, 0.08)`,
                border: `hsla(${{hue}}, 55%, 45%, 0.22)`,
                text: `hsl(${{hue}}, 65%, 72%)`
            }};
        }}

        function getActionBadgeClass(action) {{
            const act = action.toUpperCase();
            if (act.includes('SEARCH')) return 'search';
            if (act.includes('CLOUD')) return 'cloud';
            if (act.includes('DOWNLOAD')) return 'download';
            if (act.includes('DETAIL')) return 'details';
            return 'system';
        }}

        function getActionIcon(action) {{
            const act = action.toUpperCase();
            if (act.includes('SEARCH')) return '🔍';
            if (act.includes('CLOUD')) return '☁️';
            if (act.includes('DOWNLOAD')) return '🚀';
            if (act.includes('DETAIL')) return 'ℹ️';
            return '⚙️';
        }}

        let currentFilter = 'all';
        let searchQuery = '';

        const searchInput = document.getElementById('search-input');
        const container = document.getElementById('logs-container');
        const countLabel = document.getElementById('count-label');

        function renderLogs() {{
            container.innerHTML = '';
            
            const filtered = LOGS_DATA.filter(item => {{
                const act = item.action.toUpperCase();
                if (currentFilter !== 'all') {{
                    if (currentFilter === 'search' && !act.includes('SEARCH')) return false;
                    if (currentFilter === 'download' && !act.includes('DOWNLOAD')) return false;
                    if (currentFilter === 'cloud' && !act.includes('CLOUD')) return false;
                    if (currentFilter === 'details' && !act.includes('DETAIL')) return false;
                }}
                
                if (searchQuery) {{
                    const q = searchQuery.toLowerCase();
                    const userMatch = item.username.toLowerCase().includes(q);
                    const titleMatch = item.title.toLowerCase().includes(q);
                    const subMatch = item.sublines.some(sl => sl.toLowerCase().includes(q));
                    if (!userMatch && !titleMatch && !subMatch) return false;
                }}
                
                return true;
            }});

            countLabel.innerText = `${{filtered.length}} event${{filtered.length === 1 ? '' : 's'}}`;

            if (filtered.length === 0) {{
                container.innerHTML = `<div class="no-logs">No activity log events match your filters.</div>`;
                return;
            }}

            filtered.forEach(item => {{
                const colors = getUsernameColor(item.username);
                const badgeClass = getActionBadgeClass(item.action);
                const icon = getActionIcon(item.action);
                
                const card = document.createElement('div');
                card.className = 'log-card';
                
                let cleanAction = item.action.replace(/[🔍☁️🚀ℹ️⚙️👤|]/g, '').trim();

                let metaHTML = '';
                if (item.sublines && item.sublines.length > 0) {{
                    metaHTML = `<div class="meta-rows">`;
                    item.sublines.forEach(sl => {{
                        let label = 'Detail';
                        let val = sl;
                        
                        if (sl.includes(':')) {{
                            const idx = sl.indexOf(':');
                            label = sl.substring(0, idx).trim();
                            val = sl.substring(idx + 1).trim();
                        }}
                        
                        const isUrl = val.startsWith('http://') || val.startsWith('https://');
                        const valHTML = isUrl 
                            ? `<a class="meta-val link-style" href="${{val}}" target="_blank">${{val}}</a>` 
                            : `<span class="meta-val">${{val}}</span>`;
                            
                        metaHTML += `
                            <div class="meta-row">
                                <span class="meta-label">${{label}}</span>
                                ${{valHTML}}
                            </div>
                        `;
                    }});
                    metaHTML += `</div>`;
                }}

                card.innerHTML = `
                    <div class="action-badge ${{badgeClass}}">${{icon}} ${{cleanAction}}</div>
                    <div class="log-content">
                        <div class="log-meta">
                            <div class="user-container">
                                <span class="user-badge" style="background: ${{colors.bg}}; border: 1px solid ${{colors.border}}; color: ${{colors.text}};">${{item.username}}</span>
                            </div>
                            <span class="timestamp">${{item.timestamp}}</span>
                        </div>
                        <div class="log-title">${{item.title}}</div>
                        ${{metaHTML}}
                    </div>
                `;
                container.appendChild(card);
            }});
        }}

        searchInput.addEventListener('input', (e) => {{
            searchQuery = e.target.value;
            renderLogs();
        }});

        document.querySelectorAll('.filter-pill').forEach(pill => {{
            pill.addEventListener('click', (e) => {{
                document.querySelectorAll('.filter-pill').forEach(p => p.classList.remove('active'));
                pill.classList.add('active');
                currentFilter = pill.getAttribute('data-filter');
                renderLogs();
            }});
        }});

        // Modal Overlay JavaScript functions
        const modal = document.getElementById('stats-modal');

        window.showStatsModal = function() {{
            modal.style.display = 'flex';
            loadStats();
        }}

        window.hideStatsModal = function() {{
            modal.style.display = 'none';
        }}

        modal.addEventListener('click', (e) => {{
            if (e.target === modal) {{
                window.hideStatsModal();
            }}
        }});

        function loadStats() {{
            fetch('/api/storage-stats')
                .then(res => res.json())
                .then(data => {{
                    if (data.error) return;
                    document.getElementById('stat-server-time').innerText = data.server_time || '-';
                    document.getElementById('stat-uptime').innerText = data.uptime || '-';
                    document.getElementById('stat-sessions').innerText = data.admin_sessions_count || '0';
                    document.getElementById('stat-pid').innerText = data.process_id || '-';
                    document.getElementById('stat-events').innerText = data.total_events || '0';
                    document.getElementById('stat-posters').innerText = `${{data.cached_posters_count}} files (${{data.cached_posters_size}})`;
                    document.getElementById('stat-autodb').innerText = `${{data.imdb_suggest_cache_count}} items (${{data.imdb_suggest_cache_size}})`;
                    document.getElementById('stat-env').innerText = `${{data.system_os}} | Python ${{data.python_version}}`;
                }})
                .catch(err => console.error("Error loading stats:", err));
        }}

        function clearServerCache() {{
            if (!confirm("Are you sure you want to permanently clear the server-side suggestions cache and delete all cached posters from the SSD?")) return;
            const btn = document.getElementById('server-btn');
            btn.innerText = "Clearing...";
            btn.disabled = true;
            
            fetch('/api/clear-server-cache')
                .then(res => res.json())
                .then(data => {{
                    alert(data.message || "Server cache cleared successfully!");
                    loadStats();
                }})
                .catch(err => alert("Failed to clear server cache: " + err))
                .finally(() => {{
                    btn.innerText = "Wipe Server Cache";
                    btn.disabled = false;
                }});
        }}

        function clearClientCache() {{
            if (!confirm("Are you sure you want to delete all client-side browser suggestion caches?")) return;
            localStorage.removeItem('mcrackd_imdb_suggestions');
            alert("Client-side suggestions cache cleared successfully!");
            loadStats();
        }}

        renderLogs();
    </script>
</body>
</html>"""
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Cache-Control', 'no-cache, no-store, must-revalidate')
        self.end_headers()
        self.wfile.write(html_code.encode('utf-8'))

    def check_admin_auth(self) -> bool:
        """Enforce cookie-based session authentication for admin panels."""
        cookie_header = self.headers.get('Cookie', '')
        session_id = None
        for cookie in cookie_header.split(';'):
            parts = cookie.strip().split('=', 1)
            if len(parts) == 2 and parts[0] == 'admin_session':
                session_id = parts[1]
                break

        if session_id and session_id in ADMIN_SESSIONS:
            return True

        parsed = urlparse(self.path)
        if parsed.path == '/admin':
            self.serve_login_page(parsed.path)
            return False

        self.send_response(401)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(json.dumps({"status": "error", "message": "Unauthorized: Session cookie is invalid or missing."}).encode('utf-8'))
        return False

    def do_GET(self):
        parsed = urlparse(self.path)
        
        # 1. Serve frontend index.html static file
        if parsed.path == '/' or parsed.path == '/index.html':
            try:
                with open('index.html', 'rb') as f:
                    content = f.read().decode('utf-8')
                
                # Dynamic cache busting based on modification timestamps
                css_time = int(os.path.getmtime('static/css/app.css')) if os.path.exists('static/css/app.css') else 1
                js_time = int(os.path.getmtime('static/js/app.js')) if os.path.exists('static/js/app.js') else 1
                
                content = content.replace('/static/css/app.css', f'/static/css/app.css?v={css_time}')
                content = content.replace('/static/js/app.js', f'/static/js/app.js?v={js_time}')
                
                content_bytes = content.encode('utf-8')
                self.send_response(200)
                self.send_header('Content-Type', 'text/html')
                self.send_header('Content-Length', str(len(content_bytes)))
                self.end_headers()
                self.wfile.write(content_bytes)
            except Exception as e:
                self.send_response(500)
                self.end_headers()
                self.wfile.write(f"Error loading index.html: {e}".encode('utf-8'))
            return

        # 1b. Serve logo_optimized.png static image
        if parsed.path == '/logo_optimized.png':
            try:
                with open('logo_optimized.png', 'rb') as f:
                    content = f.read()
                self.send_response(200)
                self.send_header('Content-Type', 'image/png')
                self.end_headers()
                self.wfile.write(content)
            except Exception as e:
                self.send_response(404)
                self.end_headers()
            return

        # Redirect obsolete paths to the unified /admin
        if parsed.path == '/logs' or parsed.path == '/stats':
            self.send_response(302)
            self.send_header('Location', '/admin')
            self.end_headers()
            return

        # Serve secure combined /admin panel page
        if parsed.path == '/admin':
            if not self.check_admin_auth():
                return
            
            parsed_events = []
            try:
                if os.path.exists(SEARCH_LOGS_FILE):
                    with open(SEARCH_LOGS_FILE, 'r', encoding='utf-8') as f:
                        lines = f.readlines()
                    parsed_events = parse_log_events(lines)
            except Exception as e:
                parsed_events = [{"timestamp": "", "username": "system", "action": "system error", "title": f"Error loading logs: {e}", "sublines": []}]
            
            self.serve_admin_page(json.dumps(parsed_events))
            return

        # 1c. Uptime compatibility ping endpoint for Cloudflare Monitor worker
        if parsed.path == '/api/ping' or parsed.path == '/ping':
            print("[*] Received compatibility uptime ping from Cloudflare Monitor worker", flush=True)
            self.send_json({
                "status": "ok",
                "uptime": "online",
                "service": "moviescrackd-backend",
                "timestamp": time.time()
            })
            return

        # Keep /api/logs raw plaintext endpoint intact for API metrics
        if parsed.path == '/api/logs':
            if not self.check_admin_auth():
                return
            
            raw_logs_content = ""
            try:
                if os.path.exists(SEARCH_LOGS_FILE):
                    with open(SEARCH_LOGS_FILE, 'r', encoding='utf-8') as f:
                        lines = f.readlines()
                    
                    events = []
                    current_event = []
                    for line in lines:
                        if line.startswith('['):
                            if current_event:
                                events.append("".join(current_event))
                                current_event = []
                        current_event.append(line)
                    if current_event:
                        events.append("".join(current_event))
                    
                    reversed_events = reversed(events)
                    raw_logs_content = "".join(reversed_events)
                else:
                    raw_logs_content = "No search logs found yet."
            except Exception as e:
                raw_logs_content = f"Error reading logs: {e}"

            self.send_response(200)
            self.send_header('Content-Type', 'text/plain; charset=utf-8')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.send_header('Cache-Control', 'no-cache, no-store, must-revalidate')
            self.end_headers()
            self.wfile.write(raw_logs_content.encode('utf-8'))
            return

        # 1c-3. Record search log silently for cached local searches & details page views & clicks
        if parsed.path == '/api/logs/record':
            qs = parse_qs(parsed.query)
            log_type = qs.get("type", ["search"])[0]
            client_id = qs.get("clientId", [None])[0]
            
            if log_type == "detail":
                title = qs.get("title", [""])[0].strip()
                url = qs.get("url", [""])[0].strip()
                if title:
                    uid = client_id if client_id else "anonymous"
                    clean_title = clean_log_title(title)
                    event_msg = f"👤 {uid:<10} | ℹ️ DETAILS   | \"{clean_title}\"\n└─► Source Page: {url}"
                    log_instant_event(event_msg)
            elif log_type == "device_download":
                title = qs.get("title", [""])[0].strip()
                uid = client_id if client_id else "anonymous"
                clean_title = clean_log_title(title)
                event_msg = f"👤 {uid:<10} | ☁️ CLOUD DL   | \"{clean_title}\""
                log_instant_event(event_msg)
            else:
                query = qs.get("q", [""])[0].strip()
                if query:
                    log_search_query(query, client_id=client_id)
                    
            self.send_response(200)
            self.send_header('Content-Type', 'text/plain')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(b"OK")
            return

        # 1d. Serve static CSS, JS, images, etc. dynamically from the static directory with long-term caching
        if parsed.path.startswith('/static/'):
            local_path = parsed.path.lstrip('/')
            if '..' not in local_path and os.path.exists(local_path) and os.path.isfile(local_path):
                content_type = 'application/octet-stream'
                if local_path.endswith('.css'):
                    content_type = 'text/css'
                elif local_path.endswith('.js'):
                    content_type = 'application/javascript'
                elif local_path.endswith('.png'):
                    content_type = 'image/png'
                elif local_path.endswith('.jpg') or local_path.endswith('.jpeg'):
                    content_type = 'image/jpeg'
                elif local_path.endswith('.webp'):
                    content_type = 'image/webp'
                
                try:
                    with open(local_path, 'rb') as f:
                        content = f.read()
                    self.send_response(200)
                    self.send_header('Content-Type', content_type)
                    self.send_header('Cache-Control', 'public, max-age=31536000')
                    self.end_headers()
                    self.wfile.write(content)
                    return
                except Exception:
                    pass

        # 1e. Serve compressed WebP thumbnails with caching
        if parsed.path == '/api/thumbnail':
            query = parse_qs(parsed.query)
            image_url = query.get('url', [None])[0]
            if not image_url:
                self.send_response(400)
                self.end_headers()
                self.wfile.write(b"Missing url parameter")
                return

            import io
            from PIL import Image
            
            # Create a unique filename for the cached WebP image
            url_hash = hashlib.md5(image_url.encode('utf-8')).hexdigest()
            cache_dir = os.path.join('static', 'thumbnail_cache')
            os.makedirs(cache_dir, exist_ok=True)
            cache_path = os.path.join(cache_dir, f"{url_hash}.webp")

            # Check if cached file exists
            if os.path.exists(cache_path):
                try:
                    with open(cache_path, 'rb') as f:
                        content = f.read()
                    self.send_response(200)
                    self.send_header('Content-Type', 'image/webp')
                    self.send_header('Cache-Control', 'public, max-age=31536000')
                    self.end_headers()
                    self.wfile.write(content)
                    return
                except Exception:
                    pass

            # Fetch, resize, compress and cache the image
            try:
                headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'}
                resp = requests.get(image_url, headers=headers, timeout=8)
                if resp.status_code == 200:
                    img = Image.open(io.BytesIO(resp.content))
                    
                    # Convert to RGB if needed
                    if img.mode not in ('RGB', 'RGBA'):
                        img = img.convert('RGB')
                        
                    # Resize to max 320px width to save bandwidth
                    max_width = 320
                    if img.width > max_width:
                        ratio = max_width / float(img.width)
                        new_height = int(float(img.height) * float(ratio))
                        img = img.resize((max_width, new_height), Image.Resampling.LANCZOS)
                        
                    # Save as WebP
                    img.save(cache_path, 'WEBP', quality=80)
                    
                    with open(cache_path, 'rb') as f:
                        content = f.read()
                        
                    self.send_response(200)
                    self.send_header('Content-Type', 'image/webp')
                    self.send_header('Cache-Control', 'public, max-age=31536000')
                    self.end_headers()
                    self.wfile.write(content)
                    return
            except Exception as e:
                print(f"[!] Error caching thumbnail {image_url}: {e}", flush=True)

            # Fallback: Redirect to the original URL if compression/caching fails
            self.send_response(302)
            self.send_header('Location', image_url)
            self.end_headers()
            return

        # 2. API Status endpoint
        if parsed.path == '/api/status':
            tg_text, tg_color = DOWNLOAD_MGR._get_telegram_ready_status()
            qs = parse_qs(parsed.query)
            client_id = qs.get("clientId", ["anonymous"])[0]
            state = DOWNLOAD_MGR.get_client_state(client_id)
            self.send_json({
                "done_count": state.done_count,
                "fail_count": state.fail_count,
                "total_count": state.total_count,
                "active_threads": state.active_threads,
                "output_dir": state.output_dir or DOWNLOAD_MGR.output_dir,
                "cloud_mode": DOWNLOAD_MGR.cloud_mode,
                "telegram": {
                    "text": tg_text,
                    "color": tg_color
                }
            })
            return

        # 3. API Downloads list endpoint
        if parsed.path == '/api/downloads':
            qs = parse_qs(parsed.query)
            client_id = qs.get("clientId", ["anonymous"])[0]
            state = DOWNLOAD_MGR.get_client_state(client_id)
            self.send_json({
                "downloads": [card.to_json() for card in state.cards]
            })
            return

        # 3b. API Trending movies list endpoint
        if parsed.path == '/api/trending':
            self.send_json({
                "movies": get_cached_trending()
            })
            return

        # 4. Same-page details movie qualities extractor
        if parsed.path == '/api/detail':
            qs = parse_qs(parsed.query)
            target_url = qs.get("url", [""])[0]
            if not target_url:
                self.send_json({"error": "Missing url query param"}, 400)
                return

            try:
                # Scrape download option buttons using movie_search library
                options = extract_download_options(target_url)
                metadata = getattr(options, 'metadata', {})
                self.send_json({
                    "options": options,
                    "metadata": metadata
                })
            except Exception as e:
                self.send_json({"error": str(e)}, 500)
            return

        # 4b. IMDb Autocomplete Suggestion Proxy
        if parsed.path == '/api/suggest':
            qs = parse_qs(parsed.query)
            query = qs.get("q", [""])[0].strip()
            if not query:
                self.send_json([])
                return
            
            # Serve from microsecond server-side LRU cache if present
            query_lower = query.lower()
            if query_lower in IMDB_SUGGEST_CACHE:
                IMDB_SUGGEST_CACHE.move_to_end(query_lower)  # mark as recently used
                self.send_json(IMDB_SUGGEST_CACHE[query_lower])
                return

            # Check if it's alphanumeric or space to prevent potential directory traversal or malicious injection
            if not re.match(r'^[a-zA-Z0-9\s\-\:\.\'\,\!\&\(\)]+$', query):
                self.send_json([])
                return

            safe_query = urllib.parse.quote(query_lower)
            url = f"https://v3.sg.media-imdb.com/suggestion/titles/x/{safe_query}.json"
            try:
                response = SESSION.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=3)
                if response.status_code == 200:
                    data = response.json()
                    suggestions = []
                    for item in data.get('d', []):
                        if not item.get('l'):
                            continue
                        suggestions.append({
                            'id': item.get('id', ''),
                            'title': item.get('l'),
                            'year': item.get('y'),
                            'stars': item.get('s'),
                            'type': item.get('q', 'Movie'),
                            'image': item.get('i', {}).get('imageUrl')
                        })
                    res_payload = suggestions[:6]
                    # LRU eviction: drop oldest entries instead of nuking entire cache
                    while len(IMDB_SUGGEST_CACHE) >= IMDB_SUGGEST_CACHE_MAX:
                        IMDB_SUGGEST_CACHE.popitem(last=False)
                    IMDB_SUGGEST_CACHE[query_lower] = res_payload
                    self.send_json(res_payload)
                    save_imdb_suggest_cache_to_file()
                    return
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass  # Client aborted (AbortController) — expected during fast typing
            except Exception as e:
                print(f"[!] Autocomplete proxy error: {e}", flush=True)
            self.send_json([])
            return

        # 4d. Diagnostics & Storage Stats
        if parsed.path == '/api/storage-stats':
            if not self.check_admin_auth():
                return
            try:
                # 1. Total cached posters count and size
                cached_posters_count = 0
                cached_posters_size = 0
                if os.path.exists(IMG_PROXY_DIR):
                    for entry in os.scandir(IMG_PROXY_DIR):
                        if entry.is_file():
                            cached_posters_count += 1
                            cached_posters_size += entry.stat().st_size

                # 2. IMDb autocomplete cache size
                imdb_suggest_cache_count = len(IMDB_SUGGEST_CACHE)
                imdb_suggest_cache_size = 0
                if os.path.exists(IMDB_SUGGEST_CACHE_FILE):
                    imdb_suggest_cache_size = os.path.getsize(IMDB_SUGGEST_CACHE_FILE)

                # 3. Trending cache size
                trending_cache_size = 0
                if os.path.exists(TRENDING_CACHE_FILE):
                    trending_cache_size = os.path.getsize(TRENDING_CACHE_FILE)

                # 4. Total project folder size (computed fast skip massive dotfiles/venv)
                total_project_size = 0
                for root, dirs, files in os.walk('.'):
                    if '.venv' in root or 'node_modules' in root or '.git' in root or '.gemini' in root:
                        continue
                    for f in files:
                        fp = os.path.join(root, f)
                        try:
                            if os.path.exists(fp):
                                total_project_size += os.path.getsize(fp)
                        except Exception:
                            pass

                # Helper to format bytes
                def format_size(bytes_size):
                    for unit in ['B', 'KB', 'MB', 'GB']:
                        if bytes_size < 1024.0:
                            return f"{bytes_size:.2f} {unit}"
                        bytes_size /= 1024.0
                    return f"{bytes_size:.2f} TB"

                import platform
                
                uptime_seconds = int(time.time() - START_TIME)
                hours = uptime_seconds // 3600
                minutes = (uptime_seconds % 3600) // 60
                seconds = uptime_seconds % 60
                uptime_str = f"{hours}h {minutes}m {seconds}s"
                
                total_events = 0
                if os.path.exists(SEARCH_LOGS_FILE):
                    try:
                        with open(SEARCH_LOGS_FILE, 'r', encoding='utf-8') as f:
                            total_events = sum(1 for line in f if line.startswith('['))
                    except Exception:
                        pass

                stats = {
                    "cached_posters_count": cached_posters_count,
                    "cached_posters_size": format_size(cached_posters_size),
                    "imdb_suggest_cache_count": imdb_suggest_cache_count,
                    "imdb_suggest_cache_size": format_size(imdb_suggest_cache_size),
                    "trending_cache_size": format_size(trending_cache_size),
                    "total_project_size": format_size(total_project_size),
                    "server_time": get_ist_timestamp() + " IST",
                    "admin_sessions_count": len(ADMIN_SESSIONS),
                    "system_os": f"{platform.system()} {platform.machine()}",
                    "python_version": platform.python_version(),
                    "process_id": os.getpid(),
                    "uptime": uptime_str,
                    "total_events": total_events
                }
                self.send_json(stats)
            except Exception as e:
                self.send_json({"error": str(e)}, 500)
            return

        # 4e. Clear Server Cache
        if parsed.path == '/api/clear-server-cache':
            if not self.check_admin_auth():
                return
            try:
                # Clear in-memory suggestions
                IMDB_SUGGEST_CACHE.clear()
                # Delete suggestion database file
                if os.path.exists(IMDB_SUGGEST_CACHE_FILE):
                    try:
                        os.remove(IMDB_SUGGEST_CACHE_FILE)
                    except Exception:
                        pass

                # Empty poster files
                cleared_count = 0
                if os.path.exists(IMG_PROXY_DIR):
                    for entry in os.scandir(IMG_PROXY_DIR):
                        if entry.is_file():
                            try:
                                os.remove(entry.path)
                                cleared_count += 1
                            except Exception:
                                pass

                self.send_json({
                    "status": "success",
                    "message": f"Successfully cleared IMDb suggestion cache database and deleted {cleared_count} cached posters from server SSD."
                })
            except Exception as e:
                self.send_json({"status": "error", "message": str(e)}, 500)
            return

        # 4c. Image Proxy — caches and serves IMDb poster thumbnails through the server
        if parsed.path == '/api/img-proxy':
            qs = parse_qs(parsed.query)
            img_url = qs.get("url", [""])[0].strip()
            if not img_url or not img_url.startswith("https://"):
                self.send_response(400)
                self.end_headers()
                return

            # Dynamically optimize and compress IMDb/Amazon S3 images
            if "m.media-amazon.com/images/M/" in img_url:
                # Replace full-size suffix after base hash with optimized thumbnail rule: width=100px, quality=75%
                match = re.match(r'(.+?)(?:@)?\._V1_.*\.jpg$', img_url)
                if match:
                    img_url = f"{match.group(1)}@._V1_QL75_UX100_.jpg"


            # Compute safe MD5 filename for local disk storage
            img_hash = hashlib.md5(img_url.encode('utf-8')).hexdigest() + ".jpg"
            local_path = os.path.join(IMG_PROXY_DIR, img_hash)

            # Serve from local persistent disk cache if present!
            if os.path.exists(local_path):
                try:
                    with open(local_path, 'rb') as f:
                        img_data = f.read()
                    self.send_response(200)
                    self.send_header('Content-Type', 'image/jpeg')
                    self.send_header('Cache-Control', 'public, max-age=86400')
                    self.send_header('Access-Control-Allow-Origin', '*')
                    self.end_headers()
                    self.wfile.write(img_data)
                    return
                except Exception as e:
                    print(f"[-] Error reading cached poster from disk: {e}", flush=True)

            # If not in disk cache, fetch it from source
            try:
                resp = SESSION.get(img_url, headers={"User-Agent": "Mozilla/5.0"}, timeout=4)
                if resp.status_code == 200:
                    content_type = resp.headers.get('Content-Type', 'image/jpeg')
                    img_data = resp.content

                    # Save to local persistent disk cache!
                    try:
                        os.makedirs(IMG_PROXY_DIR, exist_ok=True)
                        with open(local_path, 'wb') as f:
                            f.write(img_data)
                    except Exception as e:
                        print(f"[-] Error writing poster cache to disk: {e}", flush=True)

                    self.send_response(200)
                    self.send_header('Content-Type', content_type)
                    self.send_header('Cache-Control', 'public, max-age=86400')
                    self.send_header('Access-Control-Allow-Origin', '*')
                    self.end_headers()
                    self.wfile.write(img_data)
                    return
            except Exception as e:
                print(f"[!] Image proxy error: {e}", flush=True)
            self.send_response(502)
            self.end_headers()
            return

        # 5. Server-Sent Events (SSE) Search Card Streamer!
        if parsed.path == '/api/search/stream':
            qs = parse_qs(parsed.query)
            query = qs.get("q", [""])[0].strip()
            category = qs.get("cat", ["All"])[0]
            client_id = qs.get("clientId", [None])[0]

            # Log search term to persistent disk logs securely (with IST timestamp)
            log_search_query(query, client_id=client_id)

            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.send_header('Cache-Control', 'no-cache')
            self.send_header('Connection', 'keep-alive')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()

            # Map category parameter to the expected format in movie_search.py
            cat_map = {
                "all": None,
                "hollywood": ["hollywood"],
                "bollywood": ["bollywood"],
                "anime": ["animeflix"]
            }
            cats = cat_map.get(category.lower(), None)

            # Execute streaming search and write SSE events chunk-by-chunk in real time!
            try:
                write_lock = threading.Lock()
                sent_urls = set()

                def _on_result(item):
                    url = item.get("url", "")
                    if not url:
                        return
                    with write_lock:
                        if url in sent_urls:
                            return
                        sent_urls.add(url)
                        
                        event_data = {
                            "status": "item",
                            "item": {
                                "title": item.get("title", ""),
                                "url": url,
                                "thumbnail": item.get("thumbnail", ""),
                                "category": item.get("category", "All")
                            }
                        }
                        try:
                            self.wfile.write(f"data: {json.dumps(event_data)}\n\n".encode('utf-8'))
                            self.wfile.flush()
                        except Exception:
                            pass

                # Run text search and IMDb resolution/priority search in parallel!
                def run_text_search():
                    try:
                        search_movies(query, cats, on_result_callback=_on_result)
                    except Exception as ex:
                        print(f"[-] Parallel text search failed: {ex}")

                def run_priority_search():
                    try:
                        # 1. Fetch IMDb tt ID for accurate priority searching in parallel
                        imdb_id = get_imdb_id(query)
                        if imdb_id:
                            print(f"[+] Found IMDb ID '{imdb_id}' for query '{query}'. Performing parallel priority search...")
                            search_movies(imdb_id, cats, on_result_callback=_on_result)
                    except Exception as ex:
                        print(f"[-] Parallel priority search failed: {ex}")

                # Spawn both search tasks in parallel threads
                t1 = threading.Thread(target=run_text_search)
                t2 = threading.Thread(target=run_priority_search)
                
                t1.start()
                t2.start()
                
                # Wait for both concurrent search threads to finish completely!
                t1.join()
                t2.join()
                
                # Write search completion event
                with write_lock:
                    self.wfile.write(f"data: {json.dumps({'status': 'done'})}\n\n".encode('utf-8'))
                    self.wfile.flush()
            except Exception as e:
                err_data = {"status": "error", "message": str(e)}
                try:
                    with write_lock:
                        self.wfile.write(f"data: {json.dumps(err_data)}\n\n".encode('utf-8'))
                        self.wfile.flush()
                except Exception:
                    pass
            return

        # Not Found
        self.send_response(404)
        self.end_headers()

    def do_POST(self):
        parsed = urlparse(self.path)
        content_length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(content_length).decode('utf-8') if content_length > 0 else ""

        # Admin session login handler
        if parsed.path == '/api/admin/login':
            try:
                data = json.loads(body)
                user = data.get("username", "").strip()
                password = data.get("password", "").strip()
                
                admin_user = os.getenv("ADMIN_USERNAME", "admin").strip()
                admin_pass = os.getenv("ADMIN_PASSWORD", "admin123").strip()
                
                if user == admin_user and password == admin_pass:
                    # Generate a secure session ID
                    session_id = hashlib.sha256(os.urandom(32)).hexdigest()
                    ADMIN_SESSIONS.add(session_id)
                    
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/json')
                    self.send_header('Access-Control-Allow-Origin', '*')
                    self.send_header('Set-Cookie', f'admin_session={session_id}; Path=/; HttpOnly; SameSite=Lax')
                    self.end_headers()
                    self.wfile.write(json.dumps({"status": "success"}).encode('utf-8'))
                else:
                    self.send_response(401)
                    self.send_header('Content-Type', 'application/json')
                    self.send_header('Access-Control-Allow-Origin', '*')
                    self.end_headers()
                    self.wfile.write(json.dumps({"status": "error", "message": "Invalid admin credentials"}).encode('utf-8'))
            except Exception as e:
                self.send_response(500)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Access-Control-Allow-Origin', '*')
                self.end_headers()
                self.wfile.write(json.dumps({"status": "error", "message": str(e)}).encode('utf-8'))
            return

        # 1. Native folder browser folder picker trigger
        if parsed.path == '/api/choose-folder':
            folder = DOWNLOAD_MGR.ask_directory()
            if folder:
                self.send_json({"cancelled": False, "path": folder})
            else:
                self.send_json({"cancelled": True})
            return

        # 2. Queue Direct URL/Quality link
        if parsed.path == '/api/download':
            try:
                data = json.loads(body)
                url = data.get("url")
                
                # Parse download telemetry parameters
                client_id = data.get("clientId")
                state = DOWNLOAD_MGR.get_client_state(client_id)
                output_dir = data.get("output_dir", DOWNLOAD_MGR.output_dir)
                
                if not url:
                    self.send_json({"error": "Missing url body param"}, 400)
                    return

                title = data.get("title", "Direct URL")
                option_title = data.get("optionTitle", "")
                button_text = data.get("buttonText", "")

                # Record detailed user journey log instantly
                uid = client_id if client_id else "anonymous"
                clean_title = clean_log_title(title)
                if option_title or button_text:
                    parts = [f"👤 {uid:<10} | 🚀 DOWNLOAD  | \"{clean_title}\""]
                    if option_title and button_text:
                        parts.append(f"├─► Quality Tag: \"{option_title}\"")
                        parts.append(f"└─► Button Label: \"{button_text}\"")
                    elif option_title:
                        parts.append(f"└─► Quality Tag: \"{option_title}\"")
                    else:
                        parts.append(f"└─► Button Label: \"{button_text}\"")
                    event_msg = "\n".join(parts)
                else:
                    event_msg = f"👤 {uid:<10} | 🚀 DOWNLOAD  | \"{clean_title}\""
                log_instant_event(event_msg)

                state.active_title = title
                DOWNLOAD_MGR.start_pipeline(url, output_dir, client_id=client_id)
                self.send_json({"status": "success", "message": "Pipeline initiated"})
            except Exception as e:
                self.send_json({"error": str(e)}, 500)
            return

        # 2b. Clear Client resolving downloads
        if parsed.path == '/api/downloads/clear':
            try:
                data = json.loads(body)
                client_id = data.get("clientId", "anonymous")
                DOWNLOAD_MGR.clear_client_state(client_id)
                self.send_json({"status": "success"})
            except Exception as e:
                self.send_json({"error": str(e)}, 500)
            return

        if parsed.path == '/api/retry':
            try:
                data = json.loads(body)
                client_id = data.get("clientId", "anonymous")
                state = DOWNLOAD_MGR.get_client_state(client_id)
                idx = int(data.get("index", -1))
                if idx < 0 or idx >= len(state.cards):
                    self.send_json({"error": "Invalid index"}, 400)
                    return

                card = state.cards[idx]
                item = card.item_data
                
                if not item:
                    self.send_json({"error": "Card has no metadata item data"}, 400)
                    return

                # Log the retry event
                uid = client_id
                active_title = state.active_title
                clean_title = clean_log_title(active_title)
                fname = card.filename
                method = item.get("method", "")
                
                event_msg = (
                    f"👤 {uid:<10} | 🔄 RETRY     | \"{clean_title}\"\n"
                    f"├─► Episode: \"{fname}\"\n"
                    f"└─► Method: \"{method}\""
                )
                log_instant_event(event_msg)

                if item.get("method") == "TELEGRAM":
                    if card.state == 3:
                        with DOWNLOAD_MGR._lock:
                            state.fail_count = max(0, state.fail_count - 1)
                    card.retry_count += 1
                    card.mark_pending()
                    DOWNLOAD_MGR.start_telegram_manual(idx, is_retry=True, client_id=client_id)
                elif item.get("download_url"):
                    if card.state == 3:
                        with DOWNLOAD_MGR._lock:
                            state.fail_count = max(0, state.fail_count - 1)
                    card.retry_count += 1
                    card.mark_pending()
                    DOWNLOAD_MGR.download_queue.put((idx, item))
                    threading.Thread(target=DOWNLOAD_MGR._download_worker, daemon=True).start()
                elif item.get("source_link"):
                    # Delegate entirely to resolve_card_retry — it handles fail_count, retry_count, state
                    DOWNLOAD_MGR.resolve_card_retry(idx, item.get("source_link"), client_id=client_id)
                else:
                    card.mark_failed("No source link available for retry")

                self.send_json({"status": "success"})
            except Exception as e:
                self.send_json({"error": str(e)}, 500)
            return

        # 4. Retry all failed cards concurrently
        if parsed.path == '/api/retry-all':
            try:
                data = json.loads(body)
                client_id = data.get("clientId", "anonymous")
                state = DOWNLOAD_MGR.get_client_state(client_id)
                retried = 0
                needs_download_worker = False
                for idx, card in enumerate(state.cards):
                    if card.state == 3:  # Failed state
                        item = card.item_data
                        if not item:
                            continue
                        retried += 1
                        if item.get("method") == "TELEGRAM":
                            with DOWNLOAD_MGR._lock:
                                state.fail_count = max(0, state.fail_count - 1)
                            card.retry_count += 1
                            card.mark_pending()
                            DOWNLOAD_MGR.start_telegram_manual(idx, is_retry=True, client_id=client_id)
                        elif item.get("download_url"):
                            with DOWNLOAD_MGR._lock:
                                state.fail_count = max(0, state.fail_count - 1)
                            card.retry_count += 1
                            card.mark_pending()
                            DOWNLOAD_MGR.download_queue.put((idx, item))
                            needs_download_worker = True
                        elif item.get("source_link"):
                            # Delegate entirely to resolve_card_retry — it handles fail_count, retry_count, state
                            DOWNLOAD_MGR.resolve_card_retry(idx, item.get("source_link"), client_id=client_id)
                
                if needs_download_worker:
                    threading.Thread(target=DOWNLOAD_MGR._download_worker, daemon=True).start()
                self.send_json({"status": "success", "retried": retried})
            except Exception as e:
                self.send_json({"error": str(e)}, 500)
            return

        self.send_response(404)
        self.end_headers()


# Threaded TCPServer helper to support concurrent requests beautifully
class ThreadedHTTPServer(socketserver.ThreadingMixIn, HTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        """Suppress noisy BrokenPipe/ConnectionReset tracebacks from aborted client connections."""
        import sys
        exc_type = sys.exc_info()[0]
        if exc_type in (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            return  # Client disconnected early — completely expected, no log needed
        super().handle_error(request, client_address)


def main():
    print(r"""
    __  ___            _             __            __      __
   /  |/  /___ _   __(_)____  _____/ /_________ _/ /_____/ /____
  / /|_/ / __ \ | / / / ___/ / ___/ __/ ___/ __ `/ //_/ _  / ___/
 / /  / / /_/ / |/ / (__  ) / /__/ /_/ /  / /_/ / ,< / /_/ / /
/_/  /_/\____/|___/_/____/  \___/\__/_/   \__,_/_/|_|\__,_/_/
                                                             
    """, flush=True)
    print("=== MoviesCrackd Standalone Web Downloader Server ===", flush=True)

    # Initialize and pre-warm persistent trending marquee cache & midnight scheduler
    start_cache_scheduler()

    # Warn user about default credentials in log security
    admin_user = os.getenv("ADMIN_USERNAME", "admin").strip()
    admin_pass = os.getenv("ADMIN_PASSWORD", "").strip()
    if not admin_pass:
        print("[!] SECURITY NOTICE: ADMIN_PASSWORD is not set in your .env file.", flush=True)
        print("[!] Access to /logs, /api/logs, /api/storage-stats, and /api/clear-server-cache is restricted.", flush=True)
        print(f"[!] Default credentials enabled -> Username: '{admin_user}' | Password: 'admin123'", flush=True)
        print("[!] Please set 'ADMIN_PASSWORD=your_secure_password' in your .env file to customize.\n", flush=True)

    port = int(os.environ.get("PORT", 5555))
    bind_ip = os.environ.get("BIND_ADDRESS", "").strip()
    server_address = (bind_ip, port)
    
    display_ip = bind_ip if bind_ip else "0.0.0.0 (all network interfaces)"
    print(f"[*] Binding server to {display_ip}:{port}...", flush=True)
    
    try:
        httpd = ThreadedHTTPServer(server_address, APIRequestHandler)
    except OSError as e:
        print(f"[-] Error starting server on {display_ip}:{port}: {e}", flush=True)
        sys.exit(1)

    print(f"[+] Server started successfully at http://{'localhost' if not bind_ip else bind_ip}:{port}", flush=True)
    
    # Auto-open browser window in background only if not in cloud mode
    if not DOWNLOAD_MGR.cloud_mode:
        print("[*] Automatically launching your browser window...", flush=True)
        webbrowser.open(f"http://localhost:{port}")

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[-] Keyboard interrupt received. Shutting down server...", flush=True)
        httpd.server_close()
        sys.exit(0)

if __name__ == "__main__":
    main()
