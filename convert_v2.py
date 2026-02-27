"""
convert_v2.py — XenForo backup converter: V1 → V2

What it does:
  1. Scans the assets/ directory for .php files
  2. Reads magic bytes to determine if each is an image or an HTML document
  3. Renames image .php files to their correct extension (.jpg, .png, etc.)
  4. Leaves HTML .php files alone
  5. Updates all references in every page-N.html file using a full old->new map
  6. Fixes gallery embed <a href> tags that point to HTML .php files or the
     gray pixel fallback -- redirects them to the image in the child <img src>

Usage:
  python convert_v2.py                        (converts current directory)
  python convert_v2.py <backup_directory>     (converts specified directory)
  python convert_v2.py --dryrun               (preview without modifying files)
"""

import os
import json
import argparse
from bs4 import BeautifulSoup

# ==============================================================================
# Magic byte signatures for common image formats
# ==============================================================================

IMAGE_SIGNATURES = [
    (b'\xff\xd8\xff',          '.jpg'),
    (b'\x89PNG\r\n\x1a\n',    '.png'),
    (b'GIF87a',                '.gif'),
    (b'GIF89a',                '.gif'),
    (b'RIFF',                  '.webp'),
    (b'\x00\x00\x00\x0cjP  ', '.jp2'),
    (b'BM',                    '.bmp'),
    (b'\x00\x00\x01\x00',     '.ico'),
]

WEBP_MARKER = b'WEBP'

# The gray pixel placeholder used by the scraper for failed/403 assets
GRAY_PIXEL_PREFIX = "data:image/png;base64,iVBOR"


def detect_image(filepath):
    """
    Read the first 16 bytes and check against known image signatures.
    Returns the correct extension (e.g. '.jpg') if image, or None if not.
    """
    try:
        with open(filepath, 'rb') as f:
            header = f.read(16)
    except Exception as e:
        print(f"  Could not read {filepath}: {e}")
        return None

    for sig, ext in IMAGE_SIGNATURES:
        if header[:len(sig)] == sig:
            if ext == '.webp' and header[8:12] != WEBP_MARKER:
                continue
            return ext

    return None


def make_soup(html):
    try:
        return BeautifulSoup(html, 'lxml')
    except Exception:
        return BeautifulSoup(html, 'html.parser')


def is_gallery_href(href, html_php):
    """
    Returns True if this href should be replaced with the child img src.
    Catches both HTML .php hrefs and gray pixel fallback data URIs.
    """
    if not href:
        return False
    if href.startswith(GRAY_PIXEL_PREFIX):
        return True
    fname = os.path.basename(href.split('?')[0])
    return fname in html_php


# ==============================================================================
# Main conversion logic
# ==============================================================================

