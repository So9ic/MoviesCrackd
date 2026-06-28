#!/usr/bin/env python3
"""Direct downloader for driveseed.org links.

Extracts googleusercontent download URLs from driveseed.org pages
and downloads files sequentially.

Usage:
    # Download from a single driveseed URL
    python3 direct_downloader.py "https://driveseed.org/file/XXXX"

    # Download from a ModPro/AnimeFlix page (resolves + downloads all)
    python3 direct_downloader.py "https://episodes.modpro.blog/archives/XXXXX"
    python3 direct_downloader.py "https://episodes.animeflix.dad/archives/XXXXX"

    # Optional: specify output directory
    python3 direct_downloader.py URL -o /path/to/downloads
"""

import os
import re
import sys
import time
import requests
from urllib.parse import urlparse, parse_qs, unquote, urljoin

from env_loader import load_env_file

load_env_file()

# import batch resolver if available
try:
    from batch_episodes import scrape_links, resolve_link
    from concurrent.futures import ThreadPoolExecutor, as_completed
    HAS_BATCH = True
except ImportError:
    HAS_BATCH = False

try:
    from skip_shortener import bypass_shortener
    HAS_BYPASS = True
except ImportError:
    HAS_BYPASS = False


HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
        'AppleWebKit/537.36 (KHTML, like Gecko) '
        'Chrome/131.0.0.0 Safari/537.36'
    ),
}

# Connection-pooled session for speed (reuses TCP + SSL)
SESSION = requests.Session()
SESSION.headers.update(HEADERS)
_adapter = requests.adapters.HTTPAdapter(pool_connections=20, pool_maxsize=20)
SESSION.mount('https://', _adapter)
SESSION.mount('http://', _adapter)


def _follow_download_link(download_url: str) -> str | None:
    """Follow a video-leech/video-seed redirect to get the final download URL, using proxy fallbacks if needed."""
    # Get proxy from batch_episodes if set
    proxies = None
    try:
        import batch_episodes
        if batch_episodes.WORKING_PROXY:
            p = batch_episodes.WORKING_PROXY
            proxies = {'http': f'http://{p}', 'https': f'http://{p}'}
    except Exception:
        pass

    # Try following redirect (using proxy if available)
    html = ""
    final_url = download_url
    try:
        redirect_resp = SESSION.get(download_url, allow_redirects=True, proxies=proxies, timeout=15)
        final_url = redirect_resp.url
        html = redirect_resp.text
    except Exception as e:
        print(f"    [-] Direct request or primary proxy failed for download link: {e}")
        # Try finding a working proxy from proxy list
        try:
            import batch_episodes
            fetched = batch_episodes.get_proxy_list()
            if fetched:
                p = batch_episodes.find_working_proxy_for_url(download_url, fetched)
                if p:
                    print(f"    [+] Found working proxy for download redirect: {p}")
                    batch_episodes.WORKING_PROXY = p
                    proxies = {'http': f'http://{p}', 'https': f'http://{p}'}
                    try:
                        redirect_resp = SESSION.get(download_url, allow_redirects=True, proxies=proxies, timeout=15)
                        final_url = redirect_resp.url
                        html = redirect_resp.text
                    except Exception as e2:
                        print(f"    [-] Proxy attempt failed for download redirect: {e2}")
                        return None
                else:
                    return None
            else:
                return None
        except Exception as e_proxy:
            print(f"    [-] Error in proxy fallback for download redirect: {e_proxy}")
            return None

    try:
        # Extract ?url= parameter from video-seed
        if 'video-seed' in final_url:
            parsed = urlparse(final_url)
            qs = parse_qs(parsed.query)
            if 'url' in qs:
                return qs['url'][0]

        # Handle JS redirect: window.location.href = "/upload?url=..."
        js_match = re.search(
            r'(?:window\.)?location(?:\.replace|\.href)?\s*[\(=]\s*["\']([^"\']+)["\']',
            html,
            re.IGNORECASE
        )
        if js_match:
            relative_target = js_match.group(1)
            next_url = urljoin(final_url, relative_target)
            print(f"    [+] Following JS redirect to: {next_url}")
            # Fetch the target page using proxy/direct session
            resp = SESSION.get(next_url, allow_redirects=True, proxies=proxies, timeout=15)
            final_url = resp.url
            html = resp.text

        # Try fetching the page for a googleusercontent link
        if 'googleusercontent.com' not in final_url:
            gc_match = re.search(
                r'href=["\']([^"\']*googleusercontent\.com[^"\']*)["\']',
                html,
            )
            if gc_match:
                return gc_match.group(1)
            
            # Check url query param (if redirected to videoseed with ?url=)
            parsed = urlparse(final_url)
            qs = parse_qs(parsed.query)
            if 'url' in qs and 'googleusercontent.com' in qs['url'][0]:
                return qs['url'][0]

        if 'googleusercontent.com' in final_url:
            return final_url

    except Exception as e:
        print(f"    [-] Error parsing download redirect page: {e}")
        
    return None


