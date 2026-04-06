import os
import re
import asyncio
import logging
import socket
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import requests
from pymongo import MongoClient
from twscrape import API as TwAPI

# ── Instance identification ───────────────────────────────────────────────────
INSTANCE_ID   = os.environ.get("RENDER_INSTANCE_ID", "local")
SERVICE_NAME  = os.environ.get("RENDER_SERVICE_NAME", "replit")
HOSTNAME      = socket.gethostname()

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format=f"%(asctime)s [{SERVICE_NAME}/{INSTANCE_ID}] [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN  = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHANNEL_ID = os.environ["TELEGRAM_CHANNEL_ID"]
MONGO_URI           = os.environ["MONGO_URI"]
TWITTER_AUTH_TOKEN  = os.environ["TWITTER_AUTH_TOKEN"]
TWITTER_CT0         = os.environ["TWITTER_CT0"]
TWITTER_USERNAME    = os.environ.get("TWITTER_USERNAME", "scraper_account")

ACCOUNTS      = ["pewpiece", "REIGEN326781"]
HASHTAG_RE    = re.compile(r"#ONEPIECE\d+", re.IGNORECASE)
POLL_INTERVAL = 60  # seconds — be gentle on the free tier

# ── MongoDB ───────────────────────────────────────────────────────────────────
mongo_client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=10000)
db           = mongo_client["spoiler_bot"]
posted_col   = db["posted_tweets"]
posted_col.create_index("tweet_id", unique=True)

# ── Keepalive HTTP server (no Flask / no greenlet) ────────────────────────────
class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = f"OK — {SERVICE_NAME}/{INSTANCE_ID}".encode()
        self.send_response(200)
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass  # suppress noise


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
        _tg("sendMessage", {
            "chat_id": TELEGRAM_CHANNEL_ID,
            "text": caption,
            "parse_mode": "HTML",
        })
    elif len(images) == 1:
        _tg("sendPhoto", {
            "chat_id": TELEGRAM_CHANNEL_ID,
            "photo": images[0],
            "caption": caption,
            "parse_mode": "HTML",
        })
    else:
        media = [{"type": "photo", "media": u} for u in images]
        media[0]["caption"]    = caption
        media[0]["parse_mode"] = "HTML"
        _tg("sendMediaGroup", {
            "chat_id": TELEGRAM_CHANNEL_ID,
            "media": media,
        })

# ── Twitter scraper (twscrape — no browser, ~20 MB RAM) ──────────────────────
async def build_tw_api() -> TwAPI:
    api = TwAPI("/tmp/twscrape.db")
    cookies = f"auth_token={TWITTER_AUTH_TOKEN}; ct0={TWITTER_CT0}"
    await api.pool.add_account(
        username=TWITTER_USERNAME,
        password="placeholder",
        email="placeholder@placeholder.com",
        email_password="placeholder",
        cookies=cookies,
    )
    logger.info("twscrape account pool ready")
    return api


async def scrape_account(api: TwAPI, username: str) -> list[dict]:
    results = []
    try:
        user = await api.user_by_login(username)
        if not user:
            logger.warning("User @%s not found", username)
            return results

        async for tweet in api.user_tweets(user.id, limit=20):
            text = tweet.rawContent or ""
            if not HASHTAG_RE.search(text):
                continue
            media  = tweet.media
            images = []
            if media:
                images += [p.url for p in media.photos if p.url]
                images += [v.thumbnailUrl for v in media.videos if getattr(v, "thumbnailUrl", None)]
            results.append({
                "tweet_id": str(tweet.id),
                "text":     text,
                "images":   images,
                "username": username,
            })
            logger.info("Matched tweet %s from @%s (%d image(s))",
                        tweet.id, username, len(images))

    except Exception as exc:
        logger.error("Error scraping @%s: %s", username, exc, exc_info=True)

    return results

# ── Main polling loop ─────────────────────────────────────────────────────────
async def run_scraper() -> None:
    logger.info(
        "Bot starting — hostname=%s  service=%s  instance=%s",
        HOSTNAME, SERVICE_NAME, INSTANCE_ID,
    )
    logger.info("Polling every %ds for accounts: %s", POLL_INTERVAL, ACCOUNTS)

    api = await build_tw_api()

    while True:
        try:
            for username in ACCOUNTS:
                tweets = await scrape_account(api, username)
                for tweet in tweets:
                    tid = tweet["tweet_id"]
                    if posted_col.find_one({"tweet_id": tid}):
                        logger.info("Already posted %s — skipping", tid)
                        continue
                    logger.info("Sending %s from @%s to Telegram", tid, tweet["username"])
                    send_to_telegram(tweet["username"], tweet["text"], tweet["images"])
                    posted_col.insert_one({
                        "tweet_id": tid,
                        "text":     tweet["text"],
                        "username": tweet["username"],
                    })
                    await asyncio.sleep(1)

        except Exception as exc:
            logger.error("Scraper cycle error: %s", exc, exc_info=True)

        logger.info("Sleeping %ds...", POLL_INTERVAL)
        await asyncio.sleep(POLL_INTERVAL)

# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    http_thread = threading.Thread(target=start_http_server, daemon=True)
    http_thread.start()
    asyncio.run(run_scraper())
