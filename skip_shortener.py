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
from urllib.parse import urlparse
import requests
from html.parser import HTMLParser


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
    """
    # Pattern: s_343('pepe-XXXX', 'long_value', 60)
    pattern = r"s_\d+\(\s*['\"]([^'\"]+)['\"]\s*,\s*['\"]([^'\"]+)['\"]\s*,\s*(\d+)\s*\)"
    match = re.search(pattern, html)
    if match:
        return match.group(1), match.group(2)

    # Alternative: direct cookie set
    pattern2 = r"document\.cookie\s*=\s*['\"]([^=]+)=([^;]+);"
    match2 = re.search(pattern2, html)
    if match2:
        return match2.group(1), match2.group(2)

    # Fallback: look for pepe-XXXX pattern and nearby value
    pepe_match = re.search(r"['\"]?(pepe-[a-f0-9]+)['\"]?", html)
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

    resp = session.post(
        post_url,
        data=post_data,
        headers={
            'Referer': referer,
            'Content-Type': 'application/x-www-form-urlencoded',
            'Origin': f'https://{shortener_domain}',
        },
        allow_redirects=True,
    )
    resp.raise_for_status()

    if verbose:
        print(f"    Status: {resp.status_code}, URL: {resp.url}")
        print(f"    Page length: {len(resp.text)} chars")

    return resp, resp.text


def bypass_shortener(url: str, verbose: bool = True, session: requests.Session = None) -> str:
    """
    Bypass the URL shortener and return the final URL.
    Accepts an optional pre-configured session for connection reuse.
    """
    if session is None:
        session = requests.Session()
        session.headers.update({
            'User-Agent': (
                'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                'AppleWebKit/537.36 (KHTML, like Gecko) '
                'Chrome/131.0.0.0 Safari/537.36'
            ),
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.5',
        })

    # Get shortener domain dynamically from the input URL
    try:
        parsed_url = urlparse(url)
        initial_domain = parsed_url.netloc or 'health.jkssbworld.in'
    except Exception:
        initial_domain = 'health.jkssbworld.in'

    # ── Step 1: GET the ?sid= landing page ──
    if verbose:
        print("[1] Fetching landing page...")
    resp = session.get(url, allow_redirects=True)
    resp.raise_for_status()
    if verbose:
        print(f"    Status: {resp.status_code}, URL: {resp.url}")

    current_html = resp.text
    current_url = resp.url

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
        print("\nERROR: Could not extract cookie from page!")
        # Debug output
        s_calls = re.findall(r"s_\d+\([^)]+\)", current_html)
        if s_calls:
            print(f"  Found s_ calls: {s_calls[:3]}")
        pepe_matches = re.findall(r"pepe-[a-f0-9]+", current_html)
        if pepe_matches:
            print(f"  Found pepe patterns: {pepe_matches}")

        with open("/tmp/shortener_debug.html", "w") as f:
            f.write(current_html)
        print("  Full page saved to /tmp/shortener_debug.html")
        sys.exit(1)

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

    resp_final = session.get(
        go_url,
        headers={'Referer': current_url},
        allow_redirects=True,
    )
    resp_final.raise_for_status()

    final_url = resp_final.url

    # If we're still on the shortener domain, try to find the real destination
    final_domain_lower = urlparse(final_url).netloc.lower()
    if shortener_domain.lower() in final_domain_lower or 'unblockedgames' in final_domain_lower:
        if verbose:
            print("    Still on shortener domain, looking for destination...")

        for pattern in [
            r'href=["\']([^"\']*driveseed[^"\']*)["\']',
            r'href=["\']([^"\']*drive\.[^"\']*)["\']',
            r'window\.location(?:\.href)?\s*=\s*["\']([^"\']+)["\']',
            r'http-equiv=["\']refresh["\'][^>]*url=([^"\'>\s]+)',
            r'<a[^>]+href=["\']([^"\']+)["\'][^>]*>.*?(?:download|destination)',
        ]:
            m = re.search(pattern, resp_final.text, re.IGNORECASE)
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
