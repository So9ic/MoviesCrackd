#!/usr/bin/env python3
"""
Bypass script for tech.unblockedgames.world URL shortener.

Flow:
1. GET the ?sid= URL → landing page with auto-submit POST form (_wp_http → root)
2. POST to root → second landing page with form (_wp_http2 + token → article URL)
3. POST to article URL → article page with s_XXX() JS that sets pepe-* cookie
4. Extract cookie name + value, set it, follow ?go=pepe-XXX → final destination
"""

import re
import sys
import threading
import time
from collections import OrderedDict
from urllib.parse import urlparse, parse_qs
import requests
from requests.adapters import HTTPAdapter
from html.parser import HTMLParser


# ── Pre-compiled regex patterns (avoid recompilation per call) ──────────
_RE_COOKIE_SETTER = re.compile(
    r"s_\d+\(\s*['\"]([^'\"]+)['\"]\s*,\s*['\"]([^'\"]+)['\"]\s*,\s*(\d+)\s*\)"
)
_RE_COOKIE_DIRECT = re.compile(
    r"document\.cookie\s*=\s*['\"]([^=]+)=([^;]+);"
)
_RE_PEPE_NAME = re.compile(r"['\"]?(pepe-[a-f0-9]+)['\"]?")
_RE_DEST_PATTERNS = [
    re.compile(r'href=["\']([^"\']*driveseed[^"\']*)["\']', re.IGNORECASE),
    re.compile(r'href=["\']([^"\']*drive\.[^"\']*)["\']', re.IGNORECASE),
    re.compile(r'window\.location(?:\.href)?\s*=\s*["\']([^"\']+)["\']', re.IGNORECASE),
    re.compile(r'http-equiv=["\']refresh["\'][^>]*url=([^"\'>\s]+)', re.IGNORECASE),
    re.compile(r'<a[^>]+href=["\']([^"\']+)["\'][^>]*>.*?(?:download|destination)', re.IGNORECASE),
]


# ── SID → Final URL result cache (TTL-based LRU) ───────────────────────
_BYPASS_CACHE = OrderedDict()  # key: sid_value → (final_url, timestamp)
_BYPASS_CACHE_LOCK = threading.Lock()
_BYPASS_CACHE_MAX = 500
_BYPASS_CACHE_TTL = 1800  # 30 minutes


def _cache_get(sid: str) -> str | None:
    """Return cached final URL for this SID if still fresh, else None."""
    with _BYPASS_CACHE_LOCK:
        entry = _BYPASS_CACHE.get(sid)
        if entry is None:
            return None
        final_url, ts = entry
        if time.time() - ts > _BYPASS_CACHE_TTL:
            _BYPASS_CACHE.pop(sid, None)
            return None
        # Move to end (LRU refresh)
        _BYPASS_CACHE.move_to_end(sid)
        return final_url


def _cache_set(sid: str, final_url: str) -> None:
    """Store a resolved SID → final URL with current timestamp."""
    with _BYPASS_CACHE_LOCK:
        _BYPASS_CACHE[sid] = (final_url, time.time())
        _BYPASS_CACHE.move_to_end(sid)
        while len(_BYPASS_CACHE) > _BYPASS_CACHE_MAX:
            _BYPASS_CACHE.popitem(last=False)


def _extract_sid(url: str) -> str | None:
    """Extract the ?sid= parameter from a shortener URL."""
    try:
        qs = parse_qs(urlparse(url).query)
        sids = qs.get('sid')
        return sids[0] if sids else None
    except Exception:
        return None


# ── Module-level connection-pooled session ──────────────────────────────
_POOL_SESSION = requests.Session()
_POOL_SESSION.headers.update({
    'User-Agent': (
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
        'AppleWebKit/537.36 (KHTML, like Gecko) '
        'Chrome/131.0.0.0 Safari/537.36'
    ),
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Language': 'en-US,en;q=0.5',
})
_pool_adapter = HTTPAdapter(pool_connections=10, pool_maxsize=10)
_POOL_SESSION.mount('https://', _pool_adapter)
_POOL_SESSION.mount('http://', _pool_adapter)


