import argparse
import os
import re
import shutil
import json
from datetime import datetime
from urllib.parse import urljoin, urlparse
import requests
from bs4 import BeautifulSoup
from tqdm import tqdm
from playwright.sync_api import sync_playwright, Error as PlaywrightError

# 1x1 dark gray pixel as a data URI — used as fallback for 403/failed assets
GRAY_PIXEL = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="

# Global progress file — tracks all jobs, lives in the script's working directory
PROGRESS_FILE = os.path.join(os.getcwd(), "progress.json")


# ==============================================================================
# Progress tracking helpers
# ==============================================================================

def load_progress():
    """Load the global progress file, returning an empty dict if missing/corrupt."""
    if os.path.exists(PROGRESS_FILE):
        try:
            with open(PROGRESS_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception as e:
            print(f"⚠️  Could not read progress file: {e}")
    return {}


def save_progress(progress):
    """Write the global progress file atomically."""
    tmp = PROGRESS_FILE + ".tmp"
    try:
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(progress, f, indent=2)
        os.replace(tmp, PROGRESS_FILE)
    except Exception as e:
        print(f"⚠️  Could not save progress: {e}")


def get_thread_entry(progress, base_url):
    """Return the progress entry for this thread, or a fresh one if new."""
    if base_url not in progress:
        progress[base_url] = {
            "url": base_url,
            "out_dir": None,
            "total_pages": 0,
            "completed_pages": [],
            "failed_assets": {},   # { url: page_num }
            "status": "new",       # new | in_progress | complete
            "last_run": None,
        }
    return progress[base_url]


def mark_page_complete(progress, base_url, page_num):
    """Record a page as successfully completed and persist immediately."""
    entry = get_thread_entry(progress, base_url)
    if page_num not in entry["completed_pages"]:
        entry["completed_pages"].append(page_num)
    entry["status"] = "in_progress"
    entry["last_run"] = datetime.now().isoformat(timespec='seconds')
    save_progress(progress)


def mark_asset_failed(progress, base_url, asset_url, page_num):
    """Record a failed asset download against its page number."""
    entry = get_thread_entry(progress, base_url)
    entry["failed_assets"][asset_url] = page_num
    save_progress(progress)


def clear_page_failures(progress, base_url, page_num):
    """Remove all failed asset entries belonging to a specific page.
    Called before re-scraping a page so stale failures don't linger."""
    entry = get_thread_entry(progress, base_url)
    stale = [url for url, pg in entry["failed_assets"].items() if pg == page_num]
    for url in stale:
        del entry["failed_assets"][url]
    if stale:
        save_progress(progress)


def mark_thread_complete(progress, base_url, total_pages):
    """Mark the thread as fully downloaded."""
    entry = get_thread_entry(progress, base_url)
    entry["status"] = "complete"
    entry["total_pages"] = total_pages
    entry["last_run"] = datetime.now().isoformat(timespec='seconds')
    save_progress(progress)


# ==============================================================================
# thread_info.json helpers
# ==============================================================================

def read_thread_info(out_dir):
    """Read thread_info.json from a backup directory. Returns dict or None."""
    path = os.path.join(out_dir, "thread_info.json")
    if os.path.exists(path):
        try:
            with open(path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            pass
    return None


def write_thread_info(out_dir, base_url, friendly_name, version=1, total_pages=0):
    """Write or update thread_info.json in the backup directory."""
    path = os.path.join(out_dir, "thread_info.json")
    existing = read_thread_info(out_dir) or {}
    existing.update({
        "url": base_url,
        "friendly_name": friendly_name,
        "version": version,
        "total_pages": total_pages,
        "last_updated": datetime.now().isoformat(timespec='seconds'),
    })
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(existing, f, indent=2)


def find_existing_backup(output_root, base_url):
    """
    Scan subdirectories of output_root for a thread_info.json matching base_url.
    Returns (directory_path, thread_info_dict) or (None, None) if not found.
    """
    if not os.path.isdir(output_root):
        return None, None
    for name in os.listdir(output_root):
        candidate = os.path.join(output_root, name)
        if not os.path.isdir(candidate):
            continue
        info = read_thread_info(candidate)
        if info and info.get("url") == base_url:
            return candidate, info
    return None, None


def get_backup_version(out_dir):
    """Return the version number of a backup directory (1 or 2). Defaults to 1."""
    info = read_thread_info(out_dir)
    if info:
        return int(info.get("version", 1))
    return 1


def strip_notification_prefix(title):
    """Remove XenForo unread notification indicator e.g. '(3) Thread Title'."""
    return re.sub(r'^\s*\(\d+\)\s*', '', title).strip()


# ==============================================================================
# URL / asset helpers
# ==============================================================================

def normalize_base_url(url):
    url = url.rstrip('/')
    url = re.sub(r'/page-\d+$', '', url)
    return url


def download_asset(full_url, session, assets_dir, url_to_local,
                   desc_prefix="", progress=None, thread_url=None, page_num=None):
    """Download any asset with progress. Falls back to gray placeholder on failure.
    Optionally records failures into progress tracking."""
    if full_url in url_to_local:
        return url_to_local[full_url]
    try:
        parsed = urlparse(full_url)
        fname = os.path.basename(parsed.path) or 'asset.bin'
        fname = re.sub(r'[^a-zA-Z0-9._-]', '_', fname.split('?')[0])
        local_full = os.path.join(assets_dir, fname)
        base, ext = os.path.splitext(local_full)
        counter = 1
        while os.path.exists(local_full):
            local_full = f"{base}_{counter}{ext}"
            counter += 1

        r = session.get(full_url, stream=True, timeout=60)

        if r.status_code == 403:
            print(f"  ⚠ 403 Forbidden (using gray fallback): {full_url[:80]}")
            url_to_local[full_url] = GRAY_PIXEL
            if progress is not None and thread_url and page_num is not None:
                mark_asset_failed(progress, thread_url, full_url, page_num)
            return GRAY_PIXEL

        r.raise_for_status()
        total_size = int(r.headers.get('content-length', 0))

        with open(local_full, 'wb') as f, tqdm(
            total=total_size or None, unit='B', unit_scale=True, unit_divisor=1024,
            desc=(desc_prefix + fname[:40]), leave=False, dynamic_ncols=True
        ) as bar:
            for chunk in r.iter_content(8192):
                if chunk:
                    size = f.write(chunk)
                    bar.update(size)

        rel = os.path.relpath(local_full, os.path.dirname(assets_dir)).replace('\\', '/')
        url_to_local[full_url] = rel
        return rel
    except Exception as e:
        print(f"  ✗ Failed {full_url[:80]}... {e}")
        url_to_local[full_url] = GRAY_PIXEL
        if progress is not None and thread_url and page_num is not None:
            mark_asset_failed(progress, thread_url, full_url, page_num)
        return GRAY_PIXEL


def rewrite_css(css_content, css_base_url, session, assets_dir, url_to_local):
    """Rewrite url(), background, background-image, @import."""
    def replacer(match):
        quote = match.group(1) or ''
        url_str = match.group(2)
        quote2 = match.group(3) or ''
        if not url_str or url_str.startswith(('data:', '#')):
            return match.group(0)
        full = urljoin(css_base_url, url_str)
        local = download_asset(full, session, assets_dir, url_to_local, "CSS: ")
        return f"url({quote}{local}{quote2})"

    css_content = re.sub(
        r'url\s*\(\s*([\'"]?)([^\'")]+?)([\'"]?)\s*\)',
        replacer,
        css_content,
        flags=re.IGNORECASE
    )

    def import_replacer(match):
        quote = match.group(1) or ''
        url_str = match.group(2)
        quote2 = match.group(3) or ''
        full = urljoin(css_base_url, url_str)
        local = download_and_process_css(full, session, assets_dir, url_to_local)
        return f"@import url({quote}{local}{quote2});"

    css_content = re.sub(
        r'@import\s+(?:url\s*\(\s*)?([\'"]?)([^\'";]+?)([\'"]?)\s*\)?\s*;',
        import_replacer,
        css_content,
        flags=re.IGNORECASE
    )
    return css_content


def download_and_process_css(full_url, session, assets_dir, url_to_local):
    if full_url in url_to_local:
        return url_to_local[full_url]
    try:
        r = session.get(full_url, timeout=60)
        r.raise_for_status()
        css_content = r.text
        processed = rewrite_css(css_content, full_url, session, assets_dir, url_to_local)

        parsed = urlparse(full_url)
        fname = os.path.basename(parsed.path) or 'style.css'
        fname = re.sub(r'[^a-zA-Z0-9._-]', '_', fname.split('?')[0])
        if not fname.lower().endswith('.css'):
            fname += '.css'
        local_full = os.path.join(assets_dir, fname)
        base, ext = os.path.splitext(local_full)
        counter = 1
        while os.path.exists(local_full):
            local_full = f"{base}_{counter}{ext}"
            counter += 1

        with open(local_full, 'w', encoding='utf-8') as f:
            f.write(processed)

        rel = os.path.relpath(local_full, os.path.dirname(assets_dir)).replace('\\', '/')
        url_to_local[full_url] = rel
        return rel
    except Exception as e:
        print(f"  ✗ CSS failed {full_url[:80]}... {e}")
        return full_url


def rewrite_inline_styles(soup, base_url, session, assets_dir, url_to_local):
    for tag in soup.find_all(style=True):
        style = tag['style']
        def replacer(match):
            quote = match.group(1) or ''
            url_str = match.group(2)
            quote2 = match.group(3) or ''
            if not url_str or url_str.startswith(('data:', '#')):
                return match.group(0)
            full = urljoin(base_url, url_str)
            local = download_asset(full, session, assets_dir, url_to_local, "Inline: ")
            return f"url({quote}{local}{quote2})"
        new_style = re.sub(
            r'url\s*\(\s*([\'"]?)([^\'")]+?)([\'"]?)\s*\)',
            replacer,
            style,
            flags=re.IGNORECASE
        )
        tag['style'] = new_style


def make_post_images_clickable(soup):
    """Wrap inline post images in <a target="_blank"> so clicking opens full image."""
    selectors = ['img.bbImage', 'img.bbCodeImage']
    candidates = []
    for sel in selectors:
        candidates.extend(soup.select(sel))

    for img in soup.select('.message-cell--main img'):
        if img not in candidates and 'avatar' not in ' '.join(img.get('class', [])):
            candidates.append(img)

    for img in candidates:
        if img.parent and img.parent.name == 'a':
            continue
        src = img.get('src', '')
        if not src or src.startswith('data:'):
            continue
        wrapper = soup.new_tag('a', href=src, target='_blank', rel='noopener noreferrer')
        wrapper['style'] = 'display:inline-block;cursor:zoom-in;'
        img.wrap(wrapper)


def inject_xenforo_fixes(soup):
    """Comprehensive XenForo layout restoration."""
    head = soup.find('head') or soup.new_tag('head')
    if not soup.find('head'):
        soup.insert(0, head)

    if not soup.find('meta', attrs={'name': 'viewport'}):
        viewport = soup.new_tag('meta')
        viewport['name'] = 'viewport'
        viewport['content'] = 'width=device-width, initial-scale=1.0'
        head.append(viewport)

    fix_style = soup.new_tag('style', id='xenforo-offline-fixes')
    fix_style.string = """
/* === XENFORO OFFLINE LOOK RESTORED === */

body, html {
    background: #2e2e2e !important;
    margin: 0;
    padding: 0;
}

.p-pageWrapper {
    max-width: 1280px !important;
    margin: 20px auto !important;
    background: #3a3a3a !important;
    box-shadow: 0 0 15px rgba(0,0,0,0.25) !important;
    border-radius: 6px;
    overflow: hidden;
}

.p-body {
    background: #474747 !important;
    padding: 20px 15px !important;
}
.p-body-inner, .pageContent { max-width: 100% !important; margin: 0 auto !important; }

.message, .message--post, .block--messages .message {
    background: #545454 !important;
    border: 1px solid #404040 !important;
    border-radius: 4px !important;
    margin-bottom: 20px !important;
    box-shadow: 0 1px 3px rgba(0,0,0,0.2) !important;
    color: #e8e8e8 !important;
}
.message .message-inner { display: flex !important; }

.message-cell--user {
    background: #606060 !important;
    border-right: 1px solid #404040 !important;
    padding: 15px 12px !important;
    width: 140px !important;
}

.message-cell--main {
    padding: 15px !important;
    flex: 1 !important;
    background: #4e4e4e !important;
}

.attachment, .attachment-icon, .bbImageWrapper .thumbnail, .thumbnail {
    border: 1px solid #404040 !important;
    background: #5a5a5a !important;
    padding: 6px !important;
    margin: 8px 0 !important;
    max-width: 240px !important;
}
.attachment .thumbnail img, .bbImageWrapper img {
    max-height: 200px !important;
    max-width: 100% !important;
    height: auto !important;
}

img.bbImage, .bbCodeImage, .message img:not(.avatar) {
    max-width: 100% !important;
    width: auto !important;
    height: auto !important;
    border: 1px solid #404040 !important;
    display: block !important;
    margin: 4px 0 !important;
}

.bbCodeSpoiler-content {
    display: block !important;
    visibility: visible !important;
    opacity: 1 !important;
    height: auto !important;
    overflow: visible !important;
}

.p-nav, .p-header { background: #1e1e1e !important; color: #fff !important; }
.block, .block-container { border: 1px solid #404040 !important; }

.message-cell--main, .message-cell--user,
.message-cell--main * { color: #e8e8e8 !important; }
a { color: #7ab3e0 !important; }
a:hover { color: #a8d0f0 !important; }
"""
    head.append(fix_style)


def make_soup(html):
    try:
        return BeautifulSoup(html, 'lxml')
    except Exception:
        print("⚠️  lxml not found — falling back (slower). pip install lxml")
        return BeautifulSoup(html, 'html.parser')


def process_and_save_page(page_num, soup, out_dir, assets_dir, url_to_local,
                          base_url, session, progress=None):
    """Full processing with relative URL support. Passes progress context to
    download_asset so failures are recorded against the correct page."""

    kw = dict(progress=progress, thread_url=base_url, page_num=page_num)

    # Stylesheets
    for link in soup.find_all('link', href=True):
        rel = ' '.join(link.get('rel', [])).lower()
        href = link['href']
        if 'stylesheet' in rel and href and not href.startswith(('data:', '#', 'javascript:', 'tel:', 'mailto:')):
            full = urljoin(base_url, href)
            local = download_and_process_css(full, session, assets_dir, url_to_local)
            link['href'] = local

    for style in soup.find_all('style'):
        if style.string:
            processed = rewrite_css(style.string, base_url, session, assets_dir, url_to_local)
            style.string = processed

    # Images
    for img in soup.find_all('img'):
        src = img.get('data-src') or img.get('src')
        if src and not src.startswith(('data:', '#', 'javascript:')):
            full = urljoin(base_url, src)
            local = download_asset(full, session, assets_dir, url_to_local, **kw)
            img['src'] = local
            if 'data-src' in img.attrs:
                del img['data-src']

        if img.get('srcset'):
            new_set = []
            for part in img['srcset'].split(','):
                s = part.strip().split(None, 1)
                u = s[0]
                if not u.startswith(('data:', '#')):
                    full = urljoin(base_url, u)
                    loc = download_asset(full, session, assets_dir, url_to_local, **kw)
                    new_set.append(loc + (' ' + s[1] if len(s) > 1 else ''))
                else:
                    new_set.append(part)
            img['srcset'] = ', '.join(new_set)

    # Attachments
    for a in soup.find_all('a', href=True):
        href_lower = a['href'].lower()
        if 'attachment' in href_lower or 'attachments/' in href_lower:
            full = urljoin(base_url, a['href'])
            local = download_asset(full, session, assets_dir, url_to_local, **kw)
            a['href'] = local

    # Media embeds
    for a in soup.find_all('a', href=True):
        if 'index.php?media/' in a['href']:
            img = a.find('img')
            if img:
                img_src = img.get('src', '')
                if img_src:
                    full = urljoin(base_url, img_src)
                    local = download_asset(full, session, assets_dir, url_to_local, **kw)
                    a['href'] = local
            else:
                full = urljoin(base_url, a['href'])
                local = download_asset(full, session, assets_dir, url_to_local, **kw)
                a['href'] = local

    # Video/audio
    for tag in soup.find_all(['video', 'audio', 'source']):
        for attr in ['src', 'data-src']:
            val = tag.get(attr)
            if val and not val.startswith(('data:', '#')):
                full = urljoin(base_url, val)
                local = download_asset(full, session, assets_dir, url_to_local, **kw)
                tag[attr] = local
                if attr == 'data-src':
                    del tag[attr]

    # Inline styles
    rewrite_inline_styles(soup, base_url, session, assets_dir, url_to_local)

    # Pagination
    for a in soup.find_all('a', href=True):
        href = a['href']
        m = re.search(r'/page-(\d+)', href)
        if m:
            a['href'] = f"page-{int(m.group(1))}.html"
        elif re.search(r'threads/[^/]+\.\d+/?$', href):
            a['href'] = "page-1.html"

    make_post_images_clickable(soup)
    inject_xenforo_fixes(soup)

    filepath = os.path.join(out_dir, f"page-{page_num}.html")
    with open(filepath, 'w', encoding='utf-8') as f:
        f.write(str(soup))


# ==============================================================================
# Core scrape logic
# ==============================================================================

def _run_scrape(args, progress):
    """Core scrape logic, shared by normal runs, --retryFailed, and --checkUpdates."""
    base_url = normalize_base_url(args.url)
    domain = urlparse(base_url).netloc
    cookies_file = os.path.join(os.getcwd(), f"cookies_{domain.replace('.', '_')}.json")

    print(f"Domain: {domain} | Cookies file: {cookies_file}")

    # Get/create entry for this thread
    entry = get_thread_entry(progress, base_url)

    # Check for an existing backup directory via thread_url.txt
    existing_dir, existing_info = find_existing_backup(args.output, base_url)
    if existing_dir:
        print(f"📁 Found existing backup: {existing_dir}")
        entry["out_dir"] = existing_dir

    saved_cookies = []
    if os.path.exists(cookies_file):
        try:
            with open(cookies_file, 'r', encoding='utf-8') as f:
                saved_cookies = json.load(f)
            print(f"✅ Loaded {len(saved_cookies)} saved cookies.")
        except Exception as e:
            print(f"⚠️ Cookie load error: {e}")

    try:
        with sync_playwright() as p:
            headless = bool(saved_cookies)
            browser = p.chromium.launch(headless=headless, slow_mo=50 if not headless else 0)
            context = browser.new_context(
                viewport={'width': 1280, 'height': 900},
                user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                           '(KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36',
                locale='en-US',
            )
            if saved_cookies:
                context.add_cookies(saved_cookies)

            page = context.new_page()
            print("Loading thread...")
            page.goto(base_url, wait_until='networkidle', timeout=90000)

            html = page.content()
            soup = make_soup(html)
            thread_looks_good = bool(soup.find('title')) and len(str(soup)) > 15000

            if not (saved_cookies and thread_looks_good):
                if headless:
                    browser.close()
                    browser = p.chromium.launch(headless=False, slow_mo=50)
                    context = browser.new_context(
                        viewport={'width': 1280, 'height': 900},
                        user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                                   '(KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36')
                    if saved_cookies:
                        context.add_cookies(saved_cookies)
                    page = context.new_page()
                    page.goto(base_url, wait_until='networkidle', timeout=90000)

                print("\nSolve CAPTCHA/login if needed, then press ENTER...")
                input("✅ Press ENTER when thread is fully loaded... ")

            current_cookies = context.cookies()
            with open(cookies_file, 'w', encoding='utf-8') as f:
                json.dump(current_cookies, f, indent=2)
            print(f"💾 Saved {len(current_cookies)} cookies.")

            print("\nDownloading full thread with full styling...\n")

            title_tag = soup.find('title')
            raw_title = strip_notification_prefix(
                title_tag.get_text(strip=True).split('|')[0]
            ) if title_tag else 'thread'
            safe_title = re.sub(r'[\\/*?:"<>|]', '_', raw_title)[:80]
            thread_id_match = re.search(r'\.(\d+)', base_url)
            thread_id = thread_id_match.group(1) if thread_id_match else 'unknown'

            # Use existing backup dir if found, otherwise create a new one
            if existing_dir:
                out_dir = existing_dir
            else:
                out_dir = os.path.join(args.output, f"{safe_title}_{thread_id}")

            os.makedirs(out_dir, exist_ok=True)
            assets_dir = os.path.join(out_dir, 'assets')
            os.makedirs(assets_dir, exist_ok=True)

            # Record output dir in progress and persist
            entry["out_dir"] = out_dir
            save_progress(progress)

            # Total pages detection
            page_nums = [1]
            for a in soup.find_all('a', href=True):
                m = re.search(r'/page-(\d+)', a['href'])
                if m:
                    page_nums.append(int(m.group(1)))
            last_link = soup.find('a', class_=lambda x: x and 'pageNav-jump--last' in str(x).lower())
            if last_link and last_link.get('href'):
                m = re.search(r'/page-(\d+)', last_link['href'])
                if m:
                    page_nums.append(int(m.group(1)))
            total_pages = max(page_nums) if page_nums else 1

            entry["total_pages"] = total_pages
            save_progress(progress)

            # Write thread_info.json — authoritative identity and metadata
            backup_version = get_backup_version(out_dir) if existing_dir else 1
            write_thread_info(out_dir, base_url, raw_title,
                              version=backup_version, total_pages=total_pages)

            print(f"✅ Thread: {raw_title}")
            print(f"✅ {total_pages} page(s) detected → {out_dir}\n")

            # --- Resolve mode flags ---
            retry_pages   = getattr(args, '_retry_pages', None)
            check_updates = getattr(args, '_check_updates', False)
            prev_total    = getattr(args, '_prev_total', 0)
            manual_range  = args.page_from is not None or args.page_to is not None
            completed     = set(entry.get("completed_pages", []))

            # checkUpdates: if no new pages, bail early; otherwise set manual range
            if check_updates:
                if total_pages <= prev_total:
                    print(f"  ✅ No new pages (still {total_pages}). Skipping.\n")
                    browser.close()
                    return
                print(f"  📄 New pages found: {prev_total} → {total_pages}")
                print(f"  Re-scraping from page {prev_total} (last page of previous run)\n")
                # Treat as manual range so completed-page skipping doesn't interfere
                args.page_from = prev_total
                args.page_to   = total_pages
                manual_range   = True

            # --- Build pages_to_do ---
            if retry_pages:
                # Retry pass: scrape exactly the listed pages
                pages_to_do = sorted(retry_pages)

            elif manual_range:
                # Manual --from/--to or checkUpdates: force re-scrape the full range,
                # clearing stale failures before each page
                start_page  = max(1, min(args.page_from if args.page_from is not None else 1, total_pages))
                end_page    = max(start_page, min(args.page_to if args.page_to is not None else total_pages, total_pages))
                pages_to_do = list(range(start_page, end_page + 1))

            else:
                # Normal run / resume: skip already-completed pages
                missing = [p for p in range(1, total_pages + 1) if p not in completed]
                if not missing:
                    print("✅ All pages already completed. Nothing to do.")
                    print("   Use --checkUpdates to look for new pages.")
                    browser.close()
                    return
                start_page  = missing[0]
                end_page    = total_pages
                pages_to_do = missing
                if start_page > 1:
                    print(f"🔄 Resuming from page {start_page} "
                          f"({len(completed)} page(s) already completed)\n")

            if not pages_to_do:
                print("✅ Nothing to download.")
                browser.close()
                return

            first_page = pages_to_do[0]
            remaining  = pages_to_do[1:]

            print(f"📄 Downloading {len(pages_to_do)} page(s) "
                  f"(pages {pages_to_do[0]}–{pages_to_do[-1]})\n")

            session = requests.Session()
            for cookie in current_cookies:
                session.cookies.set(cookie['name'], cookie['value'], domain=cookie.get('domain', domain))
            session.headers.update({
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                              '(KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36',
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
                'Accept-Language': 'en-US,en;q=0.9',
                'Referer': base_url,
            })

            url_to_local = {}

            # Process first page — reuse already-loaded soup if it happens to be page 1
            # and we're not forcing a re-fetch
            if first_page == 1 and not manual_range and not retry_pages:
                process_and_save_page(1, soup, out_dir, assets_dir, url_to_local,
                                      base_url, session, progress=progress)
                mark_page_complete(progress, base_url, 1)
            else:
                clear_page_failures(progress, base_url, first_page)
                p_url = base_url.rstrip('/') + f'/page-{first_page}'
                page.goto(p_url, wait_until='networkidle', timeout=60000)
                html = page.content()
                soup = make_soup(html)
                process_and_save_page(first_page, soup, out_dir, assets_dir, url_to_local,
                                      base_url, session, progress=progress)
                mark_page_complete(progress, base_url, first_page)

            for page_num in tqdm(remaining, desc="Downloading pages", unit="page"):
                p_url = base_url.rstrip('/') + f'/page-{page_num}'
                try:
                    clear_page_failures(progress, base_url, page_num)
                    page.goto(p_url, wait_until='networkidle', timeout=60000)
                    html = page.content()
                    soup = make_soup(html)
                    process_and_save_page(page_num, soup, out_dir, assets_dir, url_to_local,
                                         base_url, session, progress=progress)
                    mark_page_complete(progress, base_url, page_num)
                except Exception as e:
                    print(f"  ✗ Page {page_num}: {e}")

            # index.html always points to page 1 as the entry point
            page1_file = os.path.join(out_dir, "page-1.html")
            if os.path.exists(page1_file):
                shutil.copy(page1_file, os.path.join(out_dir, "index.html"))

            # Mark complete only if the full thread is now covered
            all_done = set(entry.get("completed_pages", [])) >= set(range(1, total_pages + 1))
            if all_done:
                mark_thread_complete(progress, base_url, total_pages)
                print(f"\n✅ Thread marked as complete in progress.json")
            else:
                entry["status"] = "in_progress"
                save_progress(progress)

            # Summary of any failed assets
            failed = entry.get("failed_assets", {})
            if failed:
                print(f"\n⚠️  {len(failed)} asset(s) failed to download and were logged to progress.json.")
                print("    Run --retryFailed to attempt downloading them again.")

            browser.close()

    except PlaywrightError as e:
        if "Executable doesn't exist" in str(e):
            print("\n❌ Chromium not installed → run: playwright install chromium")
        else:
            print(f"\n❌ Playwright error: {e}")
        return
    except Exception as e:
        print(f"\n❌ Error: {e}")
        return

    # Run V2 converter unless --V1 was specified
    use_v1 = getattr(args, 'V1', False)
    use_v2 = getattr(args, 'V2', False)
    if not use_v1:
        print("\n🔄 Running V2 converter...")
        from convert_v2 import convert as convert_v2
        convert_v2(out_dir)
    else:
        print("\n⚠️  Skipping V2 converter (--V1 specified).")

    print(f"\n🎉 DONE! Open:\n   {out_dir}\\index.html")


# ==============================================================================
# main()
# ==============================================================================

def main():
    parser = argparse.ArgumentParser(description="XenForo Thread Downloader")
    parser.add_argument('url', nargs='?', default=None, help="Thread URL")
    parser.add_argument('--cookies', default=None, help="Optional initial cookie string")
    parser.add_argument('--output', default='.', help="Parent output directory")
    parser.add_argument('--from', dest='page_from', type=int, default=None, help="First page to download")
    parser.add_argument('--to', dest='page_to', type=int, default=None, help="Last page to download")
    parser.add_argument('--retryFailed', action='store_true',
                        help="Re-scrape all pages with logged asset failures across all threads")
    parser.add_argument('--checkUpdates', nargs='?', const='__all__', default=None, metavar='URL',
                        help="Check for new pages on all tracked threads, or a specific URL")
    parser.add_argument('--V1', action='store_true',
                        help="Force V1 mode: skip converter even on new backups")
    parser.add_argument('--V2', action='store_true',
                        help="Force V2 mode: run converter after scraping (default for new backups)")
    args = parser.parse_args()

    # --retryFailed: re-scrape any page that has logged failures, across all threads
    if args.retryFailed:
        progress = load_progress()
        threads_with_failures = {
            url: entry for url, entry in progress.items()
            if entry.get("failed_assets")
        }
        if not threads_with_failures:
            print("✅ No logged failures found in progress.json — nothing to retry.")
            return

        print(f"🔄 Retrying failed assets across {len(threads_with_failures)} thread(s)...\n")
        for thread_url, entry in threads_with_failures.items():
            pages_with_failures = {}
            for asset_url, pg in entry["failed_assets"].items():
                pages_with_failures.setdefault(pg, []).append(asset_url)

            out_dir = entry.get("out_dir")
            if not out_dir or not os.path.isdir(out_dir):
                print(f"  ⚠ Skipping {thread_url} — output dir not found: {out_dir}")
                continue

            print(f"  Thread: {thread_url}")
            print(f"  Pages to re-scrape: {sorted(pages_with_failures.keys())}\n")

            retry_args = argparse.Namespace(
                url=thread_url,
                cookies=args.cookies,
                output=os.path.dirname(out_dir),
                page_from=None,
                page_to=None,
                retryFailed=False,
                checkUpdates=None,
                V1=getattr(args, 'V1', False),
                V2=getattr(args, 'V2', False),
                _retry_pages=sorted(pages_with_failures.keys()),
                _check_updates=False,
                _prev_total=0,
            )
            _run_scrape(retry_args, progress)

        print("\n🎉 Retry pass complete.")
        return

    # --checkUpdates: check one or all tracked threads for new pages
    if args.checkUpdates is not None:
        progress = load_progress()

        if args.checkUpdates == '__all__':
            candidates = {
                url: entry for url, entry in progress.items()
                if entry.get("status") in ("complete", "in_progress")
                and entry.get("out_dir")
                and os.path.isdir(entry["out_dir"])
            }
            if not candidates:
                print("✅ No tracked threads found in progress.json.")
                return
        else:
            target = normalize_base_url(args.checkUpdates)
            if target not in progress:
                print(f"❌ No progress entry found for: {target}")
                return
            candidates = {target: progress[target]}

        print(f"🔍 Checking {len(candidates)} thread(s) for new pages...\n")
        for thread_url, entry in candidates.items():
            out_dir = entry["out_dir"]
            prev_total = entry.get("total_pages", 0)
            print(f"  {thread_url}")

            update_args = argparse.Namespace(
                url=thread_url,
                cookies=args.cookies,
                output=os.path.dirname(out_dir),
                page_from=None,
                page_to=None,
                retryFailed=False,
                checkUpdates=None,
                V1=getattr(args, 'V1', False),
                V2=getattr(args, 'V2', False),
                _retry_pages=None,
                _check_updates=True,
                _prev_total=prev_total,
            )
            _run_scrape(update_args, progress)

        print("\n🎉 Update check complete.")
        return

    if not args.url:
        print("❌ A thread URL is required unless using --retryFailed or --checkUpdates.")
        return

    args._retry_pages  = None
    args._check_updates = False
    args._prev_total   = 0

    # Version flag validation
    _progress = load_progress()
    _existing_dir, _existing_info = find_existing_backup(args.output, normalize_base_url(args.url))
    if _existing_dir:
        _existing_version = int(_existing_info.get("version", 1)) if _existing_info else 1
        if args.V1 and _existing_version == 2:
            print("\u274c Error: --V1 was specified but this backup is already V2.")
            print("   Refusing to proceed to avoid downgrading an existing backup.")
            return
        if args.V2 and _existing_version == 2:
            print("\u2139 Backup is already V2. --V2 flag is redundant but harmless.")

    _run_scrape(args, _progress)


if __name__ == "__main__":
    main()
