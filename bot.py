import os
import time
import logging
import requests
import threading
import re
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

TWITTER_USERNAMES = ["pewpiece", "REIGEN326781"]
POLL_INTERVAL = 30  # Testing mode

# --- MONGODB SETUP ---
try:
    mongo_client = MongoClient(MONGO_URI)
    db = mongo_client["spoiler_bot_db"]
    posted_tweets = db["posted_tweets"]
    # Ensure MongoDB is actually connected
    mongo_client.admin.command('ping')
    logger.info("Successfully connected to MongoDB!")
except Exception as e:
    logger.error(f"MongoDB Connection Error: {e}")
    raise e

# --- DUMMY SERVER FOR RENDER ---
app = Flask(__name__)

@app.route('/')
def health_check():
    return "Bot is alive, connected to MongoDB, and scraping!", 200

def run_flask():
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)

# --- TELEGRAM LOGIC ---
def telegram_request(method: str, payload: dict) -> dict:
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/{method}"
    resp = requests.post(url, json=payload, timeout=30)
    data = resp.json()
    if not data.get("ok"):
        logger.warning(f"Telegram {method} failed: {data}")
    return data

def send_tweet_to_telegram(username: str, tweet_text: str, image_urls: list[str]):
    caption = f"🏴‍☠️ <b>One Piece Spoiler from @{username}</b>\n\n{tweet_text}"
    if not image_urls:
        telegram_request("sendMessage", {"chat_id": TELEGRAM_CHANNEL_ID, "text": caption, "parse_mode": "HTML"})
    elif len(image_urls) == 1:
        telegram_request("sendPhoto", {"chat_id": TELEGRAM_CHANNEL_ID, "photo": image_urls[0], "caption": caption, "parse_mode": "HTML"})
    else:
        media = []
        for i, url in enumerate(image_urls[:10]): # Telegram max is 10 images per group
            item = {"type": "photo", "media": url}
            if i == 0:
                item["caption"] = caption
                item["parse_mode"] = "HTML"
            media.append(item)
        telegram_request("sendMediaGroup", {"chat_id": TELEGRAM_CHANNEL_ID, "media": media})

# --- PLAYWRIGHT SCRAPER LOGIC ---
def scrape_twitter(username: str, page):
    url = f"https://x.com/{username}"
    logger.info(f"Navigating to {url}")
    
    try:
        page.goto(url, timeout=60000)
        # Wait for the timeline tweets to load in the DOM
        page.wait_for_selector('[data-testid="tweet"]', timeout=15000)
        
        tweets_data = []
        # Grab all tweet elements on the page
        tweet_elements = page.query_selector_all('[data-testid="tweet"]')
        
        # Scrape the first 5 tweets
        for element in tweet_elements[:5]:
            # Extract text
            text_el = element.query_selector('[data-testid="tweetText"]')
            text = text_el.inner_text() if text_el else ""
            
            # Filter: ONLY keep tweets that contain "#ONEPIECE" (case-insensitive)
            # This will match #ONEPIECE, #OnePiece1179, #onepiece, etc.
            # \d+ means "one or more digits". This perfectly matches #ONEPIECE1180, #ONEPIECE1181, etc.
            if not re.search(r'#ONEPIECE\d+', text, re.IGNORECASE):
                continue
                
            # Extract images
            img_els = element.query_selector_all('[data-testid="tweetPhoto"] img')
            images = [img.get_attribute('src') for img in img_els if img.get_attribute('src')]
            
            # Clean up image URLs to get high res
            high_res_images = [img.replace('&name=small', '&name=large') for img in images]
            
            tweets_data.append({'text': text, 'images': high_res_images})
            
        return tweets_data
        
    except Exception as e:
        logger.error(f"Failed to scrape {username}: {e}")
        return []

def run_bot():
    logger.info("Starting Playwright scraper bot with MongoDB...")
    
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage", 
                "--disable-gpu",
            ]
        )
        
        context = browser.new_context(
            viewport={'width': 1280, 'height': 720},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        page = context.new_page()

        # Main polling loop
        while True:
            for username in TWITTER_USERNAMES:
                logger.info(f"Checking @{username}...")
                current_tweets = scrape_twitter(username, page)
                
                if current_tweets:
                    # Reverse so we process oldest-first
                    for tweet in reversed(current_tweets):
                        tweet_text = tweet['text']
                        
                        # Check MongoDB to see if we already posted this exact text
                        existing_post = posted_tweets.find_one({"text": tweet_text})
                        
                        if not existing_post:
                            logger.info(f"Found brand new #ONEPIECE spoiler from @{username}!")
                            
                            # 1. Send it to Telegram
                            send_tweet_to_telegram(username, tweet_text, tweet['images'])
                            
                            # 2. Save it to MongoDB so we never post it again
                            posted_tweets.insert_one({
                                "text": tweet_text,
                                "username": username,
                                "timestamp": time.time()
                            })
                            
                            time.sleep(1) # Gap between telegram posts
                        else:
                            logger.info(f"Spoiler already in database, skipping...")
                else:
                    logger.info(f"No #ONEPIECE tweets found for @{username}.")
                
                # Small delay before checking the next user
                time.sleep(5)
                
            logger.info(f"Cycle finished. Sleeping for {POLL_INTERVAL} seconds.")
            time.sleep(POLL_INTERVAL)

if __name__ == "__main__":
    threading.Thread(target=run_bot, daemon=True).start()
    run_flask()
