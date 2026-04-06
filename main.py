import os
import re
import time
import asyncio
import logging
import threading
import html
from http.server import BaseHTTPRequestHandler, HTTPServer

import requests
from pymongo import MongoClient
from playwright.async_api import async_playwright, Page, Route

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHANNEL_ID = os.environ.get("TELEGRAM_CHANNEL_ID")
MONGO_URI = os.environ.get("MONGO_URI")
TWITTER_AUTH_TOKEN = os.environ.get("TWITTER_AUTH_TOKEN")
TWITTER_CT0 = os.environ.get("TWITTER_CT0")

# Checking both accounts now
ACCOUNTS = ["pewpiece", "REIGEN326781"] 
HASHTAG_RE = re.compile(r"#ONEPIECE\d+", re.IGNORECASE)
POLL_INTERVAL = 45 

# Global trigger for the /latest command
FORCE_CHECK = False

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
    server.serve_forever()

# ── Telegram Helpers ──────────────────────────────────────────────────────────
def _tg(method: str, payload: dict) -> dict:
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/{method}"
    try:
        resp = requests.post(url, json=payload, timeout=30)
        data = resp.json()
        if not data.get("ok"):
            err_msg = data.get("description", "")
            # Suppress the Render overlap spam in the logs
            if "Conflict" not in err_msg:
                logger.error("❌ Telegram %s FAILED: %s", method, err_msg)
        return data
    except Exception as exc:
        logger.error("Telegram request error: %s", exc)
        return {}

def send_to_telegram(username: str, text: str, images: list[str]) -> None:
    safe_text = html.escape(text)
    caption = f"🏴‍☠️ <b>One Piece Spoiler</b> — <i>@{username}</i>\n\n{safe_text}"
    
    if not images:
        _tg("sendMessage", {"chat_id": TELEGRAM_CHANNEL_ID, "text": caption, "parse_mode": "HTML"})
    elif len(images) == 1:
        _tg("sendPhoto", {"chat_id": TELEGRAM_CHANNEL_ID, "photo": images[0], "caption": caption, "parse_mode": "HTML"})
    else:
        media = [{"type": "photo", "media": u} for u in images]
        media[0]["caption"] = caption
        media[0]["parse_mode"] = "HTML"
        _tg("sendMediaGroup", {"chat_id": TELEGRAM_CHANNEL_ID, "media": media})

# ── Command Listener ─────────────────────────────────────────────────────────
async def run_telegram_listener():
    global FORCE_CHECK
    # Force drop old webhooks to help with Replit/Render conflicts
    _tg("deleteWebhook", {"drop_pending_updates": True})
    
    last_update_id = 0
    logger.info("Telegram command listener started")
    while True:
        try:
            updates = _tg("getUpdates", {"offset": last_update_id + 1, "timeout": 20})
            
            if updates and updates.get("ok"):
                for update in updates.get("result", []):
                    last_update_id = update["update_id"]
                    msg = update.get("message")
                    if not msg or "text" not in msg: continue
                    
                    text = msg["text"].strip().lower()
                    
                    # Command 1: /latest or /list
                    if text == "/latest" or text == "/list":
                        FORCE_CHECK = True
                        _tg("sendMessage", {
                            "chat_id": msg["chat"]["id"], 
                            "text": "⚡ <b>Forcing immediate check on Twitter for new spoilers...</b>", 
                            "parse_mode": "HTML"
                        })
                        
                        recent = list(posted_col.find().sort("_id", -1).limit(5))
                        if not recent:
                            resp = "❌ No spoilers recorded in database yet."
                        else:
                            resp = "<b>📜 Last 5 Recorded Spoilers:</b>\n\n" + "\n".join([f"@{t['username']}: {t['text'][:60]}..." for t in recent])
                        _tg("sendMessage", {"chat_id": msg["chat"]["id"], "text": resp, "parse_mode": "HTML"})
                    
                    # Command 2: /repost [hashtag] or just /repost
                    elif text.startswith("/repost"):
                        parts = text.split(maxsplit=1)
                        
                        if len(parts) > 1:
                            # They typed something like: /repost #onepiece1180
                            target_tag = parts[1].strip()
                            
                            # Tell MongoDB to only delete tweets containing this exact tag
                            result = posted_col.delete_many({"text": {"$regex": target_tag, "$options": "i"}})
                            
                            if result.deleted_count > 0:
                                msg_text = f"🗑️ <b>Deleted {result.deleted_count} tweet(s) matching {html.escape(target_tag)}!</b>\nChecking Twitter now to repost..."
                            else:
                                msg_text = f"⚠️ <b>No tweets found in memory matching {html.escape(target_tag)}.</b>\nChecking Twitter anyway just in case..."
                        else:
                            # They just typed: /repost (Wipe everything)
                            posted_col.delete_many({}) 
                            msg_text = "🗑️ <b>All Memory Wiped!</b>\nThe bot forgot all past tweets and is checking Twitter right now."
                        
                        FORCE_CHECK = True         
                        _tg("sendMessage", {
                            "chat_id": msg["chat"]["id"], 
                            "text": msg_text, 
                            "parse_mode": "HTML"
                        })
                        
        except Exception: 
            pass
        await asyncio.sleep(2)
        
