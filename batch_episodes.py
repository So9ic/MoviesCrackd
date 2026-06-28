#!/usr/bin/env python3
"""Batch link resolver for ModPro / AnimeFlix pages.

Scrapes a page for all download links, resolves shortener SIDs and
redirect links to final destinations, and opens them in browser tabs.

Supports:
  - episodes.modpro.blog   (episode lists)
  - links.modpro.blog      (download server options)
  - episodes.animeflix.dad  (anime episode links via /getlink/ redirects)

Usage:
    python3 batch_episodes.py [page_url]
"""

from urllib.parse import urljoin
import re
import sys
import webbrowser
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests
from skip_shortener import bypass_shortener


WORKING_PROXY = None
CACHED_PROXIES = []

def get_proxy_list() -> list[str]:
    """Fetch a fresh list of HTTP proxies from Proxyscrape."""
    global CACHED_PROXIES
    if CACHED_PROXIES:
        return list(CACHED_PROXIES)
    try:
        proxy_url = 'https://api.proxyscrape.com/v2/?request=displayproxies&protocol=http&timeout=5000&country=all&ssl=all&anonymity=all'
        r = requests.get(proxy_url, timeout=(3.05, 5))
        proxies = [p.strip() for p in r.text.strip().split('\n') if p.strip()]
        CACHED_PROXIES = list(proxies)
        return proxies
    except Exception as e:
        print(f"    [-] Failed to fetch proxy list: {e}")
        return []


# Labels to EXCLUDE — junk/nav links and batch links (not individual episodes)
JUNK_LABELS = re.compile(
    r'(comment\s*section|templatelens|wordpress|privacy|contact|about'
    r'|cookie\s*policy|term|menu|home|navigation'
    r'|all\s*episodes?\s*batch|batch)',
    re.IGNORECASE,
)

# Labels that indicate a real download/episode link
GOOD_LABELS = re.compile(
    r'(episode|ep\.?\s*\d|batch|all\s*episodes|s\d+e\d+'
    r'|complete|full|season'
    r'|server|g-?drive|gdrive|onedrive|one\s*drive|fast|direct'
    r'|download|mega|mediafire|zippy|^\s*\d+\s*$)',
    re.IGNORECASE,
)


def scrape_links(page_url: str) -> list[dict]:
    """
    Scrape a modpro.blog page and return a list of dicts:
    [{"name": "Episode 1", "url": "https://...", "needs_bypass": True/False}, ...]
    """
    html = None

    # Try direct request first (with strict connect/read timeouts to prevent hanging)
    try:
        resp = requests.get(
            page_url,
            headers={
                'User-Agent': (
                    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                    'AppleWebKit/537.36 (KHTML, like Gecko) '
                    'Chrome/131.0.0.0 Safari/537.36'
                ),
            },
            timeout=(3.05, 7)  # strict 3s connect timeout, 7s read timeout
        )
        if resp.status_code == 200 and "Just a moment..." not in resp.text:
            html = resp.text
            print(f"    [+] Successfully fetched page directly ({len(html)} bytes)")
    except Exception as e:
        print(f"    [-] Direct request failed: {e}")

    # Try Proxy fallback (completely thread-safe, strict timeouts, no hanging)
    if not html:
        print("    [!] Direct request failed or blocked by Cloudflare. Fetching proxy list...")
        try:
            proxies = get_proxy_list()
            
            import random
            random.shuffle(proxies)
            candidate_proxies = proxies[:50]  # Try up to 50 proxies in parallel
            print(f"    [*] Found {len(proxies)} proxies. Testing {len(candidate_proxies)} in parallel...")
            
            def test_proxy(p):
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
                        timeout=(2.05, 4)  # 2s connect, 4s read timeout per proxy
                    )
                    if resp.status_code == 200 and "Just a moment..." not in resp.text:
                        return p, resp.text
                except Exception:
                    pass
                return None, None

            # Check concurrently and pick the first success
            with ThreadPoolExecutor(max_workers=25) as executor:
                futures = {executor.submit(test_proxy, p): p for p in candidate_proxies}
                for future in as_completed(futures):
                    p, text = future.result()
                    if text:
                        html = text
                        global WORKING_PROXY
                        WORKING_PROXY = p
                        print(f"    [+] Successfully resolved page using proxy {p} ({len(html)} bytes)")
                        # Try to cancel remaining futures to free resources
                        for f in futures:
                            f.cancel()
                        break
        except Exception as e:
            print(f"    [-] Proxy fallback failed: {e}")

    if not html:
        raise ValueError(f"Failed to fetch content from {page_url} (Direct & Proxy attempts failed or blocked)")

    # Find ALL links on the page
    pattern = r'<a\s+[^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>'
    matches = re.findall(pattern, html, re.IGNORECASE | re.DOTALL)

    links = []
    seen_urls = set()

    for url, label in matches:
        url = url.strip()
        if not url or url.startswith('#') or url.startswith('javascript:'):
            continue

        # Convert relative links to absolute
        if url.startswith('/'):
            url = urljoin(page_url, url)

        # Clean up the label (strip HTML tags and emoji)
        name = re.sub(r'<[^>]+>', '', label).strip()
        # Remove leading/trailing emoji
        name = re.sub(r'^[\U0001f300-\U0001fAFF\u2600-\u27BF\u2700-\u27BF✅🚀⚡⬇️📌\s]+', '', name).strip()
        name = name or f"Link {len(links) + 1}"

        # Skip junk links
        if JUNK_LABELS.search(name):
            continue

        # Classify URL type
        is_shortener = 'sid=' in url
        is_redirect  = any(p in url for p in ['/getlink/', '/get-link/', '/lnk/', '/go/', '/redirect/'])
        is_direct    = any(d in url for d in ['gdrivepro.xyz', 'driveseed.org', 'urlflix.xyz'])

        if not (is_shortener or is_redirect or is_direct):
            continue

        # Skip duplicate URLs
        if url in seen_urls:
            continue
        seen_urls.add(url)

        # Only keep links with good labels
        if not GOOD_LABELS.search(name):
            continue

        # type: 'shortener' needs full bypass, 'redirect' needs a HEAD follow,
        #        'direct' is already a final URL
        if is_shortener:
            link_type = 'shortener'
        elif is_redirect:
            link_type = 'redirect'
        else:
            link_type = 'direct'

        links.append({
            "name": name,
            "url": url,
            "type": link_type,
        })

    return links


