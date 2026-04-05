import os
import re
import asyncio
import logging
import html
import requests
from playwright.async_api import async_playwright

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHANNEL_ID = os.environ.get("TELEGRAM_CHANNEL_ID")
TWITTER_AUTH_TOKEN = os.environ.get("TWITTER_AUTH_TOKEN")
TWITTER_CT0 = os.environ.get("TWITTER_CT0")

TARGET_ACCOUNT = "REIGEN326781"
HASHTAG = re.compile(r"#ONEPIECE1180", re.IGNORECASE)

def _tg_test(method: str, payload: dict):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/{method}"
    resp = requests.post(url, json=payload, timeout=30)
    data = resp.json()
    
    if not data.get("ok"):
        # This will reveal the hidden Telegram error
        logger.error("❌ TELEGRAM REJECTED THE MESSAGE!")
        logger.error("Reason: %s", data.get("description"))
    else:
        logger.info("✅ Successfully sent to Telegram!")
    return data

def send_test_to_telegram(text: str, images: list):
    logger.info("Attempting to send to Telegram channel: %s", TELEGRAM_CHANNEL_ID)
    
    # HTML escape the text to prevent Telegram parsing crashes
    safe_text = html.escape(text)
    caption = f"🏴‍☠️ <b>Test Spoiler</b> — <i>@{TARGET_ACCOUNT}</i>\n\n{safe_text}"
    
    if not images:
        _tg_test("sendMessage", {"chat_id": TELEGRAM_CHANNEL_ID, "text": caption, "parse_mode": "HTML"})
    elif len(images) == 1:
        _tg_test("sendPhoto", {"chat_id": TELEGRAM_CHANNEL_ID, "photo": images[0], "caption": caption, "parse_mode": "HTML"})
    else:
        media = [{"type": "photo", "media": u} for u in images]
        media[0]["caption"] = caption
        media[0]["parse_mode"] = "HTML"
        _tg_test("sendMediaGroup", {"chat_id": TELEGRAM_CHANNEL_ID, "media": media})

async def run_test():
    logger.info("Starting test scraper for @%s looking for #ONEPIECE1180...", TARGET_ACCOUNT)
    
    async with async_playwright() as pw:
        # Use the browser path defined in your Render env vars
        browser_path = os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "")
        executable = f"{browser_path}/chromium-1117/chrome-linux/chrome" if browser_path else None
        
        launch_kwargs = {
            "headless": True, 
            "args": ["--no-sandbox", "--disable-gpu", "--disable-blink-features=AutomationControlled"]
        }
        if executable and os.path.exists(executable):
            launch_kwargs["executable_path"] = executable

        browser = await pw.chromium.launch(**launch_kwargs)
        context = await browser.new_context(user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
        
        await context.add_cookies([
            {"name": "auth_token", "value": TWITTER_AUTH_TOKEN, "domain": ".x.com", "path": "/", "secure": True},
            {"name": "ct0", "value": TWITTER_CT0, "domain": ".x.com", "path": "/", "secure": True},
        ])
        
        page = await context.new_page()
        
        try:
            logger.info("Loading profile...")
            await page.goto(f"https://x.com/{TARGET_ACCOUNT}", wait_until="domcontentloaded", timeout=60_000)
            await page.wait_for_timeout(4000)
            
            logger.info("Page Title: %s", await page.title())
            
            await page.wait_for_selector('article[data-testid="tweet"]', timeout=15_000)
            articles = await page.query_selector_all('article[data-testid="tweet"]')
            
            logger.info("Found %d tweets on page. Scanning for #ONEPIECE1180...", len(articles))
            
            found = False
            for article in articles:
                text_el = await article.query_selector('[data-testid="tweetText"]')
                text = await text_el.inner_text() if text_el else ""
                
                if HASHTAG.search(text):
                    found = True
                    logger.info("🎯 MATCH FOUND:\n%s", text[:100])
                    
                    images = []
                    for img in await article.query_selector_all('img[src*="pbs.twimg.com/media"]'):
                        src = await img.get_attribute("src") or ""
                        clean_src = re.sub(r'\?.*$', '', src)
                        images.append(f"{clean_src}?format=jpg&name=large")
                    
                    send_test_to_telegram(text, images)
                    break # Only process the first match for the test
            
            if not found:
                logger.warning("No tweets matching #ONEPIECE1180 were found in the recent timeline.")
                
        except Exception as e:
            logger.error("Test failed: %s", e)
            
        finally:
            await browser.close()

if __name__ == "__main__":
    asyncio.run(run_test())
