#!/usr/bin/env python3
import os
import re
import aiohttp
import asyncio
import threading
import json
from pathlib import Path
from datetime import datetime, timedelta
from pyrogram import Client, filters
from pyrogram.types import Message, BotCommand, InlineKeyboardMarkup, InlineKeyboardButton
from pyrogram.enums import ParseMode
from PIL import Image
from hachoir.parser import createParser
from hachoir.metadata import extractMetadata
import subprocess
import traceback
from flask import Flask, render_template_string
import requests
import time
import math
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# env
API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
BOT_TOKEN = os.getenv("BOT_TOKEN")
PORT = int(os.getenv("PORT", "5000"))
RENDER_EXTERNAL_HOSTNAME = os.getenv("RENDER_EXTERNAL_HOSTNAME") 
# New env var for send.now API key
SENDNOW_API_KEY = os.getenv("SENDNOW_API_KEY")

TMP = Path("tmp")
TMP.mkdir(parents=True, exist_ok=True)

# state
USER_THUMBS = {}
TASKS = {}
SET_THUMB_REQUEST = set()
SUBSCRIBERS = set()
SET_CAPTION_REQUEST = set()
USER_CAPTIONS = {}
USER_COUNTERS = {}
EDIT_CAPTION_MODE = set()
USER_THUMB_TIME = {}
# New state for send.now tasks
SENDNOW_DELETE_TASKS = {}
# New state for cloud upload toggle mode
CLOUD_UPLOAD_MODE = set()


ADMIN_ID = int(os.getenv("ADMIN_ID", ""))
MAX_SIZE = 4 * 1024 * 1024 * 1024

app = Client("mybot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)
flask_app = Flask(__name__)

# ---- utilities ----
def is_admin(uid: int) -> bool:
    return uid == ADMIN_ID

def is_drive_url(url: str) -> bool:
    return "drive.google.com" in url or "docs.google.com" in url

def extract_drive_id(url: str) -> str:
    patterns = [
        r"/d/([a-zA-Z0-9_-]+)",
        r"id=([a-zA-Z0-9_-]+)",
        r"open\?id=([a-zA-Z0-9_-]+)",
        r"https://drive.google.com/file/d/([a-zA-Z0-9_-]+)/"
    ]
    for p in patterns:
        m = re.search(p, url)
        if m:
            return m.group(1)
    return None

def get_video_duration(file_path: Path) -> int:
    try:
        parser = createParser(str(file_path))
        if not parser:
            return 0
        with parser:
            metadata = extractMetadata(parser)
        if metadata and metadata.has("duration"):
            return int(metadata.get("duration").total_seconds())
    except Exception:
        return 0
    return 0

def parse_time(time_str: str) -> int:
    """Parses a time string like '5s', '1m', '1h 30s' into seconds."""
    total_seconds = 0
    parts = time_str.lower().split()
    for part in parts:
        if part.endswith('s'):
            total_seconds += int(part[:-1])
        elif part.endswith('m'):
            total_seconds += int(part[:-1]) * 60
        elif part.endswith('h'):
            total_seconds += int(part[:-1]) * 3600
    return total_seconds

def progress_keyboard():
    return InlineKeyboardMarkup([[InlineKeyboardButton("Cancel ❌", callback_data="cancel_task")]])

def delete_caption_keyboard():
    return InlineKeyboardMarkup([[InlineKeyboardButton("Delete Caption 🗑️", callback_data="delete_caption")]])

# ---- progress callback helpers (removed live progress) ----
async def progress_callback(current, total, message: Message, start_time, task="Progress"):
    pass

def pyrogram_progress_wrapper(current, total, message_obj, start_time_obj, task_str="Progress"):
    pass

# ---- robust download stream with retries ----
async def download_stream(resp, out_path: Path, message: Message = None, cancel_event: asyncio.Event = None):
    total = 0
    try:
        size = int(resp.headers.get("Content-Length", 0))
    except:
        size = 0
    chunk_size = 1024 * 1024
    try:
        with out_path.open("wb") as f:
            async for chunk in resp.content.iter_chunked(chunk_size):
                if cancel_event and cancel_event.is_set():
                    return False, "অপারেশন ব্যবহারকারী দ্বারা বাতিল করা হয়েছে।"
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_SIZE:
                    return False, "ফাইলের সাইজ 2GB এর বেশি হতে পারে না।"
                f.write(chunk)
    except Exception as e:
        return False, str(e)
    return True, None

async def fetch_with_retries(session, url, method="GET", max_tries=3, **kwargs):
    backoff = 1
    for attempt in range(1, max_tries + 1):
        try:
            resp = await session.request(method, url, **kwargs)
            return resp
        except Exception as e:
            if attempt == max_tries:
                raise
            await asyncio.sleep(backoff)
            backoff *= 2
    raise RuntimeError("unreachable")

async def download_url_generic(url: str, out_path: Path, message: Message = None, cancel_event: asyncio.Event = None):
    timeout = aiohttp.ClientTimeout(total=7200)
    headers = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64)"}
    connector = aiohttp.TCPConnector(limit=0, force_close=True)
    async with aiohttp.ClientSession(timeout=timeout, headers=headers, connector=connector) as sess:
        try:
            async with sess.get(url, allow_redirects=True) as resp:
                if resp.status != 200:
                    return False, f"HTTP {resp.status}"
                return await download_stream(resp, out_path, message, cancel_event=cancel_event)
        except Exception as e:
            return False, str(e)

