# XenForo Thread Archiver — User Manual

## What is this?

This program saves XenForo forum threads to your computer for offline reading. It creates a permanent local copy that mostly preserves the appearance of the thread as it appeared on the live forum.

The tool downloads the full HTML of each page in the thread along with all associated stylesheets, images, avatars, and fonts. It rewrites the links in the HTML so they point to the local copies of these files instead of the original online locations. The result is a self-contained folder. Opening the index.html file in a web browser displays the thread with its original layout, styling, images, and post formatting, and no internet connection is needed.

The program handles authentication by opening a browser window for manual login and CAPTCHA solving on the first run. It then saves the session cookies so subsequent runs can proceed automatically without further interaction. It tracks which pages have been downloaded, so if the process is interrupted, running the same command again resumes from the last successful page.

It can also check an already saved thread for new posts and download only the additional content that has appeared since the last save.

The tool consists of two main scripts. The scraper handles the downloading and page processing. The converter performs final cleanup and organization of the saved files. In typical use, only the scraper needs to be run directly. The converter executes automatically when the scrape completes.

---

## Requirements

**Python 3.8+** with the following packages:

```
pip install requests beautifulsoup4 lxml tqdm playwright
playwright install chromium
```

Both scripts must be kept in the same directory.

---

## Quick Start

```
python xenforo_scraper.py https://example.com/forum/threads/some-thread.12345/
```

The first time you run this on a forum that requires login, a visible browser window will open. Log in, solve any CAPTCHA if prompted, then press **Enter** in the terminal. Your session cookies are saved automatically for future runs.

---

## Output Structure

Each downloaded thread gets its own folder inside the output directory (default: the current directory):

```
Thread Title_12345/
  index.html          <- entry point, opens page 1
  page-1.html
  page-2.html
  ...
  page-N.html
  assets/             <- all CSS, images, fonts downloaded locally
  thread_info.json    <- metadata file (URL, version, friendly name, etc.)
```

Opening `index.html` in any browser gives you the full thread with styling intact.

---

## Backup Versions

The scraper produces two possible output formats:

**V1** — Raw archive. Images served by the forum as `.php` URLs are saved with `.php` extensions and some gallery embed links may not work correctly.

**V2** — Cleaned archive. `.php` image files are renamed to their real extensions (`.jpg`, `.png`, `.gif`, etc.) and gallery embed click behaviour is fixed. This is the default for all new scrapes — the converter runs automatically at the end of every scrape unless `--V1` is specified.

The version of a backup is stored in `thread_info.json` as the `"version"` field.

---

## `xenforo_scraper.py` — Full Reference

### Basic usage

```
python xenforo_scraper.py <url> [options]
```

### Arguments

| Argument | Description |
|---|---|
| `url` | The thread URL to download. Can be any page of the thread — the scraper normalises it to the base URL automatically. Required unless using `--retryFailed` or `--checkUpdates`. |
| `--output <path>` | Directory where backup folders are created. Defaults to the current working directory. |
| `--from <n>` | Start downloading from page `n`. Pages before `n` are not touched. |
| `--to <n>` | Stop downloading after page `n`. |
| `--retryFailed` | Re-scrape all pages across all tracked threads that had failed asset downloads. No URL required. |
| `--checkUpdates [url]` | Check for new pages. With no argument, checks all tracked threads. With a URL, checks only that thread. |
| `--V1` | Skip the V2 converter after scraping. The backup is saved in V1 format. Cannot be used on a thread that already has a V2 backup. |
| `--V2` | Explicitly request V2 conversion after scraping. This is the default behaviour and the flag is normally not needed. |
| `--cookies <string>` | Pass an initial cookie string. Rarely needed — the scraper handles cookie persistence automatically. |

### Examples

**Download a full thread (first run):**
```
python xenforo_scraper.py https://forum.example.com/threads/my-thread.9999/
```

**Resume an interrupted download:**
```
python xenforo_scraper.py https://forum.example.com/threads/my-thread.9999/
```
Exactly the same command. The scraper detects which pages are already complete and picks up from the first missing one.