class FormExtractor(HTMLParser):
    """Extract form action, method, and hidden input fields from HTML."""
    def __init__(self):
        super().__init__()
        self.forms = []
        self._current_form = None

    def handle_starttag(self, tag, attrs):
        attrs_dict = dict(attrs)
        if tag == 'form':
            self._current_form = {
                'action': attrs_dict.get('action', ''),
                'method': attrs_dict.get('method', 'GET').upper(),
                'fields': {}
            }
        elif tag == 'input' and self._current_form is not None:
            name = attrs_dict.get('name', '')
            value = attrs_dict.get('value', '')
            if name:
                self._current_form['fields'][name] = value

    def handle_endtag(self, tag):
        if tag == 'form' and self._current_form is not None:
            self.forms.append(self._current_form)
            self._current_form = None


def extract_forms(html: str) -> list:
    """Parse HTML and return list of form dicts."""
    parser = FormExtractor()
    parser.feed(html)
    return parser.forms


def extract_cookie_call(html: str) -> tuple:
    """
    Extract the s_XXX('cookie_name', 'cookie_value', expiry) call from JS.
    Returns (cookie_name, cookie_value) or (None, None).
    Uses pre-compiled regex patterns for speed.
    """
    match = _RE_COOKIE_SETTER.search(html)
    if match:
        return match.group(1), match.group(2)

    match2 = _RE_COOKIE_DIRECT.search(html)
    if match2:
        return match2.group(1), match2.group(2)

    # Fallback: look for pepe-XXXX pattern and nearby value
    pepe_match = _RE_PEPE_NAME.search(html)
    if pepe_match:
        cookie_name = pepe_match.group(1)
        escaped = re.escape(cookie_name)
        val_pattern = escaped + r"['\"],\s*['\"]([^'\"]+)['\"]"
        val_match = re.search(val_pattern, html)
        if val_match:
            return cookie_name, val_match.group(1)

    return None, None


def do_post_step(session, html, step_num, referer, verbose=True, default_domain='tech.unblockedgames.world'):
    """Submit a POST form found in the HTML, return the response."""
    forms = extract_forms(html)
    if not forms:
        return None, html

    form = forms[0]
    post_url = form['action']
    post_data = form['fields']

    if verbose:
        print(f"\n[{step_num}] Found form → POST to: {post_url}")
        print(f"    Fields: {list(post_data.keys())}")

    try:
        shortener_domain = urlparse(referer).netloc or default_domain
    except Exception:
        shortener_domain = default_domain

    resp = None
    for attempt in range(3):
        try:
            resp = session.post(
                post_url,
                data=post_data,
                headers={
                    'Referer': referer,
                    'Content-Type': 'application/x-www-form-urlencoded',
                    'Origin': f'https://{shortener_domain}',
                },
                allow_redirects=True,
                timeout=10
            )
            if resp.status_code in (500, 502, 503, 504):
                resp.raise_for_status()
            break
        except Exception as e:
            if verbose:
                print(f"    [-] POST attempt {attempt+1} failed: {e}")
            if attempt == 2:
                raise e
            time.sleep(0.5 * (attempt + 1))

    if verbose:
        print(f"    Status: {resp.status_code}, URL: {resp.url}")
        print(f"    Page length: {len(resp.text)} chars")

    return resp, resp.text


# Cache the last known working shortener domain globally
LAST_WORKING_DOMAIN = 'health.jkssbworld.in'
BYPASS_SEM = threading.Semaphore(3)


