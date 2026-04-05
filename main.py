import os
import re
import time
import asyncio
import logging
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import requests
from pymongo import MongoClient
from playwright.async_api import async_playwright, Page, BrowserContext

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHANNEL_ID = os.environ["TELEGRAM_CHANNEL_ID"]
MONGO_URI = os.environ["MONGO_URI"]
TWITTER_AUTH_TOKEN = os.environ["TWITTER_AUTH_TOKEN"]
TWITTER_CT0 = os.environ["TWITTER_CT0"]

ACCOUNTS = ["pewpiece", "REIGEN326781"]
HASHTAG_RE = re.compile(r"#ONEPIECE\d+", re.IGNORECASE)
POLL_INTERVAL = 30  # seconds

# ── MongoDB ───────────────────────────────────────────────────────────────────
mongo_client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=10000)
db = mongo_client["spoiler_bot"]
posted_col = db["posted_tweets"]
posted_col.create_index("tweet_id", unique=True)

# ── Keepalive HTTP server (no Flask / no greenlet needed) ─────────────────────
class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")

    def log_message(self, *args):
        pass  # suppress request noise


def start_http_server():
    port = int(os.environ.get("PORT", 5000))
    server = HTTPServer(("0.0.0.0", port), _Handler)
    logger.info("Keepalive server on port %d", port)
    server.serve_forever()

# ── Telegram helpers ──────────────────────────────────────────────────────────
def _tg(method: str, payload: dict) -> dict:
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/{method}"
    try:
        resp = requests.post(url, json=payload, timeout=30)
        data = resp.json()
        if not data.get("ok"):
            logger.warning("Telegram %s failed: %s", method, data)
        return data
    except Exception as exc:
        logger.error("Telegram request error: %s", exc)
        return {}


def send_to_telegram(username: str, text: str, images: list[str]) -> None:
    caption = f"🏴‍☠️ <b>One Piece Spoiler</b> — <i>@{username}</i>\n\n{text}"
    if not images:
        _tg("sendMessage", {"chat_id": TELEGRAM_CHANNEL_ID, "text": caption, "parse_mode": "HTML"})
    elif len(images) == 1:
        _tg("sendPhoto", {"chat_id": TELEGRAM_CHANNEL_ID, "photo": images[0], "caption": caption, "parse_mode": "HTML"})
    else:
        media = [{"type": "photo", "media": u} for u in images]
        media[0]["caption"] = caption
        media[0]["parse_mode"] = "HTML"
        _tg("sendMediaGroup", {"chat_id": TELEGRAM_CHANNEL_ID, "media": media})

# ── Playwright config ─────────────────────────────────────────────────────────
CHROMIUM_PATH = os.environ.get("CHROMIUM_PATH") or None  # None = use Playwright's own

BROWSER_ARGS = [
    "--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage",
    "--disable-setuid-sandbox", "--single-process",
    "--disable-extensions", "--no-first-run",
]

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


async def inject_cookies(context: BrowserContext) -> None:
    await context.add_cookies([
        {"name": "auth_token", "value": TWITTER_AUTH_TOKEN, "domain": ".x.com",
         "path": "/", "httpOnly": True, "secure": True, "sameSite": "None"},
        {"name": "ct0", "value": TWITTER_CT0, "domain": ".x.com",
         "path": "/", "httpOnly": False, "secure": True, "sameSite": "Lax"},
    ])


async def scrape_account(page: Page, username: str) -> list[dict]:
    results = []
    url = f"https://x.com/{username}"
    logger.info("Navigating to %s", url)

    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=60_000)
        # Log the title to see if we are stuck on a login or error page
        title = await page.title()
        logger.info("Page title for @%s: %s", username, title)
    except Exception as exc:
        logger.error("Navigation failed for @%s: %s", username, exc)
        return results

    try:
        # If this fails, the log now tells us the Page Title above
        await page.wait_for_selector('article[data-testid="tweet"]', timeout=20_000)
    except Exception:
        logger.warning("No tweets visible on @%s. Current URL: %s", username, page.url)
        return results
    # ... rest of the function

    articles = await page.query_selector_all('article[data-testid="tweet"]')
    logger.info("Found %d articles on @%s", len(articles), username)

    for article in articles[:15]:
        try:
            text_el = await article.query_selector('[data-testid="tweetText"]')
            text = await text_el.inner_text() if text_el else ""

            if not HASHTAG_RE.search(text):
                continue

            link_el = await article.query_selector('a[href*="/status/"]')
            if not link_el:
                continue
            href = await link_el.get_attribute("href") or ""
            m = re.search(r"/status/(\d+)", href)
            if not m:
                continue
            tweet_id = m.group(1)

            images: list[str] = []
            for img in await article.query_selector_all('img[src*="pbs.twimg.com/media"]'):
                src = await img.get_attribute("src") or ""
                clean = re.sub(r"\?.*$", "", src)
                if clean:
                    images.append(f"{clean}?format=jpg&name=large")

            results.append({"tweet_id": tweet_id, "text": text, "images": images, "username": username})
            logger.info("Matched tweet %s from @%s (%d image(s))", tweet_id, username, len(images))

        except Exception as exc:
            logger.error("Error parsing article: %s", exc)

    return results


async def run_scraper() -> None:
    logger.info("Scraper started — polling every %ds", POLL_INTERVAL)

    while True:
        try:
            async with async_playwright() as pw:
                launch_kwargs = {"headless": True, "args": BROWSER_ARGS}
                if CHROMIUM_PATH:
                    launch_kwargs["executable_path"] = CHROMIUM_PATH
                browser = await pw.chromium.launch(**launch_kwargs)
                context = await browser.new_context(
                    user_agent=USER_AGENT, locale="en-US",
                    viewport={"width": 1280, "height": 900},
                )
                await inject_cookies(context)
                page = await context.new_page()

                # Verify session
                await page.goto("https://x.com/home", wait_until="domcontentloaded", timeout=60_000)
                await page.wait_for_timeout(2000)
                if "login" in page.url:
                    logger.error("Session cookies expired — update TWITTER_AUTH_TOKEN and TWITTER_CT0")
                    await browser.close()
                    await asyncio.sleep(POLL_INTERVAL)
                    continue
                logger.info("Session valid")

                for username in ACCOUNTS:
                    tweets = await scrape_account(page, username)
                    for tweet in tweets:
                        tid = tweet["tweet_id"]
                        if posted_col.find_one({"tweet_id": tid}):
                            logger.info("Already posted %s — skipping", tid)
                            continue
                        logger.info("Sending %s from @%s to Telegram", tid, tweet["username"])
                        send_to_telegram(tweet["username"], tweet["text"], tweet["images"])
                        posted_col.insert_one({"tweet_id": tid, "text": tweet["text"], "username": tweet["username"]})
                        await asyncio.sleep(1)

                await browser.close()

        except Exception as exc:
            logger.error("Scraper cycle error: %s", exc, exc_info=True)

        logger.info("Sleeping %ds...", POLL_INTERVAL)
        await asyncio.sleep(POLL_INTERVAL)


# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    http_thread = threading.Thread(target=start_http_server, daemon=True)
    http_thread.start()
    asyncio.run(run_scraper())