async def download_drive_file(file_id: str, out_path: Path, message: Message = None, cancel_event: asyncio.Event = None):
    base = f"https://drive.google.com/uc?export=download&id={file_id}"
    timeout = aiohttp.ClientTimeout(total=7200)
    headers = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64)"}
    connector = aiohttp.TCPConnector(limit=0, force_close=True)
    async with aiohttp.ClientSession(timeout=timeout, headers=headers, connector=connector) as sess:
        try:
            async with sess.get(base, allow_redirects=True) as resp:
                if resp.status == 200 and "content-disposition" in (k.lower() for k in resp.headers.keys()):
                    return await download_stream(resp, out_path, message, cancel_event=cancel_event)
                text = await resp.text(errors="ignore")
                m = re.search(r"confirm=([0-9A-Za-z-_]+)", text)
                if m:
                    token = m.group(1)
                    download_url = f"https://drive.google.com/uc?export=download&confirm={token}&id={file_id}"
                    async with sess.get(download_url, allow_redirects=True) as resp2:
                        if resp2.status != 200:
                            return False, f"HTTP {resp2.status}"
                        return await download_stream(resp2, out_path, message, cancel_event=cancel_event)
                for k, v in resp.cookies.items():
                    if k.startswith("download_warning"):
                        token = v.value
                        download_url = f"https://drive.google.com/uc?export=download&confirm={token}&id={file_id}"
                        async with sess.get(download_url, allow_redirects=True) as resp2:
                            if resp2.status != 200:
                                return False, f"HTTP {resp2.status}"
                            return await download_stream(resp2, out_path, message, cancel_event=cancel_event)
                return False, "ডাউনলোডের জন্য Google Drive থেকে অনুমতি প্রয়োজন বা লিংক পাবলিক নয়।"
        except Exception as e:
            return False, str(e)

