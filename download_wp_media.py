import os
import re
import sys
import requests
from datetime import datetime
from urllib.parse import urlparse, urljoin
from bs4 import BeautifulSoup

# ----------------------------
# USAGE CHECK
# ----------------------------
if len(sys.argv) < 2:
    print("Usage: python download_wp_media.py <WORDPRESS_SITE_URL>")
    sys.exit(1)

SITE_URL = sys.argv[1].rstrip("/")
parsed_url = urlparse(SITE_URL)
DOMAIN = parsed_url.netloc or re.sub(r"^https?://", "", SITE_URL)

# REST API endpoints
MEDIA_API = urljoin(SITE_URL, "/wp-json/wp/v2/media")
POSTS_API = urljoin(SITE_URL, "/wp-json/wp/v2/posts")

# We'll store all downloaded files inside a folder named after the domain
BASE_FOLDER = f"downloaded_{DOMAIN}"
INITIAL_PER_PAGE = 100
MIN_PER_PAGE = 1

# ----------------------------
# HELPER FUNCTIONS
# ----------------------------
def ensure_directory_exists(path):
    """Create target download directory if not present."""
    if not os.path.exists(path):
        os.makedirs(path, exist_ok=True)

def clean_filename(filename):
    """Remove invalid characters for local filenames."""
    return re.sub(r'[\\/*?:"<>|]', '_', filename)

def get_date_subfolder(date_str):
    """
    Extract date (YYYY-MM-DD) from something like '2024-01-15T10:12:30'.
    If invalid, return 'unknown-date'.
    """
    try:
        dt = datetime.fromisoformat(date_str.replace("Z", ""))  # handle partial ISO strings
        return dt.strftime("%Y-%m-%d")
    except ValueError:
        return "unknown-date"

def download_file(url, date_str):
    """
    Download a file (any media type) from a URL into a date-based subfolder,
    skipping if file already exists.
    """
    subfolder_name = get_date_subfolder(date_str)
    target_folder = os.path.join(BASE_FOLDER, subfolder_name)
    ensure_directory_exists(target_folder)

    try:
        resp = requests.get(url, stream=True, timeout=15)
        if resp.status_code == 200:
            parsed_url = urlparse(url)
            filename = os.path.basename(parsed_url.path) or ""
            if not filename:
                # Fallback if no filename in path
                filename = re.sub(r"\W+", "_", url)

            filename = clean_filename(filename)
            filepath = os.path.join(target_folder, filename)

            if os.path.exists(filepath):
                print(f"[SKIP] File already exists: {filepath}")
                return

            with open(filepath, "wb") as f:
                for chunk in resp.iter_content(chunk_size=8192):
                    f.write(chunk)

            print(f"[OK] Downloaded: {filepath}")
        else:
            print(f"[WARN] Status {resp.status_code} for URL: {url}")
    except Exception as e:
        print(f"[ERROR] Failed to download {url}: {e}")

