#!/usr/bin/env python3
"""
Movie Search & Dynamic Option Extraction Engine.
Resolves domains from modlist.in, queries them in parallel,
and extracts download qualities and links from movie/series pages.
"""

import re
import requests
import time
from urllib.parse import urljoin, quote_plus
from concurrent.futures import ThreadPoolExecutor, as_completed

# Global to cache the successful proxy and domains
WORKING_PROXY = None
RESOLVED_DOMAINS = {}
FAILED_DOMAINS = {}  # domain -> timestamp of failure



def test_proxy(page_url, p):
    """Test if a proxy successfully resolves the target page."""
    try:
        p_dict = {'http': f'http://{p}', 'https': f'http://{p}'}
        resp = requests.get(
            page_url,
            headers={
                'User-Agent': (
                    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                    'AppleWebKit/537.36 (KHTML, like Gecko) '
                    'Chrome/131.0.0.0 Safari/537.36'
                ),
            },
            proxies=p_dict,
            timeout=(1.5, 2.5)
        )
        if resp.status_code == 200 and "Just a moment..." not in resp.text:
            return p, resp.text
    except Exception:
        pass
    return None, None


def fetch_with_fallback(url, max_proxies=50):
    """Fetch URL directly with a fast timeout, falling back to parallel proxy resolution if failed."""
    global WORKING_PROXY
    
    # 1. Try with cached working proxy if available
    if WORKING_PROXY:
        try:
            p_dict = {'http': f'http://{WORKING_PROXY}', 'https': f'http://{WORKING_PROXY}'}
            resp = requests.get(
                url,
                headers={
                    'User-Agent': (
                        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                        'AppleWebKit/537.36 (KHTML, like Gecko) '
                        'Chrome/131.0.0.0 Safari/537.36'
                    ),
                },
                proxies=p_dict,
                timeout=(3.05, 6)
            )
            if resp.status_code == 200 and "Just a moment..." not in resp.text:
                return resp.text
        except Exception:
            WORKING_PROXY = None  # reset if failed

    # 2. Try direct request
    try:
        resp = requests.get(
            url,
            headers={
                'User-Agent': (
                    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                    'AppleWebKit/537.36 (KHTML, like Gecko) '
                    'Chrome/131.0.0.0 Safari/537.36'
                ),
            },
            timeout=(3.05, 6)
        )
        if resp.status_code == 200 and "Just a moment..." not in resp.text:
            return resp.text
    except Exception as e:
        print(f"    [-] Direct request to {url} failed: {e}")

    # 3. Fallback to parallel proxy resolution
    print(f"    [!] Direct fetch blocked. Launching parallel proxy resolver for: {url}")
    try:
        proxy_url = 'https://api.proxyscrape.com/v2/?request=displayproxies&protocol=http&timeout=5000&country=all&ssl=all&anonymity=all'
        r = requests.get(proxy_url, timeout=(1.5, 2.5))
        proxies = [p.strip() for p in r.text.strip().split('\n') if p.strip()]
        
        import random
        random.shuffle(proxies)
        candidate_proxies = proxies[:20]
        
        with ThreadPoolExecutor(max_workers=25) as executor:
            futures = {executor.submit(test_proxy, url, p): p for p in candidate_proxies}
            for future in as_completed(futures):
                p, text = future.result()
                if text:
                    WORKING_PROXY = p
                    print(f"    [+] Resolved page via proxy: {p}")
                    for f in futures:
                        f.cancel()
                    return text
    except Exception as e:
        print(f"    [-] Proxy fallback failed for {url}: {e}")

    raise ValueError(f"Failed to fetch content from {url} (Direct and Proxy attempts failed or blocked)")


def resolve_search_domains(force_refresh=False):
    """Fetch modlist.in and dynamically resolve search domains for bollywood, animeflix, and hollywood."""
    global RESOLVED_DOMAINS
    if RESOLVED_DOMAINS and not force_refresh:
        return RESOLVED_DOMAINS

    print("[*] Resolving dynamic domains from modlist.in...")
    try:
        html = fetch_with_fallback('https://modlist.in/')
        
        # Matches sequence: <span class="badge ...">DOMAIN</span> ... href = https://modlist.in/?type=TYPE
        pattern = r'<span\s+class=\"badge[^>]*\">\s*(https?://[^\s<]+)\s*</span>.*?href\s*=\s*[\"\'\s]?https?://modlist\.in/\?type=([a-zA-Z0-9_-]+)'
        matches = re.findall(pattern, html, re.DOTALL)
        
        domains = {}
        for domain, type_name in matches:
            domains[type_name] = domain.rstrip('/')
            
        # Defaults if parsing failed
        if 'bollywood' not in domains:
            domains['bollywood'] = 'https://moviesleech.rodeo'
        if 'animeflix' not in domains:
            domains['animeflix'] = 'https://animeflix.dad'
        if 'hollywood' not in domains:
            domains['hollywood'] = 'https://moviesmod.money'
            
        RESOLVED_DOMAINS = domains
        print(f"[+] Resolved domains successfully: {RESOLVED_DOMAINS}")
        return RESOLVED_DOMAINS
    except Exception as e:
        print(f"[-] Failed to resolve domains from modlist.in, using defaults: {e}")
        # Return fallback defaults
        RESOLVED_DOMAINS = {
            'bollywood': 'https://moviesleech.rodeo',
            'animeflix': 'https://animeflix.dad',
            'hollywood': 'https://moviesmod.money'
        }
        return RESOLVED_DOMAINS