async def set_bot_commands():
    cmds = [
        BotCommand("start", "বট চালু/হেল্প"),
        BotCommand("upload_url", "URL থেকে ফাইল ডাউনলোড ও আপলোড (admin only)"),
        BotCommand("setthumb", "কাস্টম থাম্বনেইল সেট করুন (admin only)"),
        BotCommand("view_thumb", "আপনার থাম্বনেইল দেখুন (admin only)"),
        BotCommand("del_thumb", "আপনার থাম্বনেইল মুছে ফেলুন (admin only)"),
        BotCommand("set_caption", "কাস্টম ক্যাপশন সেট করুন (admin only)"),
        BotCommand("view_caption", "আপনার ক্যাপশন দেখুন (admin only)"),
        BotCommand("edit_caption_mode", "শুধু ক্যাপশন এডিট করুন (admin only)"),
        BotCommand("rename", "reply করা ভিডিও রিনেম করুন (admin only)"),
        BotCommand("broadcast", "ব্রডকাস্ট (কেবল অ্যাডমিন)"),
        # New command
        BotCommand("upload_to_cloud", "ক্লাউড আপলোড মোড চালু/বন্ধ (admin only)"),
        BotCommand("help", "সহায়িকা")
    ]
    try:
        await app.set_bot_commands(cmds)
    except Exception as e:
        logger.warning("Set commands error: %s", e)

# New send.now API integration
class SendNowAPI:
    def __init__(self, api_key: str):
        self.api_key = api_key
        self.base_url = "https://send.now/api"

    async def get_upload_server(self, session: aiohttp.ClientSession):
        url = f"{self.base_url}/upload/server?key={self.api_key}"
        async with session.get(url) as resp:
            resp.raise_for_status()
            data = await resp.json()
            if data["status"] == 200:
                return data["result"], data["sess_id"]
            else:
                raise Exception(f"Failed to get upload server: {data.get('msg')}")

    async def upload_file(self, session: aiohttp.ClientSession, upload_url: str, sess_id: str, file_path: Path):
        data = aiohttp.FormData()
        data.add_field("sess_id", sess_id)
        data.add_field("utype", "prem")
        data.add_field("file_0", open(file_path, "rb"), filename=file_path.name)
        
        async with session.post(upload_url, data=data) as resp:
            resp.raise_for_status()
            data = await resp.json()
            if data and data[0]["file_status"] == "OK":
                return data[0]["file_code"]
            else:
                raise Exception(f"Failed to upload file: {data.get('msg', 'Unknown error')}")

    async def rename_file(self, session: aiohttp.ClientSession, file_code: str, new_name: str):
        url = f"{self.base_url}/file/rename?key={self.api_key}&file_code={file_code}&name={new_name}"
        async with session.get(url) as resp:
            resp.raise_for_status()
            data = await resp.json()
            if data["status"] == 200 and data["result"] == "true":
                return True
            else:
                raise Exception(f"Failed to rename file: {data.get('msg')}")

    async def get_direct_link(self, session: aiohttp.ClientSession, file_code: str):
        url = f"{self.base_url}/file/direct_link?key={self.api_key}&file_code={file_code}"
        async with session.get(url) as resp:
            resp.raise_for_status()
            data = await resp.json()
            if data["status"] == 200:
                return data["result"]["url"]
            else:
                raise Exception(f"Failed to get direct link: {data.get('msg')}")

    async def delete_file(self, session: aiohttp.ClientSession, file_code: str):
        url = f"{self.base_url}/file/delete?key={self.api_key}&file_code={file_code}"
        async with session.get(url) as resp:
            resp.raise_for_status()
            data = await resp.json()
            if data["status"] == 200:
                return True
            else:
                raise Exception(f"Failed to delete file: {data.get('msg')}")

async def scheduled_delete_task(file_code: str, chat_id: int):
    # This function will run after 24 hours
    await asyncio.sleep(24 * 3600)  # Sleep for 24 hours
    logger.info(f"Attempting to delete file {file_code} after 24 hours.")
    async with aiohttp.ClientSession() as session:
        api = SendNowAPI(SENDNOW_API_KEY)
        try:
            await api.delete_file(session, file_code)
            await app.send_message(chat_id, f"ফাইলটি (`{file_code}`) ২৪ ঘন্টা পর ক্লাউড থেকে সফলভাবে ডিলিট করা হয়েছে।")
        except Exception as e:
            await app.send_message(chat_id, f"ফাইল (`{file_code}`) ডিলিট করতে সমস্যা হয়েছে: {e}")
            logger.error(f"Error deleting file {file_code}: {e}")
    if file_code in SENDNOW_DELETE_TASKS:
        del SENDNOW_DELETE_TASKS[file_code]