def find_working_proxy_for_url(url: str, candidates: list[str]) -> str:
    """Test candidate proxies in parallel and return the first one that successfully reaches the URL."""
    def test_one(p):
        try:
            p_dict = {'http': f'http://{p}', 'https': f'http://{p}'}
            resp = requests.get(
                url,
                headers={
                    'User-Agent': (
                        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                        'AppleWebKit/537.36 (KHTML, like Gecko) '
                        'Chrome/131.0.0.0 Safari/537.36'
                    )
                },
                proxies=p_dict,
                stream=True,
                allow_redirects=False,
                timeout=(3.05, 5),
            )
            if resp.status_code in (200, 301, 302, 307):
                return p
        except Exception:
            pass
        return None

    with ThreadPoolExecutor(max_workers=25) as executor:
        futures = {executor.submit(test_one, p): p for p in candidates[:50]}
        for future in as_completed(futures):
            p = future.result()
            if p:
                for f in futures:
                    f.cancel()
                return p
    return None

def follow_redirect(url: str) -> str:
    """Follow a 302 redirect and return the final destination URL, resolving any Javascript redirects."""
    global WORKING_PROXY
    
    # Try WORKING_PROXY first if set
    if WORKING_PROXY:
        try:
            resp = requests.get(
                url,
                allow_redirects=True,
                headers={
                    'User-Agent': (
                        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                        'AppleWebKit/537.36 (KHTML, like Gecko) '
                        'Chrome/131.0.0.0 Safari/537.36'
                    )
                },
                proxies={'http': f'http://{WORKING_PROXY}', 'https': f'http://{WORKING_PROXY}'},
                stream=True,
                timeout=(3.05, 10),
            )
            final_url = resp.url
            html = ""
            for chunk in resp.iter_content(chunk_size=4096, decode_unicode=True):
                if chunk:
                    html += chunk
                if len(html) >= 4096:
                    break
            js_match = re.search(
                r'(?:window\.)?location(?:\.replace|\.href)?\s*[\(=]\s*["\']([^"\']+)["\']',
                html,
                re.IGNORECASE
            )
            if js_match:
                relative_target = js_match.group(1)
                final_url = urljoin(final_url, relative_target)
                print(f"    [+] Resolved JS redirect using active proxy {WORKING_PROXY} to: {final_url}")
            else:
                print(f"    [+] Resolved redirect using active proxy {WORKING_PROXY} to: {final_url}")
            return final_url
        except Exception as e:
            print(f"    [-] follow_redirect with existing WORKING_PROXY {WORKING_PROXY} failed: {e}")
            WORKING_PROXY = None # reset since it failed
            
    # Find a new proxy to follow the redirect
    print("[*] Finding a working proxy to follow redirect...")
    fetched = get_proxy_list()
    p = find_working_proxy_for_url(url, fetched)
    
    if p:
        print(f"    [+] Found working proxy for redirect: {p}")
        WORKING_PROXY = p
        proxies = {'http': f'http://{p}', 'https': f'http://{p}'}
    else:
        print("    [-] Could not find any working proxy. Trying direct connection as fallback...")
        proxies = None

    try:
        resp = requests.get(
            url,
            allow_redirects=True,
            headers={
                'User-Agent': (
                    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                    'AppleWebKit/537.36 (KHTML, like Gecko) '
                    'Chrome/131.0.0.0 Safari/537.36'
                )
            },
            proxies=proxies,
            stream=True,
            timeout=(3.05, 10),
        )
        final_url = resp.url
        html = ""
        for chunk in resp.iter_content(chunk_size=4096, decode_unicode=True):
            if chunk:
                html += chunk
            if len(html) >= 4096:
                break
        js_match = re.search(
            r'(?:window\.)?location(?:\.replace|\.href)?\s*[\(=]\s*["\']([^"\']+)["\']',
            html,
            re.IGNORECASE
        )
        if js_match:
            relative_target = js_match.group(1)
            final_url = urljoin(final_url, relative_target)
            print(f"    [+] Resolved JS redirect to: {final_url}")
        else:
            print(f"    [+] Resolved redirect to: {final_url}")
        return final_url
    except Exception as e:
        print(f"    [-] follow_redirect failed: {e}")
        
    return url