def bypass_shortener(url: str, verbose: bool = True, session: requests.Session = None) -> str:
    """
    Bypass the URL shortener and return the final URL.
    Allows parallel execution up to 3 slots with isolated cookie jars.
    Uses SID-keyed cache — repeated calls for the same SID return instantly.
    """
    global LAST_WORKING_DOMAIN, BYPASS_SEM

    # ── Cache lookup: return instantly if we've resolved this SID before ──
    sid = _extract_sid(url)
    if sid:
        cached = _cache_get(sid)
        if cached:
            if verbose:
                print(f"[*] Cache HIT for SID {sid[:20]}… → {cached}")
            return cached

    with BYPASS_SEM:
        # Re-check cache inside the semaphore (another thread may have resolved it while we waited)
        if sid:
            cached = _cache_get(sid)
            if cached:
                if verbose:
                    print(f"[*] Cache HIT (post-sem) for SID {sid[:20]}… → {cached}")
                return cached

        # Create an isolated session that shares the module-level connection pool
        # but has its own cookie jar (critical for thread safety)
        iso_session = requests.Session()
        # Share the pooled adapters for TCP/TLS connection reuse
        for prefix, adapter in _POOL_SESSION.adapters.items():
            iso_session.mount(prefix, adapter)
        iso_session.headers.update(_POOL_SESSION.headers)
        # Copy caller's proxies if provided
        if session is not None:
            iso_session.proxies.update(session.proxies)
        session = iso_session

        target_url = url
        try:
            parsed_url = urlparse(target_url)
            initial_domain = parsed_url.netloc or LAST_WORKING_DOMAIN
        except Exception:
            initial_domain = LAST_WORKING_DOMAIN

        # Proactive self-healing domain swap if we know the domain is down/failing
        if initial_domain and LAST_WORKING_DOMAIN and initial_domain != LAST_WORKING_DOMAIN:
            if 'unblockedgames' in initial_domain:
                if verbose:
                    print(f"[*] Proactive domain swap: replacing {initial_domain} with last working domain {LAST_WORKING_DOMAIN}")
                target_url = target_url.replace(initial_domain, LAST_WORKING_DOMAIN)
                initial_domain = LAST_WORKING_DOMAIN

        # We run the bypass in a loop (up to 2 iterations for domain swapping fallback)
        for run_attempt in range(2):
            try:
                # ── Step 1: GET the ?sid= landing page ──
                if verbose:
                    print(f"[1] Fetching landing page ({initial_domain})...")
                
                resp = None
                for attempt in range(3):
                    try:
                        resp = session.get(target_url, allow_redirects=True, timeout=10)
                        if resp.status_code in (500, 502, 503, 504):
                            resp.raise_for_status()
                        break
                    except Exception as e:
                        if verbose:
                            print(f"    [-] GET attempt {attempt+1} failed: {e}")
                        if attempt == 2:
                            raise e
                        time.sleep(0.5 * (attempt + 1))
                
                if verbose:
                    print(f"    Status: {resp.status_code}, URL: {resp.url}")

                current_html = resp.text
                current_url = resp.url

                # Save the successful domain to our global cache
                try:
                    last_netloc = urlparse(current_url).netloc
                    if last_netloc:
                        LAST_WORKING_DOMAIN = last_netloc
                except Exception:
                    pass

                # ── Step 2+: Keep POSTing forms until we find the cookie ──
                step = 2
                max_steps = 5  # safety limit

                while step <= max_steps:
                    cookie_name, cookie_value = extract_cookie_call(current_html)
                    if cookie_name and cookie_value:
                        break

                    # Check if there's another form to submit
                    forms = extract_forms(current_html)
                    if not forms:
                        if verbose:
                            print(f"\n[{step}] No more forms and no cookie found!")
                        break

                    resp, current_html = do_post_step(
                        session, current_html, step, current_url, verbose, default_domain=initial_domain
                    )
                    if resp is None:
                        break
                    current_url = resp.url
                    step += 1

                if not cookie_name or not cookie_value:
                    raise ValueError("Could not extract cookie from page")

                if verbose:
                    print(f"\n[{step}] Extracted cookie:")
                    print(f"    Name:  {cookie_name}")
                    print(f"    Value: {cookie_value[:80]}...")

                try:
                    shortener_domain = urlparse(current_url).netloc or initial_domain
                except Exception:
                    shortener_domain = initial_domain

                # ── Set the cookie ──
                session.cookies.set(
                    cookie_name,
                    cookie_value,
                    domain=shortener_domain,
                    path='/',
                )
                if verbose:
                    print(f"\n[{step + 1}] Cookie set in session")

                # ── Follow the ?go= redirect ──
                go_url = f"https://{shortener_domain}/?go={cookie_name}"
                if verbose:
                    print(f"\n[{step + 2}] Following redirect: {go_url}")

                resp_final = None
                for attempt in range(3):
                    try:
                        resp_final = session.get(
                            go_url,
                            headers={'Referer': current_url},
                            allow_redirects=True,
                            timeout=10
                        )
                        if resp_final.status_code in (500, 502, 503, 504):
                            resp_final.raise_for_status()
                        break
                    except Exception as e:
                        if verbose:
                            print(f"    [-] GET ?go= attempt {attempt+1} failed: {e}")
                        if attempt == 2:
                            raise e
                        time.sleep(0.5 * (attempt + 1))

                final_url = resp_final.url

                # If we're still on the shortener domain, try to find the real destination
                final_domain_lower = urlparse(final_url).netloc.lower()
                if shortener_domain.lower() in final_domain_lower or 'unblockedgames' in final_domain_lower:
                    if verbose:
                        print("    Still on shortener domain, looking for destination...")

                    for pat in _RE_DEST_PATTERNS:
                        m = pat.search(resp_final.text)
                        if m:
                            candidate = m.group(1)
                            candidate_domain = urlparse(candidate).netloc.lower()
                            if shortener_domain.lower() not in candidate_domain and 'unblockedgames' not in candidate_domain:
                                final_url = candidate
                                break

                    final_domain_lower = urlparse(final_url).netloc.lower()
                    if shortener_domain.lower() in final_domain_lower or 'unblockedgames' in final_domain_lower:
                        with open("/tmp/shortener_final_debug.html", "w") as f:
                            f.write(resp_final.text)
                        if verbose:
                            print("    Could not resolve final URL. Page saved to /tmp/shortener_final_debug.html")

                if verbose:
                    print(f"\n{'=' * 60}")
                    print(f"  FINAL URL: {final_url}")
                    print(f"{'=' * 60}")

                # Cache the result for instant future lookups
                if sid:
                    _cache_set(sid, final_url)
                return final_url

            except Exception as e:
                # If the run failed, and we haven't swapped to the last working domain, do it now and try again!
                if run_attempt == 0 and LAST_WORKING_DOMAIN and initial_domain != LAST_WORKING_DOMAIN:
                    if verbose:
                        print(f"[-] Resolution failed on domain {initial_domain}: {e}. Swapping to fallback {LAST_WORKING_DOMAIN}...")
                    target_url = target_url.replace(initial_domain, LAST_WORKING_DOMAIN)
                    initial_domain = LAST_WORKING_DOMAIN
                    # Reset cookies in our session
                    session.cookies.clear()
                    continue
                else:
                    raise e
        return final_url


if __name__ == '__main__':
    if len(sys.argv) < 2:
        test_url = (
            "https://health.jkssbworld.in/?sid="
            "a3Y4azk3STZ5RVphb1c0d0pkeDllbjluV0NSTDRXNWlOSmJZTDFBU1RwM3AwTEJSbHhsejZL"
            "cmNYQzFsVGV2QkxMUmpsdURZR3hQNEo5c2g2UHhoMWRBNmt2dWQzZWx3ZjU1dkhTT3FySFRy"
            "M3ZvbjdDaGRiL3dmZUZVR2FCY1JjZ0FEdjI3SnhnSGZYWDhHQ1NQU1lTZXE0TTluakt0SUE0"
            "dTI1aVlzMjNHOFZvR1BrajV1RzVQcUZKc09ZUXlDbWE3RzZkdWp1aDJzVUtvd2ROSWtoMzRh"
            "TFE0T0NleS9zaDJHTStTWHpwNTMwOC9tbCtxMkJ1V1VnU3lQc3R5bw=="
        )
    else:
        test_url = sys.argv[1]

    print("=" * 60)
    print("  URL Shortener Bypass")
    print("=" * 60)
    print()

    bypass_shortener(test_url)