**Download into a specific folder:**
```
python xenforo_scraper.py https://forum.example.com/threads/my-thread.9999/ --output D:\Backups
```

**Download only pages 10 through 20:**
```
python xenforo_scraper.py https://forum.example.com/threads/my-thread.9999/ --from 10 --to 20
```

**Re-scrape a specific page range** (e.g. to fix something):
```
python xenforo_scraper.py https://forum.example.com/threads/my-thread.9999/ --from 5 --to 5
```
Using `--from`/`--to` always force re-downloads those pages regardless of prior completion status, and clears any previously logged failures for those pages.

**Check all tracked threads for new posts:**
```
python xenforo_scraper.py --checkUpdates
```

**Check a specific thread for new posts:**
```
python xenforo_scraper.py --checkUpdates https://forum.example.com/threads/my-thread.9999/
```
When new pages are found, the scraper re-scrapes the last page of the previous run (in case new posts were added to it since) and then downloads all genuinely new pages.

**Retry all failed asset downloads:**
```
python xenforo_scraper.py --retryFailed
```

**Download without running the V2 converter** (keep as V1):
```
python xenforo_scraper.py https://forum.example.com/threads/my-thread.9999/ --V1
```

---

## `convert_v2.py` — Full Reference

Converts a V1 backup to V2. Can also be re-run safely on a V2 backup to process any new pages added since the last conversion — it is a no-op on pages that are already clean.

The scraper calls this automatically at the end of every scrape. You would run it manually only when upgrading an existing V1 backup that was created before automatic conversion was added.

### Usage

```
python convert_v2.py [directory] [--dryrun]
```

### Arguments

| Argument | Description |
|---|---|
| `directory` | Path to the backup folder to convert. Defaults to the current working directory if omitted. |
| `--dryrun` | Preview all changes without modifying any files. Prints what would be renamed and which HTML files would be updated. Recommended before running on a backup for the first time. |

### What it does

1. Scans `assets/` for files with a `.php` extension
2. Reads the first 16 bytes (magic bytes) of each to determine its true file type
3. Files identified as images are renamed to their correct extension (`.jpg`, `.png`, `.gif`, `.webp`, etc.)
4. Files identified as HTML documents are left alone
5. All HTML page files are updated to reflect the renamed assets
6. Gallery embed `<a>` links that pointed to HTML gallery pages (or to a gray placeholder due to a failed download) are redirected to the actual image file
7. Writes or updates `thread_info.json` to record `"version": 2`. If `thread_info.json` does not exist, it is created. The `url` field is populated from `thread_url.txt` if that legacy file is present, otherwise it is left blank and a warning is printed.

### Examples

**Preview conversion without changing anything:**
```
python convert_v2.py "D:\Backups\My Thread_9999" --dryrun
```

**Run conversion:**
```
python convert_v2.py "D:\Backups\My Thread_9999"
```

**Run from inside the backup folder:**
```
cd "D:\Backups\My Thread_9999"
python convert_v2.py
```

---

## How Login and Cookies Work

The scraper uses a real Chromium browser (via Playwright) to load pages, which means it handles JavaScript rendering, CAPTCHAs, and login the same way a normal browser would.

**First run on a new forum:**
A visible browser window opens. Log in to the forum normally, complete any CAPTCHA, wait for the thread to load fully, then switch back to the terminal and press **Enter**. The scraper saves your session cookies to a file named `cookies_<domain>.json` in the script's working directory.

**Subsequent runs:**
The browser starts headless (no window). Your saved cookies are loaded automatically. If the session has expired or the forum rejects the cookies, the scraper opens a visible window again and prompts you to log in.

Cookie files are per-domain, so each forum you scrape gets its own cookie file. They persist indefinitely until you delete them or the forum invalidates the session.

---

## Progress Tracking

The scraper maintains a `progress.json` file in its working directory. This file tracks every thread it has ever worked on, including which pages have been successfully downloaded, which asset downloads failed and on which page, the path to the backup directory, and the total page count and overall status.