def convert(backup_dir, dry_run=False):
    assets_dir = os.path.join(backup_dir, 'assets')

    if not os.path.isdir(backup_dir):
        print(f"Directory not found: {backup_dir}")
        return False

    if not os.path.isdir(assets_dir):
        print(f"No assets/ directory found in: {backup_dir}")
        return False

    print(f"{'[DRY RUN] ' if dry_run else ''}Converting: {backup_dir}\n")

    # ------------------------------------------------------------------
    # Step 1: Scan assets/ for .php files and classify them
    # ------------------------------------------------------------------
    php_files = [f for f in os.listdir(assets_dir) if f.lower().endswith('.php')]

    if not php_files:
        print("No .php files found in assets/ -- nothing to convert.")
        return True

    print(f"Found {len(php_files)} .php file(s) to inspect...\n")

    rename_map = {}  # { 'index_5.php': 'index_5.jpg' }
    html_php   = set()  # .php files that are actually HTML -- leave alone

    for fname in sorted(php_files):
        fpath = os.path.join(assets_dir, fname)
        ext = detect_image(fpath)

        if ext:
            base = os.path.splitext(fname)[0]
            candidate = base + ext
            counter = 1
            while candidate in rename_map.values() or (
                os.path.exists(os.path.join(assets_dir, candidate)) and candidate != fname
            ):
                candidate = f"{base}_{counter}{ext}"
                counter += 1
            rename_map[fname] = candidate
            print(f"  IMG  {fname} -> {candidate}")
        else:
            html_php.add(fname)
            print(f"  HTM  {fname} -- HTML/other, skipping rename")

    print(f"\n  {len(rename_map)} file(s) to rename, {len(html_php)} HTML .php file(s) left alone\n")

    # ------------------------------------------------------------------
    # Step 2: Rename files on disk
    # ------------------------------------------------------------------
    if not dry_run:
        for old, new in rename_map.items():
            os.rename(os.path.join(assets_dir, old), os.path.join(assets_dir, new))

    # ------------------------------------------------------------------
    # Step 3: Update all HTML files
    # ------------------------------------------------------------------
    html_files = sorted(f for f in os.listdir(backup_dir) if f.endswith('.html'))

    if not html_files:
        print("No HTML files found in backup directory.")
        return True

    print(f"Updating references in {len(html_files)} HTML file(s)...\n")

    path_rename_map = {
        f"assets/{old}": f"assets/{new}"
        for old, new in rename_map.items()
    }

    for html_fname in html_files:
        html_path = os.path.join(backup_dir, html_fname)

        with open(html_path, 'r', encoding='utf-8', errors='replace') as f:
            content = f.read()

        modified = False

        # Step 3a: String replace all renamed asset references
        for old_ref, new_ref in path_rename_map.items():
            if old_ref in content:
                content = content.replace(old_ref, new_ref)
                modified = True

        # Step 3b: Fix gallery embed <a href> tags
        # Catches two cases:
        #   - href pointing to an HTML .php file (gallery viewer page)
        #   - href is the gray pixel data URI (scraper fallback for failed fetch)
        # In both cases, the correct target is the child <img src>.
        soup = make_soup(content)
        soup_modified = False

        for a_tag in soup.find_all('a', href=True):
            if not is_gallery_href(a_tag['href'], html_php):
                continue

            img = a_tag.find('img')
            if not img:
                continue

            img_src = img.get('src', '')
            if not img_src or img_src.startswith('data:'):
                continue

            # img src was already updated by step 3a if it was a .php image,
            # so at this point it should be the correct final path
            if a_tag['href'] != img_src:
                print(f"  LINK {html_fname}: gallery href -> {img_src}")
                a_tag['href'] = img_src
                soup_modified = True
                modified = True

        if soup_modified:
            content = str(soup)

        if modified and not dry_run:
            with open(html_path, 'w', encoding='utf-8') as f:
                f.write(content)
            print(f"  OK   Updated: {html_fname}")
        elif modified and dry_run:
            print(f"  DRY  Would update: {html_fname}")
        else:
            print(f"  --   No changes: {html_fname}")

    # ------------------------------------------------------------------
    # Step 4: Write / update thread_info.json to mark backup as V2
    # ------------------------------------------------------------------
    info_path = os.path.join(backup_dir, 'thread_info.json')
    if not dry_run:
        try:
            existing = {}
            if os.path.exists(info_path):
                with open(info_path, 'r', encoding='utf-8') as f:
                    existing = json.load(f)
            else:
                # thread_info.json missing — create it from what we can infer.
                # url: check for legacy thread_url.txt, otherwise leave blank.
                legacy_url_file = os.path.join(backup_dir, 'thread_url.txt')
                inferred_url = ''
                if os.path.exists(legacy_url_file):
                    try:
                        with open(legacy_url_file, 'r', encoding='utf-8') as f:
                            inferred_url = f.read().strip()
                        print(f"  Found legacy thread_url.txt — using URL: {inferred_url}")
                    except Exception:
                        pass
                existing = {
                    'url': inferred_url,
                    'friendly_name': os.path.basename(backup_dir),
                    'total_pages': 0,
                    'last_updated': '',
                }
                if not inferred_url:
                    print("  Warning: thread_info.json was missing and no thread_url.txt found.")
                    print("  Created thread_info.json with blank URL — edit it manually if needed.")
            existing['version'] = 2
            with open(info_path, 'w', encoding='utf-8') as f:
                json.dump(existing, f, indent=2)
            print(f"\nthread_info.json written: version set to 2")
        except Exception as e:
            print(f"\nWarning: could not write thread_info.json: {e}")
    else:
        exists = os.path.exists(info_path)
        if exists:
            print(f"\n[DRY RUN] Would update thread_info.json version to 2")
        else:
            print(f"\n[DRY RUN] Would create thread_info.json (file currently missing)")

    print(f"\nConversion {'preview complete' if dry_run else 'complete'}!")
    return True


# ==============================================================================
# Entry point
# ==============================================================================

def main():
    parser = argparse.ArgumentParser(
        description="Convert a V1 XenForo backup to V2 (rename .php images, fix gallery links)"
    )
    parser.add_argument('directory', nargs='?', default=os.getcwd(),
                        help="Path to the backup directory (default: current directory)")
    parser.add_argument('--dryrun', action='store_true',
                        help="Preview what would be changed without modifying any files")
    args = parser.parse_args()

    convert(args.directory, dry_run=args.dryrun)


if __name__ == "__main__":
    main()
