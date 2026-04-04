import os
import time
import logging
import requests
import threading
import re
import urllib.parse # Added to handle special characters in passwords
from flask import Flask
from pymongo import MongoClient
from playwright.sync_api import sync_playwright

# Logging setup
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

# Environment Variables
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHANNEL_ID = os.environ["TELEGRAM_CHANNEL_ID"]
MONGO_URI = os.environ["MONGO_URI"]

# Automatically fix the URI if the user forgot to replace the placeholder
# or has special characters in their password
if "<db_password>" in MONGO_URI:
    logger.error("CRITICAL: You forgot to replace <db_password> in your Render Environment Variables!")

TWITTER_USERNAMES = ["pewpiece", "REIGEN326781"]
POLL_INTERVAL = 30  

# --- MONGODB SETUP ---
try:
    # This connects using your full string from Render
    mongo_client = MongoClient(MONGO_URI)
    db = mongo_client["spoiler_bot_db"]
    posted_tweets = db["posted_tweets"]
    mongo_client.admin.command('ping')
    logger.info("Successfully connected to MongoDB!")
except Exception as e:
    logger.error(f"MongoDB Connection Error: {e}")
    raise e

# --- DUMMY SERVER ---
app = Flask(__name__)

@app.route('/')
def health_check():
    return "Bot is alive and scraping!", 200

def run_flask():
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port)

# --- TELEGRAM LOGIC ---
def telegram_request(method: str, payload: dict) -> dict:
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/{method}"
    resp = requests.post(url, json=payload, timeout=30)
    return resp.json()

def send_tweet_to_telegram(username: str, tweet_text: str, image_urls: list[str]):
    caption = f"🏴‍☠️ <b>One Piece Spoiler from @{username}</b>\n\n{tweet_text}"
    if not image_urls:
        telegram_request("sendMessage", {"chat_id": TELEGRAM_CHANNEL_ID, "text": caption, "parse_mode": "HTML"})
    elif len(image_urls) == 1:
        telegram_request("sendPhoto", {"chat_id": TELEGRAM_CHANNEL_ID, "photo": image_urls[0], "caption": caption, "parse_mode": "HTML"})
    else:
        media = []
        for i, url in enumerate(image_urls[:10]):
            item = {"type": "photo", "media": url}
            if i == 0:
                item["caption"] = caption
                item["parse_mode"] = "HTML"
            media.append(item)
        telegram_request("sendMediaGroup", {"chat_id": TELEGRAM_CHANNEL_ID, "media": media})

# --- SCRAPER LOGIC ---
def scrape_twitter(username: str, page):
    url = f"https://x.com/{username}"
    try:
        page.goto(url, timeout=60000)
        page.wait_for_selector('[data-testid="tweet"]', timeout=15000)
        
        tweets_data = []
        tweet_elements = page.query_selector_all('[data-testid="tweet"]')
        
        for element in tweet_elements[:5]:
            text_el = element.query_selector('[data-testid="tweetText"]')
            text = text_el.inner_text() if text_el else ""
            
            # Sequence check: matches #ONEPIECE followed by any numbers
            if not re.search(r'#ONEPIECE\d+', text, re.IGNORECASE):
                continue
                
            img_els = element.query_selector_all('[data-testid="tweetPhoto"] img')
            images = [img.get_attribute('src').replace('&name=small', '&name=large') for img in img_els if img.get_attribute('src')]
            
            tweets_data.append({'text': text, 'images': images})
        return tweets_data
    except Exception as e:
        logger.error(f"Scrape error for {username}: {e}")
        return []

def run_bot():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-gpu"])
        context = browser.new_context(user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
        page = context.new_page()

        while True:
            for username in TWITTER_USERNAMES:
                current_tweets = scrape_twitter(username, page)
                for tweet in reversed(current_tweets):
                    if not posted_tweets.find_one({"text": tweet['text']}):
                        send_tweet_to_telegram(username, tweet['text'], tweet['images'])
                        posted_tweets.insert_one({"text": tweet['text'], "username": username, "ts": time.time()})
                time.sleep(5)
            time.sleep(POLL_INTERVAL)

if __name__ == "__main__":
    threading.Thread(target=run_bot, daemon=True).start()
    run_flask()