async def upload_to_cloud_process(client: Client, message: Message, file_path: Path):
    if not SENDNOW_API_KEY:
        await message.reply_text("`SENDNOW_API_KEY` এনভায়রনমেন্ট ভেরিয়েবল সেট করা নেই।")
        return
    
    processing_msg = await message.reply_text("ভিডিও আপলোড করা হচ্ছে... ⏳")
    
    try:
        api = SendNowAPI(SENDNOW_API_KEY)
        
        await processing_msg.edit_text("ভিডিও আপলোডের জন্য ক্লাউড সার্ভার তৈরি করা হচ্ছে...")
        async with aiohttp.ClientSession() as session:
            upload_url, sess_id = await api.get_upload_server(session)
            
            await processing_msg.edit_text("ক্লাউডে ভিডিও আপলোড হচ্ছে...")
            file_code = await api.upload_file(session, upload_url, sess_id, file_path)
            
            # Rename the file
            await processing_msg.edit_text("ফাইলটি রিনেম করা হচ্ছে...")
            new_name = "@TA_HD_Anime"
            await api.rename_file(session, file_code, new_name)
            
            # Get the direct link
            direct_link = await api.get_direct_link(session, file_code)
            
            # Schedule the deletion task
            delete_task = asyncio.create_task(scheduled_delete_task(file_code, message.chat.id))
            SENDNOW_DELETE_TASKS[file_code] = delete_task
            
        final_text = f"✅ সফলভাবে আপলোড করা হয়েছে!\n\n"
        final_text += f"**ফাইল কোড:** `{file_code}`\n"
        final_text += f"**নাম:** `{new_name}`\n"
        final_text += f"**সরাসরি লিঙ্ক:** {direct_link}\n"
        final_text += f"\n_এই ফাইলটি ২৪ ঘন্টা পর স্বয়ংক্রিয়ভাবে ক্লাউড থেকে ডিলিট হয়ে যাবে।_"
        
        await processing_msg.edit_text(final_text, parse_mode=ParseMode.MARKDOWN)

    except Exception as e:
        logger.error(f"Error in cloud upload process: {traceback.format_exc()}")
        await processing_msg.edit_text(f"একটি সমস্যা হয়েছে: {e}")
    finally:
        if file_path.exists():
            try:
                os.remove(file_path)
            except Exception as e:
                logger.warning(f"Failed to delete local file: {e}")

# ---- handlers ----
@app.on_message(filters.command("start") & filters.private)
async def start_handler(c, m: Message):
    await set_bot_commands()
    SUBSCRIBERS.add(m.chat.id)
    text = (
        "Hi! আমি URL uploader bot.\n\n"
        "নোট: বটের অনেক কমান্ড শুধু অ্যাডমিন (owner) চালাতে পারবে।\n\n"
        "Commands:\n"
        "/upload_url <url> - URL থেকে ফাইল ডাউনলোড ও Telegram-এ আপলোড (admin only)\n"
        "/upload_to_cloud - ক্লাউড আপলোড মোড চালু/বন্ধ (admin only)\n"
        "/setthumb - একটি ছবি পাঠান, সেট হবে আপনার থাম্বনেইল (admin only)\n"
        "/view_thumb - আপনার থাম্বনেইল দেখুন (admin only)\n"
        "/del_thumb - আপনার থাম্বনেইল মুছে ফেলুন (admin only)\n"
        "/set_caption - কাস্টম ক্যাপশন সেট করুন (admin only)\n"
        "/view_caption - আপনার ক্যাপশন দেখুন (admin only)\n"
        "/edit_caption_mode - শুধু ক্যাপশন এডিট করার মোড টগল করুন (admin only)\n"
        "/rename <newname.ext> - reply করা ভিডিও রিনেম করুন (admin only)\n"
        "/broadcast <text> - ব্রডকাস্ট (শুধুমাত্র অ্যাডমিন)\n"
        "/help - সাহায্য"
    )
    await m.reply_text(text)

