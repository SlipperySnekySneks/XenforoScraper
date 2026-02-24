# XenForo Thread Scraper

Downloads a XenForo forum thread to a self-contained folder of HTML files that can be read offline in any browser, with full styling, images, attachments, and working navigation between pages.

---

## Prerequisites

Run the included `install_prerequisites.bat` first. It will check for Python, install it if missing, and install all required packages automatically.

If you prefer to do it manually:

```
pip install requests beautifulsoup4 lxml tqdm playwright
playwright install chromium
```

---

## First Run / Logging In

The script uses a real browser (Chromium) under the hood to load pages. On the first run it will open a visible browser window so you can log in to the forum manually. Once you're logged in and the thread is visible, switch back to the terminal and press **Enter** to continue.

Your login session is saved to a file named `cookies_<domain>.json` in the folder where you run the script. On subsequent runs the script will reuse this session automatically and won't ask you to log in again unless the session has expired.

> **WARNING:** The cookies file contains your session credentials. Keep it private and don't share it. The saved HTML files themselves contain no credentials, but keep in mind it will show the site's content from the perspective of your logged-in account.

---

## Basic Usage

```
python xenforo_scraper.py <thread_url>
```

**Example:**
```
python xenforo_scraper.py https://example.org/forum/threads/some-thread.12345
```

The script will create a folder in the current directory named after the thread title, containing one HTML file per page plus an `assets` subfolder with all downloaded images, stylesheets, and fonts.

Open `index.html` inside the folder to start reading from the beginning.

---

## Options

### `--output`
Where to create the thread folder. Defaults to the current directory.

```
python xenforo_scraper.py <url> --output C:\Archives
```

### `--from` and `--to`
Download only a range of pages rather than the whole thread. Either can be used on its own.

| Command | Effect |
|---|---|
| `--from 5` | Start at page 5, continue to the end |
| `--to 20` | Download pages 1 through 20 |
| `--from 5 --to 20` | Download pages 5 through 20 only |

```
python xenforo_scraper.py <url> --from 5 --to 20
```

Values are automatically clamped to the thread's actual page count, so `--to 9999` on a 50-page thread will simply stop at page 50.

When using `--from`, `index.html` will open at your chosen start page rather than page 1.

---

## Output Structure

```
Thread Title_12345/
├── index.html        ← start here (copy of the first downloaded page)
├── page-1.html
├── page-2.html
├── ...
└── assets/
    ├── style.css
    ├── avatar.jpg
    ├── image.png
    └── ...
```

Pagination links within each page allow you to navigate between saved pages directly in the browser.

---

## Notes

- **Images:** Inline images are downloaded and displayed at their natural size. Clicking an inline image opens the full-size version in a new tab. Attachment thumbnails link to the locally saved full-size file.
- **Media embeds:** Images embedded via the forum's media system are saved and linked correctly alongside regular inline images.
- **Failed assets:** If an image or background returns a 403 error or otherwise fails to download, it is replaced with a dark gray placeholder so the layout isn't broken.
- **Duplicate assets:** Each unique asset is only downloaded once regardless of how many pages it appears on.
- **Partial runs:** If you download a page range, navigation links to pages outside that range will be present in the HTML but won't have a corresponding file to open.