def _parse_human_size_to_bytes(size_text: str) -> int | None:
    """Parse sizes like '121.6MB', '1.2 GB', '700 MiB' into bytes."""
    m = re.search(r'(\d+(?:\.\d+)?)\s*([kmgt]?i?b)', size_text.strip(), re.IGNORECASE)
    if not m:
        return None

    value = float(m.group(1))
    unit = m.group(2).lower()

    multipliers = {
        'b': 1,
        'kb': 1000,
        'mb': 1000 ** 2,
        'gb': 1000 ** 3,
        'tb': 1000 ** 4,
        'kib': 1024,
        'mib': 1024 ** 2,
        'gib': 1024 ** 3,
        'tib': 1024 ** 4,
    }
    factor = multipliers.get(unit)
    if not factor:
        return None
    return int(value * factor)


def _extract_filename_and_size(html: str) -> tuple[str | None, int | None]:
    title_match = re.search(r'<title>([^<]+)</title>', html)
    filename = title_match.group(1).strip() if title_match else None

    name_match = re.search(r'Name\s*:\s*([^\n<]+)', html)
    if name_match:
        filename = name_match.group(1).strip()

    size_bytes = None
    size_match = re.search(r'Size\s*:\s*([^\n<]+)', html, re.IGNORECASE)
    if size_match:
        size_bytes = _parse_human_size_to_bytes(size_match.group(1))

    og_title_match = re.search(
        r'<meta\s+property=["\']og:title["\']\s+content=["\']([^"\']+)["\']',
        html,
        re.IGNORECASE,
    )
    if og_title_match:
        og_title = og_title_match.group(1).strip()

        # Often formatted like: "Download <filename> - 121.6MB"
        if not filename:
            name_from_og = re.sub(r'^\s*download\s+', '', og_title, flags=re.IGNORECASE)
            name_from_og = re.sub(r'\s*-\s*\d+(?:\.\d+)?\s*[kmgt]?i?b\s*$', '', name_from_og, flags=re.IGNORECASE)
            filename = name_from_og.strip() or filename

        if size_bytes is None:
            size_bytes = _parse_human_size_to_bytes(og_title)

    return filename, size_bytes


def get_driveseed_file_metadata(driveseed_url: str, retries: int = 2) -> tuple[str | None, int | None]:
    """Return (filename, size_bytes) parsed from a driveseed file page."""
    last_error = None

    for attempt in range(1, retries + 2):
        try:
            resp = SESSION.get(driveseed_url, timeout=30)
            resp.raise_for_status()
            html = resp.text

            # Handle /r?key=...&id=... pages → JS redirect to /file/XXX
            js_redirect = re.search(r'window\.location\.replace\(["\']([^"\']+)["\']\)', html)
            if js_redirect:
                redirect_path = js_redirect.group(1)
                if redirect_path.startswith('/'):
                    p = urlparse(driveseed_url)
                    file_url = f"{p.scheme}://{p.netloc}{redirect_path}"
                else:
                    file_url = redirect_path
                resp = SESSION.get(file_url, timeout=30)
                resp.raise_for_status()
                html = resp.text

            return _extract_filename_and_size(html)

        except Exception as e:
            last_error = e
            if attempt <= retries:
                time.sleep(0.5 * attempt)

    if last_error:
        return None, None
    return None, None