def resolve_link(index, link, session=None):
    """Resolve a single link based on its type."""
    try:
        lt = link["type"]
        if lt == 'shortener':
            url = bypass_shortener(link["url"], verbose=False, session=session)
        elif lt == 'redirect':
            url = follow_redirect(link["url"])
        else:
            url = link["url"]
        return index, link["name"], url
    except Exception as e:
        return index, link["name"], None


def main():
    page_url = sys.argv[1] if len(sys.argv) > 1 else "https://episodes.modpro.blog/archives/111328"

    print("=" * 60)
    print("  ModPro Link Resolver")
    print("=" * 60)
    print(f"\n  Source: {page_url}\n")

    # Step 1: Scrape links
    print("[*] Scraping links...")
    links = scrape_links(page_url)

    if not links:
        print("ERROR: No download links found on the page!")
        sys.exit(1)

    by_type = {}
    for l in links:
        by_type[l['type']] = by_type.get(l['type'], 0) + 1

    print(f"    Found {len(links)} link(s):", end="")
    for t, c in by_type.items():
        print(f" {c} {t}", end="")
    print("\n")

    type_icons = {'shortener': '🔗', 'redirect': '🔀', 'direct': '📎'}
    for i, link in enumerate(links, 1):
        tag = type_icons.get(link['type'], '🔗')
        print(f"    {i}. {tag} {link['name']}")

    # Step 2: Resolve links in parallel
    print(f"\n{'─' * 60}")
    print(f"  Resolving {len(links)} links in parallel...")
    print(f"{'─' * 60}\n")

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
            print(f"  {status} [{idx + 1}/{len(links)}] {name}")

    # Step 3: Print summary
    print(f"\n{'=' * 60}")
    print("  RESULTS")
    print(f"{'=' * 60}\n")

    success = [r for r in resolved if r["url"]]
    for r in resolved:
        status = r["url"] or "FAILED"
        print(f"  {r['name']}: {status}")

    print(f"\n  Resolved: {len(success)}/{len(resolved)}")

    if not success:
        print("\nNo links resolved. Nothing to open.")
        sys.exit(1)

    # Step 4: Open in browser
    print(f"\n{'─' * 60}")
    print(f"  Opening {len(success)} link(s) in browser...")
    print(f"{'─' * 60}\n")

    for r in success:
        print(f"  Opening: {r['name']}")
        webbrowser.open_new_tab(r["url"])

    print("\n  Done! All links opened in browser tabs.")


if __name__ == "__main__":
    main()