# ----------------------------
# 1) FETCH MEDIA VIA MEDIA ENDPOINT
# ----------------------------
def fetch_all_media_items():
    """
    Paginate through /wp-json/wp/v2/media, collecting all items.
    Dynamically adjust 'per_page' if we get a 400 error.
    Stop early if the next page yields no new items (duplicate IDs).
    """
    all_items = []
    all_seen_ids = set()

    page_num = 1
    per_page = INITIAL_PER_PAGE

    while True:
        print(f"[MEDIA] Requesting page {page_num} (per_page={per_page})...")
        try:
            r = requests.get(
                MEDIA_API,
                params={"page": page_num, "per_page": per_page},
                timeout=10
            )
        except Exception as ex:
            print(f"[ERROR] Exception requesting media page {page_num}: {ex}")
            break

        if r.status_code == 400:
            # "Invalid page number" or "Invalid per_page" in WP
            print(f"[WARN] 400 error on page={page_num}, per_page={per_page}. Reducing per_page.")
            if per_page > MIN_PER_PAGE:
                per_page = max(MIN_PER_PAGE, per_page // 2)
                continue
            else:
                print("[WARN] Already at per_page=1 and still failing. Stopping.")
                break

        elif r.status_code == 200:
            data = r.json()
            if not data:
                # No more media items
                break

            new_page_ids = []
            for item in data:
                media_id = item.get("id")
                if media_id not in all_seen_ids:
                    all_items.append(item)
                    all_seen_ids.add(media_id)
                    new_page_ids.append(media_id)

            if not new_page_ids:
                print("[INFO] Next media page returned duplicates. Stopping early.")
                break

            page_num += 1

        else:
            print(f"[WARN] Received status {r.status_code} for media request. Stopping.")
            break

    return all_items

# ----------------------------
# 2) FALLBACK: FETCH POSTS & PARSE IMAGES
# ----------------------------
def fetch_all_posts():
    """
    Paginate through /wp-json/wp/v2/posts, returning a list of all posts.
    We do a simpler pagination approach here. If large, consider the same
    'per_page' logic as above. By default, WP may limit to 10 or so, but
    we can attempt to increase that with 'per_page'.
    """
    all_posts = []
    page_num = 1
    per_page = 20  # Tweak as needed
    while True:
        print(f"[POSTS] Requesting page {page_num} (per_page={per_page})...")
        try:
            resp = requests.get(
                POSTS_API,
                params={"page": page_num, "per_page": per_page},
                timeout=10
            )
        except Exception as ex:
            print(f"[ERROR] Exception requesting posts page {page_num}: {ex}")
            break

        if resp.status_code == 400:
            # Possibly invalid page or per_page
            print(f"[WARN] 400 error on posts page={page_num}. We’ll just stop.")
            break
        elif resp.status_code == 200:
            data = resp.json()
            if not data:
                break
            all_posts.extend(data)
            page_num += 1
        else:
            print(f"[WARN] Received status {resp.status_code} for posts. Stopping.")
            break

    return all_posts

def parse_images_from_posts(posts):
    """
    Go through each post's content, find <img> tags and their src attributes.
    Return a list of (image_url, post_date).
    """
    image_list = []
    for post in posts:
        date_str = post.get("date") or "unknown-date"  # e.g., '2024-01-15T10:12:30'
        content_html = post.get("content", {}).get("rendered", "")
        # Parse with BeautifulSoup
        soup = BeautifulSoup(content_html, "html.parser")

        # Extract img src
        for img in soup.find_all("img", src=True):
            img_url = img["src"]
            # Absolute or relative? If relative, join with domain
            if img_url.startswith("//"):
                # Scheme-relative
                img_url = "https:" + img_url
            elif img_url.startswith("/"):
                # Relative path
                img_url = urljoin(SITE_URL, img_url)
            # Add to list
            image_list.append((img_url, date_str))

    return image_list

# ----------------------------
# MAIN LOGIC
# ----------------------------
def main():
    ensure_directory_exists(BASE_FOLDER)

    print(f"=== Attempting to fetch media items from '{SITE_URL}' ===")
    media_items = fetch_all_media_items()

    if not media_items:
        print("[INFO] Media endpoint returned zero items. Falling back to /posts scraping.")
        # 1) Fetch posts
        posts = fetch_all_posts()
        # 2) Parse images from each post
        post_images = parse_images_from_posts(posts)
        print(f"[INFO] Found {len(post_images)} images in post content.")
        # 3) Download them
        for img_url, post_date in post_images:
            download_file(img_url, post_date)

    else:
        print(f"[INFO] Found {len(media_items)} media items via media endpoint. Downloading...")
        # Download from the media endpoint
        for item in media_items:
            source_url = item.get("source_url")
            date_str = item.get("date") or ""
            if source_url:
                download_file(source_url, date_str)

if __name__ == "__main__":
    # Make sure we have BeautifulSoup installed
    # pip install beautifulsoup4
    main()