def search_movies(query, categories=None, on_result_callback=None):
    """
    Search for movies/shows in parallel across the specified categories.
    categories: list like ['bollywood', 'animeflix', 'hollywood'] or None (for all)
    """
    domains = resolve_search_domains()
    if not categories:
        categories = ['bollywood', 'animeflix', 'hollywood']
        
    search_query = quote_plus(query)
    results = []
    
    def search_category(cat):
        base_url = domains.get(cat)
        if not base_url:
            return []
            
        # Self-healing domain blacklist check
        current_time = time.time()
        if base_url in FAILED_DOMAINS:
            if current_time - FAILED_DOMAINS[base_url] < 180:  # 3-minute blacklist
                print(f"[*] Skipping category {cat.upper()} (recently failed/offline): {base_url}")
                return []
            else:
                FAILED_DOMAINS.pop(base_url, None)
            
        search_url = f"{base_url}/search/{search_query}"
        print(f"[*] Searching {cat.upper()} at: {search_url}")
        
        cat_results = []
        try:
            html = fetch_with_fallback(search_url)
            articles = re.findall(r'<article[^>]*>(.*?)</article>', html, re.DOTALL)
            
            for art in articles:
                href_match = re.search(r'href=["\']([^"\']+)["\']', art)
                title_match = re.search(r'title=["\']([^"\']+)["\']', art)
                img_match = re.search(r'src=["\']([^"\']+)["\']', art)
                
                if href_match and title_match:
                    title = re.sub(r'&#\d+;', '', title_match.group(1)).strip()  # clean HTML entities
                    title = re.sub(r'Download\s+', '', title, flags=re.IGNORECASE).strip()
                    
                    thumbnail = img_match.group(1) if img_match else ""
                    # Handle lazy loaded images
                    if 'wp-content/uploads' not in thumbnail:
                        lazy_match = re.search(r'data-src=["\']([^"\']+)["\']', art)
                        if lazy_match:
                            thumbnail = lazy_match.group(1)
                            
                    item = {
                        "title": title,
                        "url": href_match.group(1),
                        "thumbnail": thumbnail,
                        "category": cat.upper()
                    }
                    cat_results.append(item)
                    if on_result_callback:
                        on_result_callback(item)
        except Exception as e:
            print(f"[-] Search failed for category {cat}: {e}")
            FAILED_DOMAINS[base_url] = current_time
            
        return cat_results

    # Run in parallel
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = {pool.submit(search_category, cat): cat for cat in categories}
        for future in as_completed(futures):
            results.extend(future.result())
            
    return results


def extract_download_options(detail_url):
    """
    Fetch a movie detail page and extract clean download links mapped to their seasons/qualities.
    Uses backward-traversal heuristic to find corresponding heading tags.
    """
    print(f"[*] Extracting download options from: {detail_url}")
    options = []
    try:
        html = fetch_with_fallback(detail_url)
        
        # Regex to find all backend /archives/ links
        matches = list(re.finditer(r'<a\s+[^>]*href=["\']([^"\']*/archives/\d+)["\'][^>]*>(.*?)</a>', html, re.IGNORECASE | re.DOTALL))
        
        for m in matches:
            href = m.group(1)
            # Remove inner tags from button label (e.g. <span class='mb-text'>)
            btn_text = re.sub(r'<[^>]+>', '', m.group(2)).strip()
            
            # Traversal backward up to 800 characters to find the nearest quality or description header
            lookback = html[max(0, m.start() - 800):m.start()]
            
            # Find all h1-h6 tags in this lookback region
            headers = re.findall(r'<h[1-6][^>]*>(.*?)</h[1-6]>', lookback, re.IGNORECASE | re.DOTALL)
            
            if headers:
                header_text = re.sub(r'<[^>]+>', '', headers[-1]).strip()
                header_text = re.sub(r'\s+', ' ', header_text)  # sanitize spaces
            else:
                header_text = "Direct Download"
                
            # Deduplicate within this page context (avoid adding identical options)
            if not any(opt["url"] == href for opt in options):
                options.append({
                    "quality": header_text,
                    "button_text": btn_text,
                    "url": href
                })
    except Exception as e:
        print(f"[-] Option extraction failed: {e}")
        
    return options


if __name__ == '__main__':
    # Live module testing
    import sys
    query = "the boys" if len(sys.argv) < 2 else sys.argv[1]
    
    print("=" * 60)
    print(f" TESTING MOVIE SEARCH FOR: '{query}'")
    print("=" * 60)
    
    domains = resolve_search_domains()
    results = search_movies(query)
    print(f"\nFound {len(results)} search results:")
    for i, r in enumerate(results[:5]):
        print(f" [{i}] [{r['category']}] {r['title']}")
        print(f"     URL: {r['url']}")
        print(f"     Thumb: {r['thumbnail']}")
        
    if results:
        test_url = results[0]["url"]
        options = extract_download_options(test_url)
        print(f"\nDownload options for '{results[0]['title']}':")
        for i, opt in enumerate(options[:5]):
            print(f"  ({i}) [{opt['button_text']}] {opt['quality']}")
            print(f"      URL: {opt['url']}")