def get_driveseed_download_url(driveseed_url: str, retries: int = 2) -> tuple[str, str, str]:
    """
    Given a driveseed.org URL, extract the direct download URL.

    Returns (download_url, filename, method) where method is 'V2', 'V1', 'CLOUD', 'seed', or 'TELEGRAM'.

    Tries Instant Download V2 first. If it fails, falls back to
    Instant Download (V1). Retries on network errors.
    """
    last_error = None

    for attempt in range(1, retries + 2):  # retries + 1 attempts total
        try:
            resp = SESSION.get(driveseed_url, timeout=30)
            resp.raise_for_status()
            html = resp.text

            # Handle /r?key=...&id=... pages → JS redirect to /file/XXX
            js_redirect = re.search(r'window\.location\.replace\(["\']([^"\']+)["\']\)', html)
            if js_redirect:
                redirect_path = js_redirect.group(1)
                if redirect_path.startswith('/'):
                    p = urlparse(driveseed_url)
                    file_url = f"{p.scheme}://{p.netloc}{redirect_path}"
                else:
                    file_url = redirect_path
                resp = SESSION.get(file_url, timeout=30)
                resp.raise_for_status()
                html = resp.text
            filename, _ = _extract_filename_and_size(html)

            # If the page contains a "/zfile/" link (Resume Cloud), fetch that page too
            zfile_match = re.search(r'href=["\']([^"\']*/zfile/[^"\']+)["\']', html)
            if zfile_match:
                zfile_path = zfile_match.group(1)
                if zfile_path.startswith('/'):
                    p = urlparse(driveseed_url)
                    zfile_url = f"{p.scheme}://{p.netloc}{zfile_path}"
                else:
                    zfile_url = zfile_path
                
                print(f"    [+] Found Resume Cloud link, fetching zfile page: {zfile_url}")
                try:
                    zfile_resp = SESSION.get(zfile_url, timeout=30)
                    zfile_resp.raise_for_status()
                    html += "\n" + zfile_resp.text
                except Exception as ze:
                    print(f"    [-] Failed to fetch zfile page: {ze}")

            # Collect ALL download links (ordered by priority)
            methods = {
                'V2': [],
                'V1': [],
                'CLOUD': [],
                'seed': [],
                'TELEGRAM': []
            }
            seen_urls = set()

            # 1. Parse all anchor <a> tags to extract links based on their text labels (fully dynamic!)
            anchor_pattern = r'<a[^>]+href=["\']([^"\']+)["\'][^>]*>(.*?)</a>'
            for match in re.finditer(anchor_pattern, html, re.IGNORECASE | re.DOTALL):
                href = match.group(1).strip()
                if not href or href.startswith('#') or href.startswith('javascript:'):
                    continue

                if href.startswith('/'):
                    p = urlparse(driveseed_url)
                    href = f"{p.scheme}://{p.netloc}{href}"

                raw_label = match.group(2)
                # Strip HTML tags (e.g. <i>), extra whitespaces and convert to lowercase
                clean_label = ' '.join(re.sub(r'<[^>]+>', '', raw_label).split()).strip().lower()

                if href in seen_urls:
                    continue

                # Classify based on clean label
                if 'instant download v2' in clean_label:
                    methods['V2'].append(href)
                    seen_urls.add(href)
                elif 'instant download' in clean_label:
                    methods['V1'].append(href)
                    seen_urls.add(href)
                elif 'cloud' in clean_label or 'resume' in clean_label:
                    methods['CLOUD'].append(href)
                    seen_urls.add(href)
                elif 'telegram' in clean_label:
                    methods['TELEGRAM'].append(href)
                    seen_urls.add(href)
                elif 'video-seed' in href.lower():
                    methods['seed'].append(href)
                    seen_urls.add(href)

            # 2. Fallbacks: Regex searches for specific patterns if they were not captured in <a> labels
            
            # Direct Cloud URLs (like R2.dev links) embedded in text/scripts
            for m in re.finditer(r'https?://[^\s"\'<>]+\.r2\.dev/[^\s"\'<>]+', html, re.IGNORECASE):
                url = m.group(0)
                if url not in seen_urls:
                    methods['CLOUD'].append(url)
                    seen_urls.add(url)

            # Generic Telegram links embedded in text/scripts (tgseed.link)
            for m in re.finditer(r'https?://[^\s"\'<>]*tgseed\.link/[^\s"\'<>]*', html, re.IGNORECASE):
                url = m.group(0)
                if url not in seen_urls:
                    methods['TELEGRAM'].append(url)
                    seen_urls.add(url)

            # Generic instant/cdn domain patterns as robust regex fallbacks
            v2_match = re.search(r'href=["\']([^"\']*instant\.video-[a-zA-Z0-9-]+\.[a-z]+[^"\']*)["\']', html, re.IGNORECASE)
            if v2_match:
                url = v2_match.group(1)
                if url.startswith('/'):
                    p = urlparse(driveseed_url)
                    url = f"{p.scheme}://{p.netloc}{url}"
                if url not in seen_urls:
                    methods['V2'].append(url)
                    seen_urls.add(url)

            v1_match = re.search(r'href=["\']([^"\']*cdn\.video-[a-zA-Z0-9-]+\.[a-z]+[^"\']*)["\']', html, re.IGNORECASE)
            if v1_match:
                url = v1_match.group(1)
                if url.startswith('/'):
                    p = urlparse(driveseed_url)
                    url = f"{p.scheme}://{p.netloc}{url}"
                if url not in seen_urls:
                    methods['V1'].append(url)
                    seen_urls.add(url)

            vs_match = re.search(r'href=["\']([^"\']*video-seed\.[a-z]+[^"\']*)["\']', html, re.IGNORECASE)
            if vs_match:
                url = vs_match.group(1)
                if url.startswith('/'):
                    p = urlparse(driveseed_url)
                    url = f"{p.scheme}://{p.netloc}{url}"
                if url not in seen_urls:
                    methods['seed'].append(url)
                    seen_urls.add(url)

            # Combine them in priority order
            download_candidates = []
            for method in ['V2', 'V1', 'CLOUD', 'seed', 'TELEGRAM']:
                for url in methods[method]:
                    download_candidates.append((method, url))

            if not download_candidates:
                raise ValueError(f"No download links found on {driveseed_url}")

            # Try each candidate in priority order
            for method, dl_url in download_candidates:
                if method == 'CLOUD' and ('.r2.dev/' in dl_url or 'workers.dev/' in dl_url):
                    return dl_url, filename, method
                if method == 'TELEGRAM' and 'tgseed.link' in dl_url:
                    return dl_url, filename, method
                final_url = _follow_download_link(dl_url)
                if final_url:
                    return final_url, filename, method

            raise ValueError(f"All download methods failed ({', '.join(m for m, _ in download_candidates)})")

        except Exception as e:
            last_error = e
            if attempt <= retries:
                time.sleep(0.5 * attempt)  # backoff

    raise last_error or ValueError(f"Failed after {retries + 1} attempts")


