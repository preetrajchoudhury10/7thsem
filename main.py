import asyncio
import time
import aiohttp
import pytz
import urllib.parse
import os
import sys
from datetime import datetime
from collections import deque
from playwright.async_api import async_playwright

# --- CONFIGURATION START ---

TARGET_REG_NO = "22156148001"

# Automatically pulls from Railway Environment Variables
BOT_TOKEN = os.environ.get("BOT_TOKEN")
CHAT_ID = os.environ.get("CHAT_ID")

EXAM_CONFIG = {
    "ordinal_sem": "7th",      
    "roman_sem": "VII",         
    "session": "2025",         
    "held_month": "February",  
    "held_year": "2026"
}

TARGET_SUBJECT = {
    "Student Name": "SHANTANU KUMAR"
}

CHECK_INTERVAL = 180      # Check result every 10 minutes
POLL_INTERVAL = 2         # Check for commands every 2 seconds
NOTIFY_INTERVAL = 21600    # 6 Hour Status Report

# --- CONFIGURATION END ---

class ResultRepairMonitor:
    def __init__(self):
        self.ist_timezone = pytz.timezone('Asia/Kolkata')
        self.last_notify_time = time.time()
        self.last_update_id = 0
        self.stop_signal = False
        self.browser_lock = asyncio.Lock()
        
        # A memory queue that stores the last 15 log messages
        self.log_history = deque(maxlen=15)

    def get_indian_time(self) -> str:
        utc_now = datetime.now(pytz.utc)
        ist_now = utc_now.astimezone(self.ist_timezone)
        return ist_now.strftime("%d-%m-%Y %I:%M:%S %p")

    # CUSTOM LOGGER: Optimized for Railway Console
    def log(self, message: str):
        # Extracts HH:MM:SS PM for cleaner railway logs
        time_parts = self.get_indian_time().split(" ")[1:] 
        timestamp = " ".join(time_parts)
        log_entry = f"[{timestamp}] {message}"
        
        # Save to internal memory for the /logs command
        self.log_history.append(log_entry)
        
        # Print to console (and force flush so Railway sees it immediately)
        print(log_entry)
        sys.stdout.flush()

    def construct_url(self):
        name_param = f"B.Tech. {EXAM_CONFIG['ordinal_sem']} Semester Examination, {EXAM_CONFIG['session']}"
        held_param = f"{EXAM_CONFIG['held_month']}/{EXAM_CONFIG['held_year']}"
        params = {
            'name': name_param,
            'semester': EXAM_CONFIG['roman_sem'],
            'session': EXAM_CONFIG['session'],
            'regNo': TARGET_REG_NO,
            'exam_held': held_param
        }
        return f"https://beu-bih.ac.in/result-three?{urllib.parse.urlencode(params)}"

    async def send_telegram_message(self, text: str):
        if not BOT_TOKEN or not CHAT_ID: 
            self.log("⚠️ Telegram Token/ChatID missing from ENV variables!")
            return
            
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
        payload = {"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML"}
        try:
            async with aiohttp.ClientSession() as session:
                await session.post(url, json=payload)
        except Exception as e:
            self.log(f"Telegram Message Error: {e}")

    async def send_telegram_photo(self, photo_bytes, caption):
        if not BOT_TOKEN or not CHAT_ID: return False

        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto"
        
        # Timeout added to prevent network dropping large images
        timeout = aiohttp.ClientTimeout(total=60) 

        # Retry logic for WinError 64 / Network drops
        for attempt in range(1, 4):
            try:
                data = aiohttp.FormData()
                data.add_field('chat_id', CHAT_ID)
                data.add_field('photo', photo_bytes, filename="result.png", content_type="image/png")
                data.add_field('caption', caption)
                data.add_field('parse_mode', 'HTML')

                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.post(url, data=data) as resp:
                        if resp.status != 200:
                            err = await resp.text()
                            self.log(f"Telegram Rejected Image: {err}")
                            return False
                        return True
                        
            except Exception as e:
                self.log(f"Photo Upload Error (Attempt {attempt}/3): {e}")
                if attempt < 3:
                    await asyncio.sleep(2) # Wait 2 seconds before retry

        self.log("Photo Upload Failed after 3 attempts.")
        return False

    async def get_page_data_and_screenshot(self, url, custom_scrape=False):
        async with self.browser_lock:
            try:
                self.log(f"Launching browser for: {url[:30]}...")
                async with async_playwright() as p:
                    browser = await p.chromium.launch(
                        headless=True,
                        args=['--no-sandbox', '--disable-dev-shm-usage']
                    )
                    try:
                        page = await browser.new_page()
                        self.log("Navigating to URL...")
                        await page.goto(url, timeout=45000)
                        
                        if not custom_scrape:
                            try:
                                await page.wait_for_selector(f"text={TARGET_REG_NO}", timeout=10000)
                            except:
                                self.log("RegNo not found immediately, taking screenshot anyway.")
                        else:
                            # For custom URLs via /url command
                            await page.wait_for_load_state("networkidle", timeout=10000)
                        
                        self.log("Extracting text and taking screenshot...")
                        text_content = await page.inner_text("body")
                        screenshot = await page.screenshot(full_page=True)
                        return text_content, screenshot
                        
                    finally:
                        await browser.close()
                        self.log("Browser closed successfully.")
            except Exception as e:
                self.log(f"CRITICAL Playwright Error: {repr(e)}")
                return None, None

    async def listen_for_commands(self):
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates"
        self.log("Bot Listener Started...")
        
        while not self.stop_signal:
            try:
                async with aiohttp.ClientSession() as session:
                    params = {"offset": self.last_update_id + 1, "timeout": 10}
                    async with session.get(url, params=params) as resp:
                        data = await resp.json()
                        
                        if data.get("ok") and data.get("result"):
                            for update in data["result"]:
                                self.last_update_id = update["update_id"]
                                message = update.get("message", {})
                                text = message.get("text", "").strip()
                                
                                # --- COMMAND: /ping ---
                                if text == "/ping":
                                    self.log("/ping command received")
                                    await self.send_telegram_message("🏓 <b>Pong!</b>\nFetching current result screenshot. Please wait...")
                                    
                                    target_url = self.construct_url()
                                    _, screenshot = await self.get_page_data_and_screenshot(target_url)
                                    
                                    if screenshot:
                                        self.log("Screenshot taken, sending to Telegram...")
                                        caption = f"🟢 <b>Monitor is Active</b>\nTarget: {TARGET_REG_NO}\nTime: {self.get_indian_time()}"
                                        success = await self.send_telegram_photo(screenshot, caption)
                                        if success:
                                            self.log("Screenshot sent successfully!")
                                        else:
                                            await self.send_telegram_message("⚠️ <b>Error:</b> Screenshot taken, but Telegram rejected it. Check /logs.")
                                    else:
                                        self.log("Screenshot failed to generate.")
                                        await self.send_telegram_message("⚠️ <b>Error:</b> Failed to take screenshot. Use /logs to see why.")

                                # --- COMMAND: /logs ---
                                elif text == "/logs":
                                    self.log("/logs command received")
                                    if not self.log_history:
                                        await self.send_telegram_message("📜 No logs available yet.")
                                    else:
                                        log_text = "\n".join(self.log_history).replace('<', '&lt;').replace('>', '&gt;')
                                        await self.send_telegram_message(f"📜 <b>Recent Internal Logs:</b>\n<pre>{log_text}</pre>")

                                # --- COMMAND: /stop ---
                                elif text == "/stop":
                                    self.log("/stop command received")
                                    await self.send_telegram_message("🛑 Stopping monitor...")
                                    self.stop_signal = True
                                    return
                                    
                                # --- COMMAND: /url ---
                                elif text.startswith("/url"):
                                    parts = text.split(" ", 1)
                                    if len(parts) > 1:
                                        custom_link = parts[1].strip()
                                        self.log(f"/url command: Scraping custom link -> {custom_link}")
                                        await self.send_telegram_message(f"🌐 <b>Navigating to custom URL...</b>\nPlease wait up to 30 seconds.")
                                        
                                        _, screenshot = await self.get_page_data_and_screenshot(custom_link, custom_scrape=True)
                                        
                                        if screenshot:
                                            await self.send_telegram_photo(screenshot, f"📸 <b>Custom Scrape Complete</b>\nURL: {custom_link}")
                                        else:
                                            await self.send_telegram_message("❌ <b>Error:</b> Failed to load or scrape that specific URL.")
                                    else:
                                        self.log("/url command: Showing monitored link")
                                        current_url = self.construct_url()
                                        await self.send_telegram_message(f"🔗 <b>Current Monitored Result Link:</b>\n<a href='{current_url}'>Click here to open in browser</a>")

            except Exception as e:
                self.log(f"Polling Error: {e}")
                await asyncio.sleep(5) 
            
            await asyncio.sleep(POLL_INTERVAL)

    async def check_for_correction(self):
        url = self.construct_url()
        text_content, screenshot = await self.get_page_data_and_screenshot(url)
        
        if not text_content:
            return ("ERROR", None)

        student_name = TARGET_SUBJECT.get("Student Name", "").upper()
        reg_no = str(TARGET_REG_NO)
        content_upper = text_content.upper()

        if reg_no not in content_upper or student_name not in content_upper:
            return ("NOT_FOUND", None)

        if "PASS" in content_upper and "FAIL" not in content_upper:
            return ("FIXED", screenshot)

        if "FAIL" in content_upper:
            return ("STILL_BROKEN", None)
        
        return ("UNCERTAIN", None)

    async def monitor_loop(self):
        self.log(f"Monitor Started for {TARGET_REG_NO}")
        self.last_notify_time = time.time()

        while not self.stop_signal:
            self.log("Running scheduled background check...")
            status, evidence = await self.check_for_correction()
            current_time = time.time()

            if status == "FIXED":
                self.log("CORRECTION DETECTED!")
                msg = (
                    f"✅ <b>RESULT UPDATED!</b>\n\n"
                    f"Time: {self.get_indian_time()}\n"
                    f"Status: PASS detected for {TARGET_SUBJECT.get('Student Name')}!"
                )
                if evidence:
                    success = await self.send_telegram_photo(evidence, msg)
                    if not success: await self.send_telegram_message(msg)
                else:
                    await self.send_telegram_message(msg)
                
                self.stop_signal = True 
                return

            if current_time - self.last_notify_time > NOTIFY_INTERVAL:
                self.log("Sending status report.")
                await self.send_telegram_message(
                    f"ℹ️ <b>Status Report:</b>\nMonitor is running cleanly. Result not yet updated.\nLast check status: {status}\n{self.get_indian_time()}"
                )
                self.last_notify_time = current_time 

            await asyncio.sleep(CHECK_INTERVAL)

    async def run(self):
        if "ENTER_YOUR" in TARGET_REG_NO:
            print("❌ ERROR: Set TARGET_REG_NO in main.py!")
            return

        if not BOT_TOKEN or not CHAT_ID:
            print("❌ ERROR: BOT_TOKEN or CHAT_ID environment variables are missing! Railway needs these set.")
            return

        await self.send_telegram_message(
            f"🕵️ <b>Monitor Started (Cloud Enabled)</b>\nTarget: {TARGET_REG_NO}\nChecking every 10 mins.\nStatus update every 6 hours.\n\nCommands:\n/ping - Get current screenshot\n/logs - View bot logs\n/url - Show current URL\n/url [link] - Scrape any URL\n/stop - Stop monitoring"
        )
        
        await asyncio.gather(
            self.monitor_loop(),
            self.listen_for_commands()
        )

if __name__ == "__main__":
    asyncio.run(ResultRepairMonitor().run())
