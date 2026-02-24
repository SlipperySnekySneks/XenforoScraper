import argparse
import os
import re
import shutil
import json
from urllib.parse import urljoin, urlparse
import requests
from bs4 import BeautifulSoup
from tqdm import tqdm
from playwright.sync_api import sync_playwright, Error as PlaywrightError

# 1x1 dark gray pixel as a data URI — used as fallback for 403/failed assets
GRAY_PIXEL = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="


def normalize_base_url(url):
    url = url.rstrip('/')
    url = re.sub(r'/page-\d+$', '', url)
    return url


def download_asset(full_url, session, assets_dir, url_to_local, desc_prefix=""):
    """Download any asset (images, fonts, thumbs, backgrounds) with progress.
    Falls back to a dark gray placeholder on 403 or other failures."""
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

        # --- CHANGE 1: 403 fallback ---
        if r.status_code == 403:
            print(f"  ⚠ 403 Forbidden (using gray fallback): {full_url[:80]}")
            url_to_local[full_url] = GRAY_PIXEL
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
        # Return gray fallback for any failure so backgrounds don't break layout
        url_to_local[full_url] = GRAY_PIXEL
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

    # @import
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
    """
    CHANGE 3: Wrap inline post images in <a target="_blank"> so clicking
    opens the full image in a new tab. Skips avatars and already-linked images.
    """
    selectors = ['img.bbImage', 'img.bbCodeImage']
    candidates = []
    for sel in selectors:
        candidates.extend(soup.select(sel))

    # Also catch any <img> inside .message-cell--main that isn't an avatar
    for img in soup.select('.message-cell--main img'):
        if img not in candidates and 'avatar' not in ' '.join(img.get('class', [])):
            candidates.append(img)

    for img in candidates:
        # Don't double-wrap if already inside an <a>
        if img.parent and img.parent.name == 'a':
            continue
        src = img.get('src', '')
        if not src or src.startswith('data:'):
            continue
        wrapper = soup.new_tag('a', href=src, target='_blank', rel='noopener noreferrer')
        wrapper['style'] = 'display:inline-block;cursor:zoom-in;'
        img.wrap(wrapper)


def inject_xenforo_fixes(soup):
    """Comprehensive XenForo layout restoration (centering, backgrounds, post separation, thumbnails)"""
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

/* CHANGE 1: Dark gray fallback for any background that 403'd */
body, html {
    background: #3a3a3a !important;
    margin: 0;
    padding: 0;
}

.p-pageWrapper {
    max-width: 1280px !important;
    margin: 20px auto !important;
    background: #ffffff !important;
    box-shadow: 0 0 15px rgba(0,0,0,0.12) !important;
    border-radius: 6px;
    overflow: hidden;
}
.p-body { background: transparent !important; padding: 20px 15px !important; }
.p-body-inner, .pageContent { max-width: 100% !important; margin: 0 auto !important; }