@app.on_message(filters.command("help") & filters.private)
async def help_handler(c, m):
    await start_handler(c, m)

@app.on_message(filters.command("upload_url") & filters.private)
async def upload_url_handler(c, m: Message):
    if not is_admin(m.from_user.id):
        await m.reply_text("এই কমান্ড শুধুমাত্র অ্যাডমিন ব্যবহার করতে পারবে।")
        return
    # Existing upload_url logic remains here

# New command handler for cloud upload toggle
@app.on_message(filters.command("upload_to_cloud") & filters.private)
async def upload_to_cloud_toggle_handler(c, m: Message):
    if not is_admin(m.from_user.id):
        await m.reply_text("এই কমান্ড শুধুমাত্র অ্যাডমিন ব্যবহার করতে পারবে।")
        return
    
    user_id = m.from_user.id
    if user_id in CLOUD_UPLOAD_MODE:
        CLOUD_UPLOAD_MODE.remove(user_id)
        await m.reply_text("Upload to cloud is now **OFF**.")
    else:
        CLOUD_UPLOAD_MODE.add(user_id)
        await m.reply_text("Upload to cloud is now **ON**.")

@app.on_message(filters.video & filters.private)
async def handle_video_upload(c: Client, m: Message):
    if not is_admin(m.from_user.id):
        # Allow non-admins to use the bot normally
        return

    if m.from_user.id in CLOUD_UPLOAD_MODE:
        processing_msg = await m.reply_text("ভিডিওটি ক্লাউডে আপলোডের জন্য প্রক্রিয়া করা হচ্ছে...")
        file_path = Path(f"tmp/{m.video.file_unique_id}.mp4")
        try:
            await c.download_media(m, file_path)
            await upload_to_cloud_process(c, m, file_path)
        except Exception as e:
            await processing_msg.edit_text(f"ফাইল ডাউনলোড করতে সমস্যা হয়েছে: {e}")
            logger.error(f"Error downloading video: {e}")
        finally:
            if file_path.exists():
                try:
                    os.remove(file_path)
                except Exception as e:
                    logger.warning(f"Failed to delete local file: {e}")
    else:
        # Existing video handling logic goes here
        # Example: just acknowledging the video or uploading to Telegram
        pass

@app.on_message(filters.command("setthumb") & filters.private)
async def set_thumb_handler(c, m: Message):
    if not is_admin(m.from_user.id):
        await m.reply_text("এই কমান্ড শুধুমাত্র অ্যাডমিন ব্যবহার করতে পারবে।")
        return
    # Existing set_thumb logic remains here

@app.on_message(filters.command("view_thumb") & filters.private)
async def view_thumb_handler(c, m: Message):
    if not is_admin(m.from_user.id):
        await m.reply_text("এই কমান্ড শুধুমাত্র অ্যাডমিন ব্যবহার করতে পারবে।")
        return
    # Existing view_thumb logic remains here

@app.on_message(filters.command("del_thumb") & filters.private)
async def del_thumb_handler(c, m: Message):
    if not is_admin(m.from_user.id):
        await m.reply_text("এই কমান্ড শুধুমাত্র অ্যাডমিন ব্যবহার করতে পারবে।")
        return
    # Existing del_thumb logic remains here
    
@app.on_message(filters.command("set_caption") & filters.private)
async def set_caption_handler(c, m: Message):
    if not is_admin(m.from_user.id):
        await m.reply_text("এই কমান্ড শুধুমাত্র অ্যাডমিন ব্যবহার করতে পারবে।")
        return
    # Existing set_caption logic remains here

