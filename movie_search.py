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

# High-performance requests session with custom connection pooling and retries
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

HTTP_SESSION = requests.Session()
retries = Retry(total=3, backoff_factor=0.1, status_forcelist=[500, 502, 503, 504])
HTTP_SESSION.mount('http://', HTTPAdapter(pool_connections=15, pool_maxsize=40, max_retries=retries))
HTTP_SESSION.mount('https://', HTTPAdapter(pool_connections=15, pool_maxsize=40, max_retries=retries))



def test_proxy(page_url, p):
    """Test if a proxy successfully resolves the target page."""
    try:
        p_dict = {'http': f'http://{p}', 'https': f'http://{p}'}
        resp = HTTP_SESSION.get(
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
            resp = HTTP_SESSION.get(
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
        resp = HTTP_SESSION.get(
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
        r = HTTP_SESSION.get(proxy_url, timeout=(1.5, 2.5))
        proxies = [p.strip() for p in r.text.strip().split('\n') if p.strip()]
        
        import random
        random.shuffle(proxies)
        # Test only 5 candidate proxies to keep CPU/thread usage extremely minimal on cloud containers
        candidate_proxies = proxies[:5]
        
        with ThreadPoolExecutor(max_workers=5) as executor:
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


class OptionsList(list):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.metadata = {}


def clean_html_to_text(html_content):
    # Replace block-level tags and line breaks with newlines
    text = re.sub(r'<(p|br|li|tr|div|h[1-6]|ul|ol)[^>]*>', '\n', html_content, flags=re.IGNORECASE)
    # Replace all other HTML tags with a single space
    text = re.sub(r'<[^>]+>', ' ', text)
    return text


def extract_download_options(detail_url):
    """
    Fetch a movie detail page and extract clean download links mapped to their seasons/qualities.
    Uses backward-traversal heuristic to find corresponding heading tags.
    """
    import html as html_parser
    print(f"[*] Extracting download options from: {detail_url}")
    options = OptionsList()
    try:
        raw_html = fetch_with_fallback(detail_url)
        # Decode HTML entities (e.g. curly quotes, double primes like &#8221; and &#8243;)
        html = html_parser.unescape(raw_html)
        
        # Extract page-level metadata (Movie/Anime Info)
        try:
            meta = {}
            clean_text = clean_html_to_text(raw_html)
            clean_text = html_parser.unescape(clean_text)
            lines = [line.strip() for line in clean_text.split('\n') if line.strip()]
            for i, line in enumerate(lines):
                if re.search(r'\b(Movie|Anime|Series|Show)\s+Info\b', line, re.IGNORECASE):
                    for j in range(i + 1, min(i + 15, len(lines))):
                        sub_line = lines[j]
                        if ':' in sub_line:
                            parts = sub_line.split(':', 1)
                            key = parts[0].strip().lower().replace(' ', '_')
                            val = parts[1].strip()
                            if key and val and len(key) < 25 and len(val) < 200:
                                if 'info' in key or 'download' in key:
                                    break
                                meta[key] = val
                        else:
                            if meta and not re.search(r'\b(full_name|language|release|year|size|quality|format|director|writer|star|subtitles|status|episodes|aired|premiered|studio|genre|demographic|duration|rating)\b', sub_line, re.IGNORECASE):
                                break
                    if meta:
                        break
            options.metadata = meta
        except Exception as meta_err:
            print(f"[-] Meta parsing failed: {meta_err}")
        
        def _find_best_header(match_start):
            # 1. Search for potential inline headers or label elements (h1-h6, strong, b, p, span) in a tight 800-char lookback
            lookback_inline = html[max(0, match_start - 800):match_start]
            matches = re.findall(r'<(h[1-6]|strong|b|p|span)[^>]*>(.*?)</\1>', lookback_inline, re.IGNORECASE | re.DOTALL)
            
            # Find the closest inline quality label (closest to button)
            quality_label = ""
            for tag, content in reversed(matches):
                clean_text = re.sub(r'<[^>]+>', '', content).strip()
                clean_text = re.sub(r'\s+', ' ', clean_text)
                
                # Strip leading/trailing separator symbols (hyphens, dashes, colons, dots)
                clean_text = clean_text.strip(' -–—:·.').strip()
                
                # Check if this text block looks like a valid quality/resolution/size label
                text_lower = clean_text.lower()
                has_res = any(r in text_lower for r in ['480p', '720p', '1080p', '2160p', '4k', '8k']) or bool(re.search(r'\b(480|720|1080|2160)\b', text_lower))
                has_size = bool(re.search(r'\b\d+(?:\.\d+)?\s*(?:mb|gb)\b', text_lower))
                
                if clean_text and (has_res or has_size):
                    # Normalize standalone resolutions (e.g. "720" -> "720p")
                    if re.match(r'^(480|720|1080|2160)$', clean_text):
                        clean_text = clean_text + 'p'
                    quality_label = clean_text
                    break
            
            # 2. Find the closest preceding parent heading tag in the entire document preceding the match
            html_before = html[:match_start]
            h_matches = list(re.finditer(r'<h[1-6][^>]*>(.*?)</h[1-6]>', html_before, re.IGNORECASE | re.DOTALL))
            
            heading_label = ""
            if h_matches:
                closest_h = h_matches[-1]
                
                # Sanity check: parent heading shouldn't be miles away (max 4000 characters)
                if match_start - closest_h.end() < 4000:
                    clean_text = re.sub(r'<[^>]+>', '', closest_h.group(1)).strip()
                    heading_label = re.sub(r'\s+', ' ', clean_text)
                    
                    # Check if this heading is essentially just a quality/resolution label (e.g. "720p x265")
                    hl_lower = heading_label.lower()
                    structural_words = ['season', 'episode', 'ep', 'complete', 'batch', 'ova', 'movie']
                    has_structural = any(w in hl_lower for w in structural_words)
                    has_res_indicator = any(r in hl_lower for r in ['480p', '720p', '1080p', '2160p', '4k', '8k'])
                    
                    is_just_quality = has_res_indicator and not has_structural and len(heading_label) < 25
                    
                    if is_just_quality and len(h_matches) > 1:
                        prev_h = h_matches[-2]
                        if match_start - prev_h.end() < 4000:
                            prev_text = re.sub(r'<[^>]+>', '', prev_h.group(1)).strip()
                            prev_label = re.sub(r'\s+', ' ', prev_text)
                            prev_lower = prev_label.lower()
                            
                            # If the previous heading contains structural keywords (season, episode, special, bonus, etc.)
                            structural_keywords = ['season', 'episode', 'special', 'bonus', 'pack', 'batch', 'ova', 'movie']
                            if any(k in prev_lower for k in structural_keywords):
                                heading_label = f"{prev_label} - {heading_label}"
            
            # 3. Combine them if appropriate
            if heading_label and quality_label:
                h_lower = heading_label.lower()
                q_lower = quality_label.lower()
                
                # If they are essentially the same or one contains the other, use heading
                if q_lower in h_lower or h_lower in q_lower:
                    return heading_label
                
                # Filter out generic page-level or standard headings we don't want to append
                generic_headings = ['storyline', 'screenshots', 'screen shots', 'trailer', 'download', 'direct download']
                if not any(g in h_lower for g in generic_headings):
                    return f"{heading_label} - {quality_label}"
            
            if quality_label:
                return quality_label
            if heading_label:
                return heading_label
                
            return "Direct Download"

        # 1. Standard matches
        matches = list(re.finditer(r'<a\s+[^>]*href=["\']([^"\']*/archives/\d+)["\'][^>]*>(.*?)</a>', html, re.IGNORECASE | re.DOTALL))
        
        for m in matches:
            href = m.group(1)
            # Remove inner tags from button label (e.g. <span class='mb-text'>)
            btn_text = re.sub(r'<[^>]+>', '', m.group(2)).strip()
            
            header_text = _find_best_header(m.start())
                
            # Deduplicate within this page context (avoid adding identical options)
            if not any(opt["url"] == href for opt in options):
                options.append({
                    "quality": header_text,
                    "button_text": btn_text,
                    "url": href
                })

        # 2. Match unrendered or malformed WordPress [maxbutton] shortcodes
        shortcode_matches = list(re.finditer(r'\[maxbutton[^\]]*url=["\'“”](https?://[^"\'“”]*/archives/\d+)', html, re.IGNORECASE))
        for sm in shortcode_matches:
            href = sm.group(1)
            
            start_pos = sm.start()
            end_bracket = html.find(']', start_pos)
            if end_bracket != -1 and end_bracket - start_pos < 500:
                shortcode_block = html[start_pos:end_bracket+1]
            else:
                shortcode_block = html[start_pos:start_pos+300]
                
            text_match = re.search(r'text=["\'“”]([^"\'“”]+)["\'“”]', shortcode_block, re.IGNORECASE)
            btn_text = text_match.group(1).strip() if text_match else "Download"
            
            header_text = _find_best_header(start_pos)
                
            if not any(opt["url"] == href for opt in options):
                options.append({
                    "quality": header_text,
                    "button_text": btn_text,
                    "url": href
                })

        # 3. Fallback: scan for any raw /archives/ links in text that might have been missed
        raw_matches = list(re.finditer(r'(https?://[a-zA-Z0-9.-]+/archives/(\d+))', html, re.IGNORECASE))
        for rm in raw_matches:
            href = rm.group(1)
            if any(opt["url"] == href for opt in options):
                continue
                
            start_pos = rm.start()
            lookback_small = html[max(0, start_pos - 100):start_pos]
            if '<!--' in lookback_small or '<script' in lookback_small or '<style' in lookback_small:
                continue
                
            btn_text = "Download"
            header_text = _find_best_header(start_pos)
                
            options.append({
                "quality": header_text,
                "button_text": btn_text,
                "url": href
            })
    except Exception as e:
        print(f"[-] Option extraction failed: {e}")
        
    return options


def fetch_trending_movies(categories=None):
    """
    Fetch trending/latest movies and shows from the home pages of Bollywood, Hollywood, AnimeFlix.
    """
    domains = resolve_search_domains()
    if not categories:
        categories = ['bollywood', 'animeflix', 'hollywood']
        
    results = []
    
    def scrape_home(cat):
        base_url = domains.get(cat)
        if not base_url:
            return []
            
        print(f"[*] Fetching home page for trending {cat.upper()} at: {base_url}")
        
        cat_results = []
        try:
            html = fetch_with_fallback(base_url)
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
        except Exception as e:
            print(f"[-] Fetching home page failed for category {cat}: {e}")
            
        return cat_results

    # Run in parallel
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = {pool.submit(scrape_home, cat): cat for cat in categories}
        for future in as_completed(futures):
            res = future.result()
            results.extend(res)
            
    # Randomize results to make it a fun mix
    import random
    random.shuffle(results)
    return results


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
