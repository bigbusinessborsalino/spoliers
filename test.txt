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

# CHECK THIS: Your log showed @REIGEN32, ensure it matches your test account exactly!
ACCOUNTS = ["pewpiece", "REIGEN326781"] 
HASHTAG_RE = re.compile(r"#ONEPIECE\d+", re.IGNORECASE)
POLL_INTERVAL = 30 

# ── MongoDB ───────────────────────────────────────────────────────────────────
mongo_client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=10000)
db = mongo_client["spoiler_bot"]
posted_col = db["posted_tweets"]
posted_col.create_index("tweet_id", unique=True)

# ── Keepalive HTTP server ─────────────────────────────────────────────────────
class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")
    def log_message(self, *args): pass

def start_http_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), _Handler)
    logger.info("Keepalive server on port %d", port)
    server.serve_forever()

# ── Telegram Listeners & Helpers ──────────────────────────────────────────────
def _tg(method: str, payload: dict) -> dict:
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/{method}"
    try:
        resp = requests.post(url, json=payload, timeout=30)
        return resp.json()
    except Exception as exc:
        logger.error("Telegram error: %s", exc)
        return {}

async def run_telegram_listener():
    """Listens for the /list command from you."""
    last_update_id = 0
    logger.info("Telegram command listener started")
    while True:
        try:
            updates = _tg("getUpdates", {"offset": last_update_id + 1, "timeout": 20})
            if updates.get("ok"):
                for update in updates.get("result", []):
                    last_update_id = update["update_id"]
                    msg = update.get("message")
                    if not msg or "text" not in msg: continue
                    
                    text = msg["text"].strip()
                    chat_id = msg["chat"]["id"]
                    
                    if text == "/list":
                        # Fetch last 10 spoilers recorded in MongoDB
                        recent = list(posted_col.find().sort("_id", -1).limit(10))
                        if not recent:
                            _tg("sendMessage", {"chat_id": chat_id, "text": "❌ No spoilers recorded in database yet."})
                        else:
                            response = "<b>📜 Latest 10 Recorded Spoilers:</b>\n\n"
                            for i, t in enumerate(recent, 1):
                                response += f"{i}. <b>@{t['username']}</b>\n{t['text'][:100]}...\n\n"
                            _tg("sendMessage", {"chat_id": chat_id, "text": response, "parse_mode": "HTML"})
        except Exception as e:
            logger.error("TG Listener error: %s", e)
        await asyncio.sleep(2)

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

# ── Stealth Scraper Logic ─────────────────────────────────────────────────────
async def scrape_account(page: Page, username: str) -> list[dict]:
    results = []
    try:
        # Navigate and wait for content
        await page.goto(f"https://x.com/{username}", wait_until="networkidle", timeout=60_000)
        await page.wait_for_timeout(5000) # Give it extra time to render
        
        title = await page.title()
        logger.info("Page title for @%s: %s", username, title)
        
        if "login" in page.url or not title:
            logger.warning("X is blocking the scraper or cookies expired for @%s", username)
            return results

        await page.wait_for_selector('article[data-testid="tweet"]', timeout=15_000)
        articles = await page.query_selector_all('article[data-testid="tweet"]')
        
        for article in articles[:10]:
            text_el = await article.query_selector('[data-testid="tweetText"]')
            text = await text_el.inner_text() if text_el else ""
            if not HASHTAG_RE.search(text): continue

            link_el = await article.query_selector('a[href*="/status/"]')
            if not link_el: continue
            href = await link_el.get_attribute("href") or ""
            m = re.search(r"/status/(\d+)", href)
            if not m: continue
            tweet_id = m.group(1)

            images = []
            for img in await article.query_selector_all('img[src*="pbs.twimg.com/media"]'):
                src = await img.get_attribute("src") or ""
                clean = re.sub(r"\?.*$", "", src)
                if clean: images.append(f"{clean}?format=jpg&name=large")

            results.append({"tweet_id": tweet_id, "text": text, "images": images, "username": username})
    except Exception as exc:
        logger.error("Scrape failed for @%s: %s", username, exc)
    return results

async def run_scraper() -> None:
    async with async_playwright() as pw:
        # STEALTH ARGS added here
        browser = await pw.chromium.launch(headless=True, args=[
            "--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage",
            "--disable-blink-features=AutomationControlled"
        ])
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        )
        await context.add_cookies([
            {"name": "auth_token", "value": TWITTER_AUTH_TOKEN, "domain": ".x.com", "path": "/", "httpOnly": True, "secure": True},
            {"name": "ct0", "value": TWITTER_CT0, "domain": ".x.com", "path": "/", "httpOnly": False, "secure": True},
        ])
        page = await context.new_page()

        while True:
            for username in ACCOUNTS:
                tweets = await scrape_account(page, username)
                for tweet in tweets:
                    if not posted_col.find_one({"tweet_id": tweet["tweet_id"]}):
                        send_to_telegram(tweet["username"], tweet["text"], tweet["images"])
                        posted_col.insert_one({"tweet_id": tweet["tweet_id"], "text": tweet["text"], "username": tweet["username"], "ts": time.time()})
                await asyncio.sleep(5)
            await asyncio.sleep(POLL_INTERVAL)

if __name__ == "__main__":
    threading.Thread(target=start_http_server, daemon=True).start()
    # Run both the scraper and the Telegram listener
    loop = asyncio.get_event_loop()
    loop.create_task(run_telegram_listener())
    loop.run_until_complete(run_scraper())