This file is what enables automatic resume — if a scrape is interrupted for any reason (crash, power loss, manual Ctrl+C), simply re-run the same command and it will continue from the last successfully completed page. The page that was actively downloading at the time of interruption will be re-scraped from scratch.

`progress.json` is written atomically after each page completes, so it cannot be corrupted by an abrupt termination.

---

## `thread_info.json`

Each backup directory contains a `thread_info.json` file that serves as its identity and metadata record. Example:

```json
{
  "url": "https://forum.example.com/threads/my-thread.9999",
  "friendly_name": "My Thread Title",
  "version": 2,
  "total_pages": 47,
  "last_updated": "2026-02-25T14:30:00"
}
```

| Field | Description |
|---|---|
| `url` | The canonical base URL of the thread. Used by the scraper to recognise an existing backup even if the folder has been renamed. |
| `friendly_name` | The thread title as extracted from the page, with the forum name and any unread notification counts stripped. Can be edited freely. |
| `version` | `1` for a raw V1 archive, `2` for a converted V2 archive. |
| `total_pages` | The page count at the time of the last scrape. |
| `last_updated` | ISO 8601 timestamp of the last time this file was written. |

The `friendly_name` field is intended as a human-readable label for future tooling. You can edit it to anything you like without affecting the scraper's ability to identify the backup — identity is determined by the `url` field only.

This file is written by the scraper. The converter writes it only if it is missing, to ensure V2 status is always recorded even when the converter is run standalone.

---

## Failed Assets

Some assets (typically avatars or images behind login walls on certain forums) return HTTP 403 and cannot be downloaded. When this happens, the scraper substitutes a small dark gray placeholder image so the layout is preserved, and logs the failed URL in `progress.json` against the page number it appeared on.

At the end of a scrape, a summary is printed if any failures occurred:

```
⚠️  12 asset(s) failed to download and were logged to progress.json.
    Run --retryFailed to attempt downloading them again.
```

Running `--retryFailed` re-scrapes the affected pages. If the assets are still unavailable (e.g. permanently 403), they will remain as gray placeholders and stay logged in `progress.json`.

---

## Typical Workflows

### Archiving a new thread from scratch
```
python xenforo_scraper.py https://forum.example.com/threads/thread.1234/
```
Log in if prompted, press Enter, wait. The V2 converter runs automatically at the end.

### Archiving a long thread over multiple sessions
```
# Session 1 — interrupted after 30 pages
python xenforo_scraper.py https://forum.example.com/threads/thread.1234/
^C

# Session 2 — automatically resumes from page 31
python xenforo_scraper.py https://forum.example.com/threads/thread.1234/
```

### Keeping an archive up to date
```
python xenforo_scraper.py --checkUpdates
```

### Upgrading a V1 backup created before automatic conversion was added
```
python convert_v2.py "D:\Backups\My Thread_1234"
```

### Checking what the converter would do before committing
```
python convert_v2.py "D:\Backups\My Thread_1234" --dryrun
```

---

## Limitations and Known Behaviour

**Spoilers are expanded by default.** XenForo's spoiler toggle requires JavaScript which does not run from local `file://` URLs. All spoiler content is made permanently visible in the saved HTML.

**External embeds will not play.** Videos embedded from third-party services (YouTube, etc.) require an internet connection and cannot be saved locally. Their placeholder thumbnails are saved where available.

**Gallery embed click behaviour.** After V2 conversion, clicking a gallery image opens the full-size image directly. In a V1 archive, clicking may open a non-functional gallery page or a gray placeholder — right-click → Open Image in New Tab works in both cases.

**Sessions expire.** If significant time has passed since your last scrape of a given forum, you may be prompted to log in again when running `--checkUpdates` or `--retryFailed`.

**Both scripts must be in the same directory.** The scraper imports `convert_v2` directly at runtime. If they are in different locations, the V2 conversion step will fail with an import error.