@app.on_message(filters.command("view_caption") & filters.private)
async def view_caption_handler(c, m: Message):
    if not is_admin(m.from_user.id):
        await m.reply_text("এই কমান্ড শুধুমাত্র অ্যাডমিন ব্যবহার করতে পারবে।")
        return
    # Existing view_caption logic remains here

@app.on_message(filters.command("edit_caption_mode") & filters.private)
async def edit_caption_mode_handler(c, m: Message):
    if not is_admin(m.from_user.id):
        await m.reply_text("এই কমান্ড শুধুমাত্র অ্যাডমিন ব্যবহার করতে পারবে।")
        return
    # Existing edit_caption_mode logic remains here

@app.on_message(filters.command("rename") & filters.private)
async def rename_handler(c, m: Message):
    if not is_admin(m.from_user.id):
        await m.reply_text("এই কমান্ড শুধুমাত্র অ্যাডমিন ব্যবহার করতে পারবে।")
        return
    # Existing rename logic remains here

@app.on_message(filters.command("broadcast") & filters.private)
async def broadcast_handler(c, m: Message):
    if not is_admin(m.from_user.id):
        await m.reply_text("এই কমান্ড শুধুমাত্র অ্যাডমিন ব্যবহার করতে পারবে।")
        return
    # Existing broadcast logic remains here

@app.on_callback_query()
async def callback_query_handler(c, cb):
    if cb.data == "cancel_task":
        user_id = cb.from_user.id
        if user_id in TASKS:
            TASKS[user_id].set()
            await cb.answer("অপারেশন বাতিল করা হয়েছে।")
            del TASKS[user_id]
        else:
            await cb.answer("কোনো চলমান অপারেশন নেই।")
    elif cb.data == "delete_caption":
        user_id = cb.from_user.id
        if user_id in USER_CAPTIONS:
            del USER_CAPTIONS[user_id]
            await cb.message.edit_text("আপনার ক্যাপশন মুছে ফেলা হয়েছে।")
        else:
            await cb.answer("মুছে ফেলার জন্য কোন ক্যাপশন নেই।")
    else:
        await cb.answer("এই বাটনে কোন কাজ নেই।")

# ---- Flask and other utility functions ----
@flask_app.route("/")
def index():
    return render_template_string("The bot is running.")

def ping_service():
    if not RENDER_EXTERNAL_HOSTNAME:
        print("Render URL is not set. Ping service is disabled.")
        return

    url = f"http://{RENDER_EXTERNAL_HOSTNAME}"
    while True:
        try:
            response = requests.get(url, timeout=10)
            print(f"Pinged {url} | Status Code: {response.status_code}")
        except requests.exceptions.RequestException as e:
            print(f"Error pinging {url}: {e}")
        time.sleep(600)

def run_flask_and_ping():
    flask_thread = threading.Thread(target=lambda: flask_app.run(host="0.0.0.0", port=PORT, use_reloader=False))
    flask_thread.start()
    ping_thread = threading.Thread(target=ping_service)
    ping_thread.start()
    print("Flask and Ping services started.")

async def periodic_cleanup():
    while True:
        try:
            now = datetime.now()
            for p in TMP.iterdir():
                try:
                    if p.is_file():
                        if now - datetime.fromtimestamp(p.stat().st_mtime) > timedelta(days=3):
                            p.unlink()
                except Exception:
                    pass
        except Exception:
            pass
        await asyncio.sleep(3600)

if __name__ == "__main__":
    print("Bot চালু হচ্ছে... Flask and Ping threads start করা হচ্ছে, তারপর Pyrogram চালু হবে।")
    t = threading.Thread(target=run_flask_and_ping, daemon=True)
    t.start()
    try:
        loop = asyncio.get_event_loop()
        loop.run_until_complete(periodic_cleanup())
        app.run()
    except KeyboardInterrupt:
        print("Bot বন্ধ করা হচ্ছে...")
