import os
import time
import logging
import requests
import tweepy
import threading
from flask import Flask

# Logging setup
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

# Environment Variables
TWITTER_BEARER_TOKEN = os.environ["TWITTER_BEARER_TOKEN"]
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHANNEL_ID = os.environ["TELEGRAM_CHANNEL_ID"]

TWITTER_USERNAME = "pewpiece"
POLL_INTERVAL = 900  # 15 minutes

client = tweepy.Client(bearer_token=TWITTER_BEARER_TOKEN, wait_on_rate_limit=True)

# --- DUMMY SERVER FOR RENDER ---
app = Flask(__name__)

@app.route('/')
def health_check():
    return "Bot is alive and polling!", 200

def run_flask():
    # Render provides a PORT environment variable
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)

# --- BOT LOGIC ---
def get_user_id(username: str) -> str:
    logger.info(f"Looking up user ID for @{username}")
    response = client.get_user(username=username)
    if not response.data:
        raise RuntimeError(f"Could not find Twitter user @{username}")
    user_id = response.data.id
    logger.info(f"Found user ID: {user_id}")
    return user_id

def fetch_new_tweets(user_id: str, since_id: str | None):
    kwargs = {
        "max_results": 5,
        "tweet_fields": ["created_at", "attachments", "text"],
        "expansions": ["attachments.media_keys"],
        "media_fields": ["url", "preview_image_url", "type"],
    }
    if since_id:
        kwargs["since_id"] = since_id
    return client.get_users_tweets(user_id, **kwargs)

def telegram_request(method: str, payload: dict) -> dict:
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/{method}"
    resp = requests.post(url, json=payload, timeout=30)
    data = resp.json()
    if not data.get("ok"):
        logger.warning(f"Telegram {method} failed: {data}")
    return data

def send_tweet_to_telegram(tweet_text: str, image_urls: list[str]):
    caption = f"🏴‍☠️ <b>One Piece Spoiler from @pewpiece</b>\n\n{tweet_text}"
    if not image_urls:
        telegram_request("sendMessage", {"chat_id": TELEGRAM_CHANNEL_ID, "text": caption, "parse_mode": "HTML"})
    elif len(image_urls) == 1:
        telegram_request("sendPhoto", {"chat_id": TELEGRAM_CHANNEL_ID, "photo": image_urls[0], "caption": caption, "parse_mode": "HTML"})
    else:
        media = []
        for i, url in enumerate(image_urls):
            item = {"type": "photo", "media": url}
            if i == 0:
                item["caption"] = caption
                item["parse_mode"] = "HTML"
            media.append(item)
        telegram_request("sendMediaGroup", {"chat_id": TELEGRAM_CHANNEL_ID, "media": media})

def extract_image_urls(response) -> dict[str, list[str]]:
    media_map = {}
    tweet_media = {}
    if not response.includes or "media" not in response.includes:
        return tweet_media
    for media in response.includes["media"]:
        url = media.get("url") or media.get("preview_image_url")
        if url and media.get("type") in ("photo", "animated_gif"):
            media_map[media["media_key"]] = url
    if response.data:
        for tweet in response.data:
            attachments = getattr(tweet, "attachments", None) or {}
            keys = attachments.get("media_keys", []) if isinstance(attachments, dict) else []
            urls = [media_map[k] for k in keys if k in media_map]
            if urls:
                tweet_media[str(tweet.id)] = urls
    return tweet_media

def run_bot():
    logger.info("Starting One Piece spoiler bot polling...")
    user_id = get_user_id(TWITTER_USERNAME)
    last_tweet_id = None

    try:
        initial = fetch_new_tweets(user_id, since_id=None)
        if initial.data:
            last_tweet_id = str(initial.data[0].id)
            logger.info(f"Starting from tweet ID: {last_tweet_id}")
    except Exception as e:
        logger.error(f"Initial fetch error: {e}")

    while True:
        try:
            response = fetch_new_tweets(user_id, since_id=last_tweet_id)
            if response.data:
                image_url_map = extract_image_urls(response)
                for tweet in reversed(response.data):
                    send_tweet_to_telegram(tweet.text, image_url_map.get(str(tweet.id), []))
                    time.sleep(1)
                last_tweet_id = str(response.data[0].id)
            else:
                logger.info("No new tweets.")
        except tweepy.errors.TooManyRequests:
            time.sleep(POLL_INTERVAL)
        except Exception as e:
            logger.error(f"Poll error: {e}")
        
        time.sleep(POLL_INTERVAL)

if __name__ == "__main__":
    # Start bot in background
    threading.Thread(target=run_bot, daemon=True).start()
    # Start Flask server in foreground
    run_flask()
