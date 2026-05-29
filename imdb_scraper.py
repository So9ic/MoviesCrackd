import urllib.parse
import requests
from requests.adapters import HTTPAdapter
import sys

# Shared high-performance session for IMDb ID resolution with connection pooling
IMDB_SESSION = requests.Session()
IMDB_SESSION.mount('https://', HTTPAdapter(pool_connections=5, pool_maxsize=10))

def get_imdb_id(query: str) -> str:
    """
    Given a movie or TV show title, returns its IMDb ID (e.g. 'tt1190634').
    Extremely fast, minimal, and doesn't require keys or heavy requests.
    """
    query = query.strip()
    if not query:
        return None
    
    # First character for the subfolder in the CDN endpoint
    first_char = query[0].lower()
    
    # URL encode the search term
    encoded_query = urllib.parse.quote(query.lower())
    
    # Fetch from IMDb's lightning-fast, public suggestion CDN using the universal /x/ endpoint
    url = f"https://v3.sg.media-imdb.com/suggestion/x/{encoded_query}.json?includeVideos=1"
    
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "application/json"
    }
    
    try:
        response = IMDB_SESSION.get(url, headers=headers, timeout=5)
        if response.status_code == 200:
            data = response.json()
            results = data.get("d", [])
            
            # Filter results for titles (movie or series) starting with "tt"
            for item in results:
                title_id = item.get("id", "")
                if title_id.startswith("tt"):
                    return title_id
    except Exception as e:
        print(f"Error connecting to IMDb API: {e}", file=sys.stderr)
        
    return None

if __name__ == "__main__":
    test_queries = ["The Boys", "House md", "13 Reasons Why", "Inception"]
    print("--- Running IMDb Scraper Tests ---")
    for q in test_queries:
        imdb_id = get_imdb_id(q)
        print(f"Query: '{q}' -> IMDb ID: {imdb_id}")
