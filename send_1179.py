"""One-off script: find all #ONEPIECE1179 tweets from @pewpiece and send to Telegram."""
import os
import re
import asyncio
import logging

import requests
from twscrape import API as TwAPI

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

TELEGRAM_BOT_TOKEN  = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHANNEL_ID = os.environ["TELEGRAM_CHANNEL_ID"]
TWITTER_AUTH_TOKEN  = os.environ["TWITTER_AUTH_TOKEN"]
TWITTER_CT0         = os.environ["TWITTER_CT0"]
TWITTER_USERNAME    = os.environ.get("TWITTER_USERNAME", "scraper_account")

TARGET_HASHTAG = re.compile(r"#ONEPIECE1179", re.IGNORECASE)


def _tg(method, payload):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/{method}"
    resp = requests.post(url, json=payload, timeout=30)
    data = resp.json()
    if not data.get("ok"):
        logger.warning("Telegram %s failed: %s", method, data)
    return data


def send_to_telegram(text, images):
    caption = f"🏴‍☠️ <b>One Piece Spoiler #1179</b> — <i>@pewpiece</i>\n\n{text}"
    if not images:
        _tg("sendMessage", {"chat_id": TELEGRAM_CHANNEL_ID, "text": caption, "parse_mode": "HTML"})
    elif len(images) == 1:
        _tg("sendPhoto", {"chat_id": TELEGRAM_CHANNEL_ID, "photo": images[0], "caption": caption, "parse_mode": "HTML"})
    else:
        media = [{"type": "photo", "media": u} for u in images]
        media[0]["caption"]    = caption
        media[0]["parse_mode"] = "HTML"
        _tg("sendMediaGroup", {"chat_id": TELEGRAM_CHANNEL_ID, "media": media})


async def main():
    api = TwAPI("/tmp/twscrape_1179.db")
    cookies = f"auth_token={TWITTER_AUTH_TOKEN}; ct0={TWITTER_CT0}"
    await api.pool.add_account(
        username=TWITTER_USERNAME,
        password="placeholder",
        email="placeholder@placeholder.com",
        email_password="placeholder",
        cookies=cookies,
    )

    user = await api.user_by_login("pewpiece")
    if not user:
        logger.error("Could not find @pewpiece")
        return

    sent = 0
    async for tweet in api.user_tweets(user.id, limit=50):
        text = tweet.rawContent or ""
        if not TARGET_HASHTAG.search(text):
            continue

        images = []
        if tweet.media:
            images += [p.url for p in tweet.media.photos if p.url]

        logger.info("Sending: %s... (%d images)", text[:80], len(images))
        send_to_telegram(text, images)
        sent += 1
        await asyncio.sleep(1)

    if sent == 0:
        logger.warning("No #ONEPIECE1179 tweets found")
    else:
        logger.info("Done — sent %d tweet(s)", sent)


if __name__ == "__main__":
    asyncio.run(main())