def download_file(url: str, filename: str, output_dir: str):
    """Download a file with progress display and resume support."""
    filepath = os.path.join(output_dir, filename)

    # Check if already exists
    if os.path.exists(filepath):
        print(f"    ⏭  Already exists, skipping")
        return filepath

    part_filepath = filepath + '.part'
    downloaded = 0
    write_mode = 'wb'
    headers = HEADERS.copy()

    # Check if a partial file exists and get its size to resume
    if os.path.exists(part_filepath):
        downloaded = os.path.getsize(part_filepath)
        if downloaded > 0:
            write_mode = 'ab'
            headers['Range'] = f"bytes={downloaded}-"
            print(f"    Resuming from: {downloaded / (1024 * 1024):.1f} MB")

    resp = requests.get(url, headers=headers, stream=True, timeout=120)
    
    # Process responses for partial download or normal download
    if resp.status_code == 416:  # Range Not Satisfiable (e.g. file already fully downloaded)
        print("    Range not satisfiable, starting from scratch")
        downloaded = 0
        write_mode = 'wb'
        resp = requests.get(url, headers=HEADERS, stream=True, timeout=120)
        resp.raise_for_status()
    elif resp.status_code == 206:
        # Partial Content
        content_length = int(resp.headers.get('content-length', 0))
        total = content_length + downloaded
    else:
        # 200 OK (server does not support Range or requested start was 0)
        if downloaded > 0:
            print("    Server does not support resuming, starting from scratch")
        resp.raise_for_status()
        downloaded = 0
        write_mode = 'wb'
        total = int(resp.headers.get('content-length', 0))

    chunk_size = 1024 * 1024  # 1MB chunks

    with open(part_filepath, write_mode) as f:
        for chunk in resp.iter_content(chunk_size=chunk_size):
            if chunk:
                f.write(chunk)
                downloaded += len(chunk)
                if total:
                    pct = (downloaded / total) * 100
                    mb_down = downloaded / (1024 * 1024)
                    mb_total = total / (1024 * 1024)
                    print(f"\r    [{pct:5.1f}%] {mb_down:.1f}/{mb_total:.1f} MB", end='', flush=True)
                else:
                    mb_down = downloaded / (1024 * 1024)
                    print(f"\r    {mb_down:.1f} MB downloaded", end='', flush=True)

    # Rename from .part to final name
    os.rename(part_filepath, filepath)
    print(f"\n    ✓ Done!")
    return filepath


