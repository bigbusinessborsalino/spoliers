import os
import re
import time
import logging
import threading

import requests
from flask import Flask
from pymongo import MongoClient
from playwright.sync_api import sync_playwright, Page, BrowserContext

# ── Logging ──────────────────────────────────────────────────────────────────
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

# ── Flask keepalive ───────────────────────────────────────────────────────────
flask_app = Flask(__name__)

@flask_app.route("/")
def health():
    return "OK", 200

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
    caption = (
        f"🏴‍☠️ <b>One Piece Spoiler</b> — <i>@{username}</i>\n\n"
        f"{text}"
    )
    if not images:
        _tg("sendMessage", {"chat_id": TELEGRAM_CHANNEL_ID, "text": caption, "parse_mode": "HTML"})
    elif len(images) == 1:
        _tg("sendPhoto", {"chat_id": TELEGRAM_CHANNEL_ID, "photo": images[0], "caption": caption, "parse_mode": "HTML"})
    else:
        media = [{"type": "photo", "media": u} for u in images]
        media[0]["caption"] = caption
        media[0]["parse_mode"] = "HTML"
        _tg("sendMediaGroup", {"chat_id": TELEGRAM_CHANNEL_ID, "media": media})

# ── Playwright browser config ─────────────────────────────────────────────────
CHROMIUM_PATH = (
    os.environ.get("CHROMIUM_PATH")
    or "/nix/store/qa9cnw4v5xkxyip6mb9kxqfq1z4x2dx1-chromium-138.0.7204.100/bin/chromium"
)

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

# ── Inject session cookies ────────────────────────────────────────────────────
def inject_cookies(context: BrowserContext) -> None:
    context.add_cookies([
        {
            "name": "auth_token",
            "value": TWITTER_AUTH_TOKEN,
            "domain": ".x.com",
            "path": "/",
            "httpOnly": True,
            "secure": True,
            "sameSite": "None",
        },
        {
            "name": "ct0",
            "value": TWITTER_CT0,
            "domain": ".x.com",
            "path": "/",
            "httpOnly": False,
            "secure": True,
            "sameSite": "Lax",
        },
    ])
    logger.info("Session cookies injected")

# ── Scrape a single account ───────────────────────────────────────────────────
def scrape_account(page: Page, username: str) -> list[dict]:
    results = []
    url = f"https://x.com/{username}"
    logger.info("Navigating to %s", url)

    try:
        page.goto(url, wait_until="domcontentloaded", timeout=60_000)
    except Exception as exc:
        logger.error("Navigation failed for @%s: %s", username, exc)
        return results

    try:
        page.wait_for_selector('article[data-testid="tweet"]', timeout=20_000)
    except Exception:
        logger.warning("No tweets visible on @%s", username)
        return results

    articles = page.query_selector_all('article[data-testid="tweet"]')
    logger.info("Found %d articles on @%s", len(articles), username)

    for article in articles[:15]:
        try:
            text_el = article.query_selector('[data-testid="tweetText"]')
            text = text_el.inner_text() if text_el else ""

            if not HASHTAG_RE.search(text):
                continue

            link_el = article.query_selector('a[href*="/status/"]')
            if not link_el:
                continue
            href = link_el.get_attribute("href") or ""
            match = re.search(r"/status/(\d+)", href)
            if not match:
                continue
            tweet_id = match.group(1)

            images: list[str] = []
            for img in article.query_selector_all('img[src*="pbs.twimg.com/media"]'):
                src = img.get_attribute("src") or ""
                clean = re.sub(r"\?.*$", "", src)
                if clean:
                    images.append(f"{clean}?format=jpg&name=large")

            results.append({"tweet_id": tweet_id, "text": text, "images": images, "username": username})
            logger.info("Matched tweet %s from @%s (%d image(s))", tweet_id, username, len(images))

        except Exception as exc:
            logger.error("Error parsing article: %s", exc)

    return results

# ── Main poll loop ────────────────────────────────────────────────────────────
def run_scraper() -> None:
    logger.info("Scraper thread started — polling every %ds", POLL_INTERVAL)

    while True:
        try:
            with sync_playwright() as pw:
                browser = pw.chromium.launch(
                    headless=True,
                    executable_path=CHROMIUM_PATH,
                    args=BROWSER_ARGS,
                )
                context = browser.new_context(
                    user_agent=USER_AGENT,
                    locale="en-US",
                    viewport={"width": 1280, "height": 900},
                )
                inject_cookies(context)
                page = context.new_page()

                # Verify session is valid
                page.goto("https://x.com/home", wait_until="domcontentloaded", timeout=60_000)
                page.wait_for_timeout(2000)
                if "login" in page.url:
                    logger.error("Session cookies expired — please refresh TWITTER_AUTH_TOKEN and TWITTER_CT0")
                    browser.close()
                    time.sleep(POLL_INTERVAL)
                    continue
                logger.info("Session valid — logged in as authenticated user")

                for username in ACCOUNTS:
                    tweets = scrape_account(page, username)
                    for tweet in tweets:
                        tid = tweet["tweet_id"]
                        if posted_col.find_one({"tweet_id": tid}):
                            logger.info("Already posted %s — skipping", tid)
                            continue
                        logger.info("Sending new spoiler %s from @%s to Telegram", tid, tweet["username"])
                        send_to_telegram(tweet["username"], tweet["text"], tweet["images"])
                        posted_col.insert_one({
                            "tweet_id": tid,
                            "text": tweet["text"],
                            "username": tweet["username"],
                        })
                        time.sleep(1)

                browser.close()

        except Exception as exc:
            logger.error("Scraper cycle error: %s", exc, exc_info=True)

        logger.info("Sleeping %ds before next poll...", POLL_INTERVAL)
        time.sleep(POLL_INTERVAL)


# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    scraper_thread = threading.Thread(target=run_scraper, daemon=True)
    scraper_thread.start()

    port = int(os.environ.get("PORT", 5000))
    logger.info("Flask keepalive server starting on port %d", port)
    flask_app.run(host="0.0.0.0", port=port)