/* Post separation + styling */
.message, .message--post, .block--messages .message {
    background: #fff !important;
    border: 1px solid #d8d8d8 !important;
    border-radius: 4px !important;
    margin-bottom: 20px !important;
    box-shadow: 0 1px 3px rgba(0,0,0,0.05) !important;
}
.message .message-inner { display: flex !important; }
.message-cell--user { background: #f8f9fa !important; border-right: 1px solid #e5e5e5 !important; padding: 15px 12px !important; width: 140px !important; }
.message-cell--main { padding: 15px !important; flex: 1 !important; }

/* Attachment thumbnails */
.attachment, .attachment-icon, .bbImageWrapper .thumbnail, .thumbnail {
    border: 1px solid #ddd !important;
    background: #fafafa !important;
    padding: 6px !important;
    margin: 8px 0 !important;
    max-width: 240px !important;
}
.attachment .thumbnail img, .bbImageWrapper img {
    max-height: 200px !important;
    max-width: 100% !important;
    height: auto !important;
}

/* CHANGE 2: Fix inline image distortion — constrain width, never force height */
img.bbImage, .bbCodeImage, .message img:not(.avatar) {
    max-width: 100% !important;
    width: auto !important;
    height: auto !important;
    border: 1px solid #eee !important;
    display: block !important;
    margin: 4px 0 !important;
}

/* General containers */
.p-nav, .p-header { background: #2a2a2a !important; color: #fff !important; }
.block, .block-container { border: 1px solid #e0e0e0 !important; }
"""
    head.append(fix_style)


def make_soup(html):
    try:
        return BeautifulSoup(html, 'lxml')
    except Exception:
        print("⚠️  lxml not found — falling back (slower). pip install lxml")
        return BeautifulSoup(html, 'html.parser')


def process_and_save_page(page_num, soup, out_dir, assets_dir, url_to_local, base_url, session):
    """Full processing with relative URL support"""
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

    # Images, srcset, lazy
    for img in soup.find_all('img'):
        src = img.get('data-src') or img.get('src')
        if src and not src.startswith(('data:', '#', 'javascript:')):
            full = urljoin(base_url, src)
            local = download_asset(full, session, assets_dir, url_to_local)
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
                    loc = download_asset(full, session, assets_dir, url_to_local)
                    new_set.append(loc + (' ' + s[1] if len(s) > 1 else ''))
                else:
                    new_set.append(part)
            img['srcset'] = ', '.join(new_set)

    # Attachments + thumbnails
    for a in soup.find_all('a', href=True):
        href_lower = a['href'].lower()
        if 'attachment' in href_lower or 'attachments/' in href_lower:
            full = urljoin(base_url, a['href'])
            local = download_asset(full, session, assets_dir, url_to_local)
            a['href'] = local

    # Media embed links (index.php?media/...) — rewrite href to local asset
    for a in soup.find_all('a', href=True):
        if 'index.php?media/' in a['href']:
            full = urljoin(base_url, a['href'])
            local = download_asset(full, session, assets_dir, url_to_local)
            a['href'] = local

    # Video/audio
    for tag in soup.find_all(['video', 'audio', 'source']):
        for attr in ['src', 'data-src']:
            val = tag.get(attr)
            if val and not val.startswith(('data:', '#')):
                full = urljoin(base_url, val)
                local = download_asset(full, session, assets_dir, url_to_local)
                tag[attr] = local
                if attr == 'data-src':
                    del tag[attr]

    # Inline styles (backgrounds)
    rewrite_inline_styles(soup, base_url, session, assets_dir, url_to_local)

    # Pagination
    for a in soup.find_all('a', href=True):
        href = a['href']
        m = re.search(r'/page-(\d+)', href)
        if m:
            a['href'] = f"page-{int(m.group(1))}.html"
        elif re.search(r'threads/[^/]+\.\d+/?$', href):
            a['href'] = "page-1.html"

    # CHANGE 3: Make inline images clickable (open in new tab)
    make_post_images_clickable(soup)

    # Inject layout fixes
    inject_xenforo_fixes(soup)

    # Save page
    filepath = os.path.join(out_dir, f"page-{page_num}.html")
    with open(filepath, 'w', encoding='utf-8') as f:
        f.write(str(soup))


def main():
    parser = argparse.ArgumentParser(description="XenForo Thread Downloader - Full Forum Look")
    parser.add_argument('url', help="Thread URL")
    parser.add_argument('--cookies', default=None, help="Optional initial cookie string")
    parser.add_argument('--output', default='.', help="Parent output directory")
    parser.add_argument('--from', dest='page_from', type=int, default=None, help="First page to download (default: 1)")
    parser.add_argument('--to', dest='page_to', type=int, default=None, help="Last page to download (default: last page)")
    args = parser.parse_args()

    base_url = normalize_base_url(args.url)
    domain = urlparse(base_url).netloc
    cookies_file = os.path.join(os.getcwd(), f"cookies_{domain.replace('.', '_')}.json")

    print(f"Domain: {domain} | Cookies file: {cookies_file}")

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
                user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36',
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
                    context = browser.new_context(viewport={'width': 1280, 'height': 900},
                                                  user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36')
                    if saved_cookies:
                        context.add_cookies(saved_cookies)
                    page = context.new_page()
                    page.goto(base_url, wait_until='networkidle', timeout=90000)

                print("\nSolve CAPTCHA/login if needed, then press ENTER...")
                input("✅ Press ENTER when thread is fully loaded... ")

            # Save cookies
            current_cookies = context.cookies()
            with open(cookies_file, 'w', encoding='utf-8') as f:
                json.dump(current_cookies, f, indent=2)
            print(f"💾 Saved {len(current_cookies)} cookies.")

            print("\nDownloading full thread with full styling...\n")

            title_tag = soup.find('title')
            raw_title = title_tag.get_text(strip=True).split('|')[0].strip() if title_tag else 'thread'
            safe_title = re.sub(r'[\\/*?:"<>|]', '_', raw_title)[:80]
            thread_id_match = re.search(r'\.(\d+)', base_url)
            thread_id = thread_id_match.group(1) if thread_id_match else 'unknown'

            out_dir = os.path.join(args.output, f"{safe_title}_{thread_id}")
            os.makedirs(out_dir, exist_ok=True)
            assets_dir = os.path.join(out_dir, 'assets')
            os.makedirs(assets_dir, exist_ok=True)

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

            print(f"✅ Thread: {raw_title}")
            print(f"✅ {total_pages} page(s) detected → {out_dir}\n")

            start_page = args.page_from if args.page_from is not None else 1
            end_page = args.page_to if args.page_to is not None else total_pages

            # Clamp to valid range
            start_page = max(1, min(start_page, total_pages))
            end_page = max(start_page, min(end_page, total_pages))

            print(f"📄 Downloading pages {start_page}–{end_page}\n")

            session = requests.Session()
            for cookie in current_cookies:
                session.cookies.set(cookie['name'], cookie['value'], domain=cookie.get('domain', domain))
            session.headers.update({
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36',
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
                'Accept-Language': 'en-US,en;q=0.9',
                'Referer': base_url,
            })

            url_to_local = {}

            if start_page == 1:
                process_and_save_page(1, soup, out_dir, assets_dir, url_to_local, base_url, session)
            else:
                # Navigate to the actual start page
                p_url = base_url.rstrip('/') + f'/page-{start_page}'
                page.goto(p_url, wait_until='networkidle', timeout=60000)
                html = page.content()
                soup = make_soup(html)
                process_and_save_page(start_page, soup, out_dir, assets_dir, url_to_local, base_url, session)

            for page_num in tqdm(range(start_page + 1, end_page + 1), desc="Downloading pages", unit="page"):
                p_url = base_url.rstrip('/') + f'/page-{page_num}'
                try:
                    page.goto(p_url, wait_until='networkidle', timeout=60000)
                    html = page.content()
                    soup = make_soup(html)
                    process_and_save_page(page_num, soup, out_dir, assets_dir, url_to_local, base_url, session)
                except Exception as e:
                    print(f"  ✗ Page {page_num}: {e}")

            shutil.copy(os.path.join(out_dir, f"page-{start_page}.html"), os.path.join(out_dir, "index.html"))
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

    print(f"\n🎉 DONE! Open:\n   {out_dir}\\index.html")
    print("   (Forum look fully restored — centered posts, backgrounds, thumbnails, separation)")


if __name__ == "__main__":
    main()