def resolve_modpro_page(page_url: str) -> list[str]:
    """Resolve a ModPro/AnimeFlix page to a list of driveseed URLs."""
    if not HAS_BATCH:
        print("ERROR: batch_episodes.py not found. Cannot resolve ModPro pages.")
        sys.exit(1)

    print("[*] Scraping and resolving links from page...")
    links = scrape_links(page_url)

    if not links:
        print("ERROR: No download links found!")
        sys.exit(1)

    print(f"    Found {len(links)} link(s), resolving...\n")

    resolved = [None] * len(links)

    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = {
            pool.submit(resolve_link, i, link): i
            for i, link in enumerate(links)
        }
        for future in as_completed(futures):
            idx, name, url = future.result()
            resolved[idx] = {"name": name, "url": url}
            status = "✓" if url else "✗"
            print(f"  {status} {name}")

    # Return only resolved driveseed URLs
    return [r["url"] for r in resolved if r["url"] and 'driveseed.org' in r["url"]]


def main():
    import argparse
    parser = argparse.ArgumentParser(description='Download files from driveseed.org / ModPro / AnimeFlix')
    parser.add_argument('url', help='driveseed.org URL or ModPro/AnimeFlix page URL')
    parser.add_argument('-o', '--output', default='.', help='Output directory (default: current dir)')
    args = parser.parse_args()

    url = args.url
    output_dir = os.path.abspath(args.output)
    os.makedirs(output_dir, exist_ok=True)

    print("=" * 60)
    print("  Direct Downloader")
    print("=" * 60)
    print(f"\n  Output: {output_dir}\n")

    # Determine what kind of URL we have
    driveseed_urls = []

    if 'driveseed.org' in url:
        # Direct driveseed URL
        driveseed_urls = [url]
    else:
        # Attempt to resolve as archive/episode list page dynamically
        try:
            driveseed_urls = resolve_modpro_page(url)
        except Exception:
            driveseed_urls = []
        
        if not driveseed_urls:
            print(f"ERROR: Unrecognized URL format or no download links found: {url}")
            sys.exit(1)

    if not driveseed_urls:
        print("No driveseed.org links found to download.")
        sys.exit(1)

    print(f"\n{'─' * 60}")
    print(f"  Downloading {len(driveseed_urls)} file(s)...")
    print(f"{'─' * 60}\n")

    for i, ds_url in enumerate(driveseed_urls, 1):
        print(f"── [{i}/{len(driveseed_urls)}] ──")
        print(f"    Source: {ds_url[:80]}...")

        try:
            download_url, filename, method = get_driveseed_download_url(ds_url)

            if not filename:
                # Extract filename from URL
                filename = os.path.basename(urlparse(download_url).path) or f"download_{i}"

            # Sanitize filename
            filename = re.sub(r'[<>:"/\\|?*]', '_', filename)

            print(f"    File:   {filename}  [{method}]")
            print(f"    URL:    {download_url[:80]}...")

            download_file(download_url, filename, output_dir)

        except Exception as e:
            print(f"    ✗ FAILED: {e}")

        print()

    print("=" * 60)
    print("  All downloads complete!")
    print("=" * 60)


if __name__ == "__main__":
    main()