# ── Memory Optimization Route ─────────────────────────────────────────────────
async def block_heavy_resources(route: Route):
    if route.request.resource_type in ["image", "media", "font", "stylesheet"]:
        await route.abort()
    else:
        await route.continue_()

# ── Scraper Logic ─────────────────────────────────────────────────────────────
async def scrape_account(page: Page, username: str) -> list[dict]:
    results = []
    try:
        await page.goto(f"https://x.com/{username}", wait_until="domcontentloaded", timeout=60_000)
        await page.wait_for_timeout(4000)
        
        title = await page.title()
        logger.info("Page title for @%s: %s", username, title)
        
        if not title or title == "X":
            logger.warning("X served a blank page. Bot check or slow load for @%s", username)
            return results

        await page.wait_for_selector('article[data-testid="tweet"]', timeout=20_000)
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
                clean_src = re.sub(r'\?.*$', '', src)
                images.append(f"{clean_src}?format=jpg&name=large")

            results.append({"tweet_id": tweet_id, "text": text, "images": images, "username": username})
    except Exception as exc:
        logger.error("Scrape failed for @%s: %s", username, exc)
    return results

async def run_scraper() -> None:
    global FORCE_CHECK
    logger.info("Scraper started — polling every %ds", POLL_INTERVAL)
    async with async_playwright() as pw:
        browser_path = os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "")
        executable = f"{browser_path}/chromium-1117/chrome-linux/chrome" if browser_path else None
        
        launch_kwargs = {
            "headless": True, 
            "args": [
                "--no-sandbox", 
                "--disable-gpu", 
                "--disable-dev-shm-usage", 
                "--single-process", 
                "--no-zygote", 
                "--disable-blink-features=AutomationControlled"
            ]
        }
        if executable and os.path.exists(executable):
            launch_kwargs["executable_path"] = executable

        browser = await pw.chromium.launch(**launch_kwargs)

        while True:
            try:
                context = await browser.new_context(user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
                await context.add_cookies([
                    {"name": "auth_token", "value": TWITTER_AUTH_TOKEN, "domain": ".x.com", "path": "/", "secure": True},
                    {"name": "ct0", "value": TWITTER_CT0, "domain": ".x.com", "path": "/", "secure": True},
                ])
                
                page = await context.new_page()
                await page.route("**/*", block_heavy_resources)
                
                for username in ACCOUNTS:
                    tweets = await scrape_account(page, username)
                    for tweet in reversed(tweets):
                        if not posted_col.find_one({"tweet_id": tweet["tweet_id"]}):
                            logger.info("🎯 Found new tweet %s! Sending to Telegram...", tweet['tweet_id'])
                            send_to_telegram(tweet["username"], tweet["text"], tweet["images"])
                            posted_col.insert_one({"tweet_id": tweet["tweet_id"], "text": tweet["text"], "username": tweet["username"]})
                
                await context.close() 
                
            except Exception as e:
                logger.error("Cycle error: %s", e)
                
            # Smart waiting: Break the sleep immediately if /latest command is used
            for _ in range(POLL_INTERVAL):
                if FORCE_CHECK:
                    logger.info("⚡ /latest command received! Skipping wait timer...")
                    FORCE_CHECK = False
                    break
                await asyncio.sleep(1)

if __name__ == "__main__":
    threading.Thread(target=start_http_server, daemon=True).start()
    
    # Run both the scraper and the Telegram listener together
    loop = asyncio.get_event_loop()
    loop.create_task(run_telegram_listener())
    loop.run_until_complete(run_scraper())
