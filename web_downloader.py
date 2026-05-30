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
    return ist_now.strftime("%Y-%m-%d %H:%M:%S")

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
    user_prefix = f"[User: {client_id}] " if client_id else ""
    formatted_msg = f"{user_prefix}SEARCHED: \"{query_str}\""
    
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
        log_line = f"[{timestamp} IST] {event_message}\n"
        with LOG_LOCK:
            with open(SEARCH_LOGS_FILE, 'a', encoding='utf-8') as f:
                f.write(log_line)
    except Exception as e:
        print(f"[-] Error writing instant search log: {e}", flush=True)

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

def prewarm_dns():
    def _resolve(host):
        try:
            socket.getaddrinfo(host, 443)
        except Exception:
            pass
    with ThreadPoolExecutor(max_workers=len(DNS_DOMAINS)) as pool:
        pool.map(_resolve, DNS_DOMAINS)

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
            "size": size_str
        }


# ── Standalone Download Manager Core ─────────────────────────────────────

class DownloaderBackend:
    def __init__(self):
        self.output_dir = os.path.expanduser("/media/so9ic/HDD/Downloads/Movies")
        self.cloud_mode = os.getenv("CLOUD_MODE", "false").lower() == "true" or "DISPLAY" not in os.environ
        self.cards: list[VirtualDownloadCard] = []
        self.download_queue = PriorityQueue()
        self._active_threads = 0
        self._lock = threading.Lock()
        self._telegram_lock = threading.Lock()

        self._done_count = 0
        self._fail_count = 0
        self._total_count = 0

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
                time.sleep(30)
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

    def start_pipeline(self, url, output_dir):
        self.output_dir = output_dir

        with self._lock:
            self._done_count = 0
            self._fail_count = 0
            self._total_count = 0
            self._active_threads = 0
            self.cards.clear()

        while not self.download_queue.empty():
            try:
                self.download_queue.get_nowait()
            except Empty:
                break

        threading.Thread(target=prewarm_dns, daemon=True).start()
        threading.Thread(target=self._resolve_pipeline, args=(url,), daemon=True).start()

    def _resolve_pipeline(self, url):
        try:
            if "tgseed.link" in url:
                self._resolve_single_telegram_url(url)
                return

            if ".r2.dev/" in url:
                self._resolve_single_cloud_url(url)
                return

            if "driveseed.org" in url:
                self._resolve_single_driveseed(url, 0)
                return

            if not HAS_BATCH:
                print("[-] Batch module missing")
                return

            links = scrape_links(url)
            if not links:
                print("[-] No download links found on page")
                return

            total = len(links)
            with self._lock:
                self._total_count = total

            # Create placeholder cards
            for i in range(total):
                card = VirtualDownloadCard(i + 1, "Resolving…", "…")
                card.set_status("Resolving…")
                self.cards.append(card)

            existing = set()
            if os.path.isdir(self.output_dir):
                existing = set(os.listdir(self.output_dir))

            workers_started = [0]
            workers_lock = threading.Lock()

            def _fused_resolve(i, link):
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
                    fname = meta_fname or os.path.basename(urlparse(dl_url).path) or name or f"download_{i + 1}"
                fname = re.sub(r'[<>:"/\\|?*]', "_", fname)
                
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
                futures = {pool.submit(_fused_resolve, i, lnk): i for i, lnk in enumerate(links)}
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
                    
                    if item.get("download_url"):
                        fname = item["filename"]
                        card = self.cards[idx]
                        card.filename = fname
                        card.set_method(item.get("method", ""))
                        card.item_data = item
                        card.url = item["download_url"]

                        if fname in existing:
                            card.set_detail("Already downloaded")
                            card.mark_done()
                            with self._lock:
                                self._done_count += 1
                        else:
                            if item.get("method") == "TELEGRAM":
                                card.set_status("Manual Telegram")
                                exp = item.get("expected_size_bytes")
                                card.set_detail(f"Click Download to open Telegram Desktop. Expected: {fmt_bytes(exp)}" if exp else "Click Download to open Telegram Desktop.")
                                card.set_action("Download", lambda index=idx: self.start_telegram_manual(index))
                            else:
                                if self.cloud_mode:
                                    card.status = "✓ Ready"
                                    card.state = 2
                                    card.progress = 1.0
                                    card.detail = "Direct link resolved! Click 'Download to Device' below."
                                    with self._lock:
                                        self._done_count += 1
                                else:
                                    card.set_status("Queued")
                                    self.download_queue.put((idx, item))
                                    with workers_lock:
                                        while workers_started[0] < MAX_CONCURRENT:
                                            workers_started[0] += 1
                                            threading.Thread(target=self._download_worker, daemon=True).start()
                    else:
                        card = self.cards[idx]
                        card.filename = item["filename"]
                        card.set_method(item.get("method", ""))
                        card.mark_failed(item.get("error", "Could not resolve"))
                        with self._lock:
                            self._fail_count += 1

        except Exception as e:
            print(f"[-] Resolve error: {e}", flush=True)

    def _resolve_single_driveseed(self, url, idx):
        with self._lock:
            self._total_count = 1
        card = VirtualDownloadCard(1, "Resolving…", "…")
        self.cards.append(card)

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
                "source_index": 0,
                "source_name_hint": fname,
                "source_driveseed_url": url,
            }
            card.filename = fname
            card.set_method(method)
            card.item_data = item
            card.url = dl_url

            existing = set(os.listdir(self.output_dir)) if os.path.isdir(self.output_dir) else set()
            if fname in existing:
                card.mark_done()
                with self._lock:
                    self._done_count += 1
                return

            if self.cloud_mode:
                card.status = "✓ Ready"
                card.state = 2
                card.progress = 1.0
                card.detail = "Direct link resolved! Click 'Download to Device' below."
                with self._lock:
                    self._done_count += 1
                return

            self.download_queue.put((0, item))
            threading.Thread(target=self._download_worker, daemon=True).start()

        except Exception as e:
            card.mark_failed(str(e))
            with self._lock:
                self._fail_count += 1

    def _resolve_single_cloud_url(self, url):
        with self._lock:
            self._total_count = 1
        fname = os.path.basename(urlparse(url).path) or "download_1"
        fname = re.sub(r'[<>:"/\\|?*]', "_", fname)
        card = VirtualDownloadCard(1, fname, "CLOUD")
        self.cards.append(card)

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
        card.item_data = item
        card.url = url

        existing = set(os.listdir(self.output_dir)) if os.path.isdir(self.output_dir) else set()
        if fname in existing:
            card.mark_done()
            with self._lock:
                self._done_count += 1
            return

        if self.cloud_mode:
            card.status = "✓ Ready"
            card.state = 2
            card.progress = 1.0
            card.detail = "Direct link resolved! Click 'Download to Device' below."
            with self._lock:
                self._done_count += 1
            return

        self.download_queue.put((0, item))
        threading.Thread(target=self._download_worker, daemon=True).start()

    def _resolve_single_telegram_url(self, url):
        with self._lock:
            self._total_count = 1
        card = VirtualDownloadCard(1, "Telegram file", "TELEGRAM")
        self.cards.append(card)

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
        card.item_data = item
        card.url = url
        card.set_status("Manual Telegram")
        card.set_detail("Click Download to open Telegram Desktop.")
        card.set_action("Download", lambda: self.start_telegram_manual(0))

    def _download_worker(self):
        while True:
            try:
                idx, item = self.download_queue.get(timeout=5)
            except Empty:
                break

            with self._lock:
                self._active_threads += 1

            card = self.cards[idx]
            card.mark_downloading()

            try:
                url = item["download_url"]
                method = item.get("method", "")
                filename = item["filename"]
                target_dir = item.get("target_dir", self.output_dir)
                filepath = os.path.join(target_dir, filename)
                part_path = filepath + ".part"

                if method == "TELEGRAM" or "tgseed.link" in url:
                    self.start_telegram_manual(idx)
                    continue

                if os.path.exists(filepath):
                    file_size = os.path.getsize(filepath)
                    with self._lock:
                        self._done_count += 1
                        self._active_threads -= 1
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
                    self._done_count += 1
                card.mark_done()
                card.set_detail(f"{total_mb:.1f} MB  •  avg {avg}")

            except Exception as e:
                with self._lock:
                    self._fail_count += 1
                card.mark_failed(str(e))

            finally:
                with self._lock:
                    self._active_threads -= 1

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

    # ── Advanced Telegram Manual Download logic ──
    def start_telegram_manual(self, idx, is_retry=False):
        if idx >= len(self.cards):
            return
        card = self.cards[idx]
        item = card.item_data or {}
        
        if is_retry:
            removed = self._cleanup_telegram_retry_artifacts(item)
            card.set_detail(f"Retrying… cleaned {removed} partial file(s).")
            card.set_progress(0)
            card.set_action("Retrying…")
        else:
            card.set_action("Waiting…")

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
        watch_timeout = max(0, int(watch_timeout_raw)) if watch_timeout_raw else 0

        with self._lock:
            self._active_threads += 1

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
                    self._done_count += 1
                card.mark_done()
                card.set_detail(f"{os.path.getsize(dest_path)/(1024*1024):.1f} MB  •  avg Telegram")

        except Exception as e:
            with self._lock:
                self._fail_count += 1
            card.mark_failed(str(e))
            card.set_action("Retry", lambda index=idx: self.start_telegram_manual(index, is_retry=True))

        finally:
            with self._lock:
                self._active_threads -= 1

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

        # 1c-2. Search Logs display endpoint (Latest logs at the top)
        if parsed.path == '/logs' or parsed.path == '/api/logs':
            self.send_response(200)
            self.send_header('Content-Type', 'text/plain; charset=utf-8')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.send_header('Cache-Control', 'no-cache, no-store, must-revalidate')
            self.end_headers()
            try:
                if os.path.exists(SEARCH_LOGS_FILE):
                    with open(SEARCH_LOGS_FILE, 'r', encoding='utf-8') as f:
                        lines = f.readlines()
                    # Reverse so that latest logs are at the top
                    reversed_lines = reversed(lines)
                    self.wfile.write("".join(reversed_lines).encode('utf-8'))
                else:
                    self.wfile.write(b"No search logs found yet.")
            except Exception as e:
                self.wfile.write(f"Error reading logs: {e}".encode('utf-8'))
            return

        # 1c-3. Record search log silently for cached local searches & details page views
        if parsed.path == '/api/logs/record':
            qs = parse_qs(parsed.query)
            log_type = qs.get("type", ["search"])[0]
            client_id = qs.get("clientId", [None])[0]
            
            if log_type == "detail":
                title = qs.get("title", [""])[0].strip()
                url = qs.get("url", [""])[0].strip()
                if title:
                    log_instant_event(f"[User: {client_id}] VIEWED DETAILS: \"{title}\" ({url})")
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
            self.send_json({
                "done_count": DOWNLOAD_MGR._done_count,
                "fail_count": DOWNLOAD_MGR._fail_count,
                "total_count": DOWNLOAD_MGR._total_count,
                "active_threads": DOWNLOAD_MGR._active_threads,
                "output_dir": DOWNLOAD_MGR.output_dir,
                "cloud_mode": DOWNLOAD_MGR.cloud_mode,
                "telegram": {
                    "text": tg_text,
                    "color": tg_color
                }
            })
            return

        # 3. API Downloads list endpoint
        if parsed.path == '/api/downloads':
            self.send_json({
                "downloads": [card.to_json() for card in DOWNLOAD_MGR.cards]
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

                stats = {
                    "cached_posters_count": cached_posters_count,
                    "cached_posters_size": format_size(cached_posters_size),
                    "imdb_suggest_cache_count": imdb_suggest_cache_count,
                    "imdb_suggest_cache_size": format_size(imdb_suggest_cache_size),
                    "trending_cache_size": format_size(trending_cache_size),
                    "total_project_size": format_size(total_project_size),
                    "server_time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
                }
                self.send_json(stats)
            except Exception as e:
                self.send_json({"error": str(e)}, 500)
            return

        # 4e. Clear Server Cache
        if parsed.path == '/api/clear-server-cache':
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
                output_dir = data.get("output_dir", DOWNLOAD_MGR.output_dir)
                
                if not url:
                    self.send_json({"error": "Missing url body param"}, 400)
                    return

                # Parse download telemetry parameters
                client_id = data.get("clientId")
                title = data.get("title", "Direct URL")
                option_title = data.get("optionTitle", "")
                button_text = data.get("buttonText", "")

                # Record detailed user journey log instantly
                if option_title or button_text:
                    event_msg = f"[User: {client_id}] DOWNLOAD INITIATED: \"{title}\" -> Selection: \"{option_title}\" | Button: \"{button_text}\" (URL: {url})"
                else:
                    event_msg = f"[User: {client_id}] DOWNLOAD INITIATED: \"{title}\" (URL: {url})"
                log_instant_event(event_msg)

                DOWNLOAD_MGR.start_pipeline(url, output_dir)
                self.send_json({"status": "success", "message": "Pipeline initiated"})
            except Exception as e:
                self.send_json({"error": str(e)}, 500)
            return

        # 3. Retry individual failed card
        if parsed.path == '/api/retry':
            try:
                data = json.loads(body)
                idx = int(data.get("index", -1))
                if idx < 0 or idx >= len(DOWNLOAD_MGR.cards):
                    self.send_json({"error": "Invalid index"}, 400)
                    return

                card = DOWNLOAD_MGR.cards[idx]
                item = card.item_data
                
                if not item:
                    self.send_json({"error": "Card has no metadata item data"}, 400)
                    return

                card.mark_pending()
                if item.get("method") == "TELEGRAM":
                    DOWNLOAD_MGR.start_telegram_manual(idx, is_retry=True)
                else:
                    DOWNLOAD_MGR.download_queue.put((idx, item))
                    threading.Thread(target=DOWNLOAD_MGR._download_worker, daemon=True).start()

                self.send_json({"status": "success"})
            except Exception as e:
                self.send_json({"error": str(e)}, 500)
            return

        # 4. Retry all failed cards concurrently
        if parsed.path == '/api/retry-all':
            retried = 0
            for idx, card in enumerate(DOWNLOAD_MGR.cards):
                if card.state == 3:  # Failed state
                    item = card.item_data
                    if not item:
                        continue
                    card.mark_pending()
                    retried += 1
                    if item.get("method") == "TELEGRAM":
                        DOWNLOAD_MGR.start_telegram_manual(idx, is_retry=True)
                    else:
                        DOWNLOAD_MGR.download_queue.put((idx, item))
            
            if retried > 0:
                threading.Thread(target=DOWNLOAD_MGR._download_worker, daemon=True).start()
            self.send_json({"status": "success", "retried": retried})
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

    port = int(os.environ.get("PORT", 5555))
    server_address = ('', port)
    
    try:
        httpd = ThreadedHTTPServer(server_address, APIRequestHandler)
    except OSError as e:
        print(f"[-] Error starting server on port {port}: {e}", flush=True)
        sys.exit(1)

    print(f"[+] Server started successfully on port {port}", flush=True)
    
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
