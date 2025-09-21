import os
import re
import aiohttp
import asyncio
import threading
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
# New env var from previous code
RENDER_EXTERNAL_HOSTNAME = os.getenv("RENDER_EXTERNAL_HOSTNAME") 

TMP = Path("tmp")
TMP.mkdir(parents=True, exist_ok=True)

# state
USER_THUMBS = {}
TASKS = {}
SET_THUMB_REQUEST = set()
SUBSCRIBERS = set()
SET_CAPTION_REQUEST = set()
USER_CAPTIONS = {}
# New state for dynamic captions
USER_COUNTERS = {}
# New state for edit caption mode
EDIT_CAPTION_MODE = set()
USER_THUMB_TIME = {}


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

async def download_url_generic(url: str, out_path: Path, message: Message = None, cancel_event: asyncio.Event = None, max_retries=3):
    timeout = aiohttp.ClientTimeout(total=7200)
    headers = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64)"}
    connector = aiohttp.TCPConnector(limit=0, force_close=True)
    
    for attempt in range(max_retries):
        if cancel_event and cancel_event.is_set():
            return False, "অপারেশন ব্যবহারকারী দ্বারা বাতিল করা হয়েছে।"
            
        try:
            async with aiohttp.ClientSession(timeout=timeout, headers=headers, connector=connector) as sess:
                async with sess.get(url, allow_redirects=True) as resp:
                    if resp.status == 403:
                        return False, "ডাউনলোড ব্যর্থ: HTTP 403 Forbidden. লিঙ্কটি সম্ভবত পাবলিক নয় বা অনুমতি নেই।"
                    elif resp.status == 429:
                        return False, "ডাউনলোড ব্যর্থ: HTTP 429 Too Many Requests. সার্ভার আপনার অনুরোধ ব্লক করছে।"
                    elif resp.status != 200:
                        return False, f"ডাউনলোড ব্যর্থ: HTTP {resp.status}"

                    ok, err = await download_stream(resp, out_path, message, cancel_event=cancel_event)
                    if ok:
                        return True, None
                    else:
                        logger.warning(f"Download stream failed: {err}. Retrying... (Attempt {attempt + 1}/{max_retries})")
                        await asyncio.sleep(5)  # Wait before retrying
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            logger.warning(f"Download failed with exception: {e}. Retrying... (Attempt {attempt + 1}/{max_retries})")
            await asyncio.sleep(5)
            continue
        except Exception as e:
            return False, str(e)
            
    return False, f"ডাউনলোড ব্যর্থ: {max_retries} বারের চেষ্টাতেও সফল হয়নি।"

async def download_drive_file(file_id: str, out_path: Path, message: Message = None, cancel_event: asyncio.Event = None, max_retries=3):
    for attempt in range(max_retries):
        if cancel_event and cancel_event.is_set():
            return False, "অপারেশন ব্যবহারকারী দ্বারা বাতিল করা হয়েছে।"
            
        base = f"https://drive.google.com/uc?export=download&id={file_id}"
        timeout = aiohttp.ClientTimeout(total=7200)
        headers = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64)"}
        connector = aiohttp.TCPConnector(limit=0, force_close=True)
        
        try:
            async with aiohttp.ClientSession(timeout=timeout, headers=headers, connector=connector) as sess:
                async with sess.get(base, allow_redirects=True) as resp:
                    if resp.status == 200 and "content-disposition" in (k.lower() for k in resp.headers.keys()):
                        ok, err = await download_stream(resp, out_path, message, cancel_event=cancel_event)
                        if ok: return True, None
                        
                    elif resp.status == 403:
                        return False, "ডাউনলোডের জন্য Google Drive থেকে অনুমতি প্রয়োজন বা লিংক পাবলিক নয়।"

                    text = await resp.text(errors="ignore")
                    m = re.search(r"confirm=([0-9A-Za-z-_]+)", text)
                    if m:
                        token = m.group(1)
                        download_url = f"https://drive.google.com/uc?export=download&confirm={token}&id={file_id}"
                        async with sess.get(download_url, allow_redirects=True) as resp2:
                            if resp2.status != 200:
                                return False, f"HTTP {resp2.status}"
                            ok, err = await download_stream(resp2, out_path, message, cancel_event=cancel_event)
                            if ok: return True, None
                            
                    for k, v in resp.cookies.items():
                        if k.startswith("download_warning"):
                            token = v.value
                            download_url = f"https://drive.google.com/uc?export=download&confirm={token}&id={file_id}"
                            async with sess.get(download_url, allow_redirects=True) as resp2:
                                if resp2.status != 200:
                                    return False, f"HTTP {resp2.status}"
                                ok, err = await download_stream(resp2, out_path, message, cancel_event=cancel_event)
                                if ok: return True, None
                                
            logger.warning(f"Drive download failed (stream or token). Retrying... (Attempt {attempt + 1}/{max_retries})")
            await asyncio.sleep(5)
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            logger.warning(f"Drive download failed with exception: {e}. Retrying... (Attempt {attempt + 1}/{max_retries})")
            await asyncio.sleep(5)
            continue
        except Exception as e:
            return False, str(e)

    return False, f"ডাউনলোড ব্যর্থ: {max_retries} বারের চেষ্টাতেও সফল হয়নি।"

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
        BotCommand("help", "সহায়িকা")
    ]
    try:
        await app.set_bot_commands(cmds)
    except Exception as e:
        logger.warning("Set commands error: %s", e)

# ---- handlers ----
@app.on_message(filters.command("start") & filters.private)
async def start_handler(c, m: Message):
    await set_bot_commands()
    SUBSCRIBERS.add(m.chat.id)
    text = (
        "Hi! আমি URL uploader bot.\n\n"
        "নোট: বটের অনেক কমান্ড শুধু অ্যাডমিন (owner) চালাতে পারবে।\n\n"
        "Commands:\n"
        "/upload_url <url> - URL থেকে ডাউনলোড ও Telegram-এ আপলোড (admin only)\n"
        "/setthumb - একটি ছবি পাঠান, সেট হবে আপনার থাম্বনেইল (admin only)\n"
        "/view_thumb - আপনার থাম্বনেইল দেখুন (admin only)\n"
        "/del_thumb - আপনার থাম্বনেইল মুছে ফেলুন (admin only)\n"
        "/set_caption - একটি ক্যাপশন সেট করুন (admin only)\n"
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

@app.on_message(filters.command("setthumb") & filters.private)
async def setthumb_prompt(c, m):
    if not is_admin(m.from_user.id):
        await m.reply_text("আপনার অনুমতি নেই এই কমান্ড চালানোর।")
        return
    
    uid = m.from_user.id
    if len(m.command) > 1:
        time_str = " ".join(m.command[1:])
        seconds = parse_time(time_str)
        if seconds > 0:
            USER_THUMB_TIME[uid] = seconds
            await m.reply_text(f"থাম্বনেইল তৈরির সময় সেট হয়েছে: {seconds} সেকেন্ড।")
        else:
            await m.reply_text("সঠিক ফরম্যাটে সময় দিন। উদাহরণ: `/setthumb 5s`, `/setthumb 1m`, `/setthumb 1m 30s`")
    else:
        SET_THUMB_REQUEST.add(uid)
        await m.reply_text("একটি ছবি পাঠান (photo) — সেট হবে আপনার থাম্বনেইল।")


@app.on_message(filters.command("view_thumb") & filters.private)
async def view_thumb_cmd(c, m: Message):
    if not is_admin(m.from_user.id):
        await m.reply_text("আপনার অনুমতি নেই এই কমান্ড চালানোর।")
        return
    uid = m.from_user.id
    thumb_path = USER_THUMBS.get(uid)
    thumb_time = USER_THUMB_TIME.get(uid)
    
    if thumb_path and Path(thumb_path).exists():
        await c.send_photo(chat_id=m.chat.id, photo=thumb_path, caption="এটা আপনার সেভ করা থাম্বনেইল।")
    elif thumb_time:
        await m.reply_text(f"আপনার থাম্বনেইল তৈরির সময় সেট করা আছে: {thumb_time} সেকেন্ড।")
    else:
        await m.reply_text("আপনার কোনো থাম্বনেইল বা থাম্বনেইল তৈরির সময় সেভ করা নেই। /setthumb দিয়ে সেট করুন।")

@app.on_message(filters.command("del_thumb") & filters.private)
async def del_thumb_cmd(c, m: Message):
    if not is_admin(m.from_user.id):
        await m.reply_text("আপনার অনুমতি নেই এই কমান্ড চালানোর।")
        return
    uid = m.from_user.id
    thumb_path = USER_THUMBS.get(uid)
    if thumb_path and Path(thumb_path).exists():
        try:
            Path(thumb_path).unlink()
        except Exception:
            pass
        USER_THUMBS.pop(uid, None)
    
    if uid in USER_THUMB_TIME:
        USER_THUMB_TIME.pop(uid)

    if not (thumb_path or uid in USER_THUMB_TIME):
        await m.reply_text("আপনার কোনো থাম্বনেইল সেভ করা নেই।")
    else:
        await m.reply_text("আপনার থাম্বনেইল/থাম্বনেইল তৈরির সময় মুছে ফেলা হয়েছে।")


@app.on_message(filters.photo & filters.private)
async def photo_handler(c, m: Message):
    if not is_admin(m.from_user.id):
        return
    uid = m.from_user.id
    if uid in SET_THUMB_REQUEST:
        SET_THUMB_REQUEST.discard(uid)
        out = TMP / f"thumb_{uid}.jpg"
        try:
            await m.download(file_name=str(out))
            img = Image.open(out)
            img.thumbnail((320, 320))
            img = img.convert("RGB")
            img.save(out, "JPEG")
            USER_THUMBS[uid] = str(out)
            # Make sure to clear the time setting if a photo is set
            USER_THUMB_TIME.pop(uid, None)
            await m.reply_text("আপনার থাম্বনেইল সেভ হয়েছে।")
        except Exception as e:
            await m.reply_text(f"থাম্বনেইল সেভ করতে সমস্যা: {e}")
    else:
        pass

# New handlers for caption
@app.on_message(filters.command("set_caption") & filters.private)
async def set_caption_prompt(c, m: Message):
    if not is_admin(m.from_user.id):
        await m.reply_text("আপনার অনুমতি নেই এই কমান্ড চালানোর।")
        return
    SET_CAPTION_REQUEST.add(m.from_user.id)
    # Reset counter data when a new caption is about to be set
    USER_COUNTERS.pop(m.from_user.id, None)
    await m.reply_text("ক্যাপশন দিন। কোড - [01 (+01, 01u)], [re (480p, 720p, 1080p)]")

@app.on_message(filters.command("view_caption") & filters.private)
async def view_caption_cmd(c, m: Message):
    if not is_admin(m.from_user.id):
        await m.reply_text("আপনার অনুমতি নেই এই কমান্ড চালানোর।")
        return
    uid = m.from_user.id
    caption = USER_CAPTIONS.get(uid)
    if caption:
        await m.reply_text(f"আপনার সেভ করা ক্যাপশন:\n\n`{caption}`", reply_markup=delete_caption_keyboard())
    else:
        await m.reply_text("আপনার কোনো ক্যাপশন সেভ করা নেই। /set_caption দিয়ে সেট করুন।")

@app.on_callback_query(filters.regex("delete_caption"))
async def delete_caption_cb(c, cb):
    uid = cb.from_user.id
    if not is_admin(uid):
        await cb.answer("আপনার অনুমতি নেই।", show_alert=True)
        return
    if uid in USER_CAPTIONS:
        USER_CAPTIONS.pop(uid)
        USER_COUNTERS.pop(uid, None) # New: delete counter data
        await cb.message.edit_text("আপনার ক্যাপশন মুছে ফেলা হয়েছে।")
    else:
        await cb.answer("আপনার কোনো ক্যাপশন সেভ করা নেই।", show_alert=True)

# New handler to toggle edit caption mode
@app.on_message(filters.command("edit_caption_mode") & filters.private)
async def toggle_edit_caption_mode(c, m: Message):
    uid = m.from_user.id
    if not is_admin(uid):
        await m.reply_text("আপনার অনুমতি নেই এই কমান্ড চালানোর।")
        return

    if uid in EDIT_CAPTION_MODE:
        EDIT_CAPTION_MODE.discard(uid)
        await m.reply_text("edit video caption mod off.\nএখন থেকে আপলোড করা ভিডিওর রিনেম ও থাম্বনেইল পরিবর্তন হবে, এবং সেভ করা ক্যাপশন যুক্ত হবে।")
    else:
        EDIT_CAPTION_MODE.add(uid)
        await m.reply_text("edit video caption mod on.\nএখন থেকে শুধু সেভ করা ক্যাপশন ভিডিওতে যুক্ত হবে। ভিডিওর নাম এবং থাম্বনেইল একই থাকবে।")


@app.on_message(filters.text & filters.private)
async def text_handler(c, m: Message):
    if not is_admin(m.from_user.id):
        return
    uid = m.from_user.id
    text = m.text.strip()
    
    # Handle set caption request
    if uid in SET_CAPTION_REQUEST:
        SET_CAPTION_REQUEST.discard(uid)
        USER_CAPTIONS[uid] = text
        USER_COUNTERS.pop(uid, None) # New: reset counter on new caption set
        await m.reply_text("আপনার ক্যাপশন সেভ হয়েছে। এখন থেকে আপলোড করা ভিডিওতে এই ক্যাপশন ব্যবহার হবে।")
        return

    # Handle auto URL upload
    if text.startswith("http://") or text.startswith("https://"):
        asyncio.create_task(handle_url_download_and_upload(c, m, text))
    
@app.on_message(filters.command("upload_url") & filters.private)
async def upload_url_cmd(c, m: Message):
    if not is_admin(m.from_user.id):
        await m.reply_text("আপনার অনুমতি নেই এই কমান্ড চালানোর।")
        return
    if not m.command or len(m.command) < 2:
        await m.reply_text("ব্যবহার: /upload_url <url>\nউদাহরণ: /upload_url https://example.com/file.mp4")
        return
    url = m.text.split(None, 1)[1].strip()
    asyncio.create_task(handle_url_download_and_upload(c, m, url))

async def handle_url_download_and_upload(c: Client, m: Message, url: str):
    uid = m.from_user.id
    cancel_event = asyncio.Event()
    TASKS.setdefault(uid, []).append(cancel_event)

    try:
        status_msg = await m.reply_text("ডাউনলোড শুরু হচ্ছে...", reply_markup=progress_keyboard())
    except Exception:
        status_msg = await m.reply_text("ডাউনলোড শুরু হচ্ছে...", reply_markup=progress_keyboard())
    try:
        fname = url.split("/")[-1].split("?")[0] or f"download_{int(datetime.now().timestamp())}"
        safe_name = re.sub(r"[\\/*?\"<>|:]", "_", fname)

        video_exts = {".mp4", ".mkv", ".avi", ".mov", ".flv", ".wmv", ".webm"}
        if not any(safe_name.lower().endswith(ext) for ext in video_exts):
            safe_name += ".mp4"

        tmp_in = TMP / f"dl_{uid}_{int(datetime.now().timestamp())}_{safe_name}"
        ok, err = False, None
        
        try:
            await status_msg.edit("ডাউনলোড হচ্ছে...", reply_markup=progress_keyboard())
        except Exception:
            status_msg = await m.reply_text("ডাউনলোড হচ্ছে...", reply_markup=progress_keyboard())

        if is_drive_url(url):
            fid = extract_drive_id(url)
            if not fid:
                try:
                    await status_msg.edit("Google Drive লিঙ্ক থেকে file id পাওয়া যায়নি। সঠিক লিংক দিন।", reply_markup=None)
                except Exception:
                    await m.reply_text("Google Drive লিঙ্ক থেকে file id পাওয়া যায়নি। সঠিক লিংক দিন।", reply_markup=None)
                TASKS[uid].remove(cancel_event)
                return
            ok, err = await download_drive_file(fid, tmp_in, status_msg, cancel_event=cancel_event)
        else:
            ok, err = await download_url_generic(url, tmp_in, status_msg, cancel_event=cancel_event)

        if not ok:
            try:
                await status_msg.edit(f"ডাউনলোড ব্যর্থ: {err}", reply_markup=None)
            except Exception:
                await m.reply_text(f"ডাউনলোড ব্যর্থ: {err}", reply_markup=None)
            try:
                if tmp_in.exists():
                    tmp_in.unlink()
            except:
                pass
            TASKS[uid].remove(cancel_event)
            return

        try:
            await status_msg.edit("ডাউনলোড সম্পন্ন, Telegram-এ আপলোড হচ্ছে...", reply_markup=None)
        except Exception:
            await m.reply_text("ডাউনলোড সম্পন্ন, Telegram-এ আপলোড হচ্ছে...", reply_markup=None)
        await process_file_and_upload(c, m, tmp_in, original_name=safe_name, messages_to_delete=[status_msg.id])
    except Exception as e:
        traceback.print_exc()
        try:
            await status_msg.edit(f"অপস! কিছু ভুল হয়েছে: {e}", reply_markup=None)
        except Exception:
            await m.reply_text(f"অপস! কিছু ভুল হয়েছে: {e}", reply_markup=None)
    finally:
        try:
            TASKS[uid].remove(cancel_event)
        except Exception:
            pass

async def handle_caption_only_upload(c: Client, m: Message):
    uid = m.from_user.id
    caption_to_use = USER_CAPTIONS.get(uid)
    if not caption_to_use:
        await m.reply_text("ক্যাপশন এডিট মোড চালু আছে কিন্তু কোনো সেভ করা ক্যাপশন নেই। /set_caption দিয়ে ক্যাপশন সেট করুন।")
        return

    cancel_event = asyncio.Event()
    TASKS.setdefault(uid, []).append(cancel_event)
    
    try:
        status_msg = await m.reply_text("ক্যাপশন এডিট করা হচ্ছে...", reply_markup=progress_keyboard())
    except Exception:
        status_msg = await m.reply_text("ক্যাপশন এডিট করা হচ্ছে...", reply_markup=progress_keyboard())
    
    try:
        source_message = m
        file_info = source_message.video or source_message.document

        if not file_info:
            try:
                await status_msg.edit("এটি একটি ভিডিও বা ডকুমেন্ট ফাইল নয়।")
            except Exception:
                await m.reply_text("এটি একটি ভিডিও বা ডকুমেন্ট ফাইল নয়।")
            return
        
        # Process the dynamic caption
        final_caption = process_dynamic_caption(uid, caption_to_use)
        
        if file_info.file_id:
            try:
                if source_message.video:
                    await c.send_video(
                        chat_id=m.chat.id,
                        video=file_info.file_id,
                        caption=final_caption,
                        thumb=file_info.thumbs[0].file_id if file_info.thumbs else None,
                        duration=file_info.duration,
                        supports_streaming=True,
                        parse_mode=ParseMode.MARKDOWN
                    )
                elif source_message.document:
                    await c.send_document(
                        chat_id=m.chat.id,
                        document=file_info.file_id,
                        file_name=file_info.file_name,
                        caption=final_caption,
                        thumb=file_info.thumbs[0].file_id if file_info.thumbs else None,
                        parse_mode=ParseMode.MARKDOWN
                    )
                try:
                    await status_msg.delete()
                except Exception:
                    pass
            except Exception as e:
                try:
                    await status_msg.edit(f"ক্যাপশন এডিটে ত্রুটি: {e}", reply_markup=None)
                except Exception:
                    await m.reply_text(f"ক্যাপশন এডিটে ত্রুটি: {e}", reply_markup=None)
                return
        else:
            try:
                await status_msg.edit("ফাইলের ফাইল আইডি পাওয়া যায়নি।", reply_markup=None)
            except Exception:
                await m.reply_text("ফাইলের ফাইল আইডি পাওয়া যায়নি।", reply_markup=None)
            return
        
        # New code to auto-delete the success message
        try:
            success_msg = await status_msg.edit("ক্যাপশন সফলভাবে আপডেট করা হয়েছে।", reply_markup=None)
            await asyncio.sleep(5)
            await success_msg.delete()
        except Exception:
            success_msg = await m.reply_text("ক্যাপশন সফলভাবে আপডেট করা হয়েছে।", reply_markup=None)
            await asyncio.sleep(5)
            await success_msg.delete()

    except Exception as e:
        traceback.print_exc()
        try:
            await status_msg.edit(f"ক্যাপশন এডিটে ত্রুটি: {e}", reply_markup=None)
        except Exception:
            await m.reply_text(f"ক্যাপশন এডিটে ত্রুটি: {e}", reply_markup=None)
    finally:
        try:
            TASKS[uid].remove(cancel_event)
        except Exception:
            pass

@app.on_message(filters.private & filters.forwarded & (filters.video | filters.document))
async def forwarded_file_rename(c: Client, m: Message):
    uid = m.from_user.id
    if not is_admin(uid):
        return
    
    # Check if the user is in edit caption mode
    if uid in EDIT_CAPTION_MODE:
        await handle_caption_only_upload(c, m)
        return

    cancel_event = asyncio.Event()
    TASKS.setdefault(uid, []).append(cancel_event)
    
    file_info = m.video or m.document
    
    if file_info and file_info.file_name:
        original_name = file_info.file_name
    elif m.video:
        original_name = f"video_{file_info.file_unique_id}.mp4"
    else:
        original_name = f"file_{file_info.file_unique_id}"

    try:
        status_msg = await m.reply_text("ফরওয়ার্ড করা ফাইল ডাউনলোড শুরু হচ্ছে...", reply_markup=progress_keyboard())
    except Exception:
        status_msg = await m.reply_text("ফরওয়ার্ড করা ফাইল ডাউনলোড শুরু হচ্ছে...", reply_markup=progress_keyboard())
    tmp_path = TMP / f"forwarded_{uid}_{int(datetime.now().timestamp())}_{original_name}"
    try:
        await m.download(file_name=str(tmp_path))
        try:
            await status_msg.edit("ডাউনলোড সম্পন্ন, এখন Telegram-এ আপলোড হচ্ছে...", reply_markup=None)
        except Exception:
            await m.reply_text("ডাউনলোড সম্পন্ন, এখন Telegram-এ আপলোড হচ্ছে...", reply_markup=None)
        await process_file_and_upload(c, m, tmp_path, original_name=original_name, messages_to_delete=[status_msg.id])
    except Exception as e:
        await m.reply_text(f"ফাইল প্রসেসিংয়ে সমস্যা: {e}")
    finally:
        try:
            TASKS[uid].remove(cancel_event)
        except Exception:
            pass

@app.on_message(filters.command("rename") & filters.private)
async def rename_cmd(c, m: Message):
    uid = m.from_user.id
    if not is_admin(uid):
        await m.reply_text("আপনার অনুমতি নেই।")
        return
    if not m.reply_to_message or not (m.reply_to_message.video or m.reply_to_message.document):
        await m.reply_text("ভিডিও/ডকুমেন্ট ফাইলের reply দিয়ে এই কমান্ড দিন।\nUsage: /rename new_name.mp4")
        return
    if len(m.command) < 2:
        await m.reply_text("নতুন ফাইল নাম দিন। উদাহরণ: /rename new_video.mp4")
        return
    new_name = m.text.split(None, 1)[1].strip()
    new_name = re.sub(r"[\\/*?\"<>|:]", "_", new_name)
    await m.reply_text(f"ভিডিও রিনেম করা হবে: {new_name}\n(রিনেম করতে reply করা ফাইলটি পুনরায় ডাউনলোড করে আপলোড করা হবে)")

    cancel_event = asyncio.Event()
    TASKS.setdefault(uid, []).append(cancel_event)
    try:
        status_msg = await m.reply_text("রিনেমের জন্য ফাইল ডাউনলোড করা হচ্ছে...", reply_markup=progress_keyboard())
    except Exception:
        status_msg = await m.reply_text("রিনেমের জন্য ফাইল ডাউনলোড করা হচ্ছে...", reply_markup=progress_keyboard())
    tmp_out = TMP / f"rename_{uid}_{int(datetime.now().timestamp())}_{new_name}"
    try:
        await m.reply_to_message.download(file_name=str(tmp_out))
        try:
            await status_msg.edit("ডাউনলোড সম্পন্ন, এখন নতুন নাম দিয়ে আপলোড হচ্ছে...", reply_markup=None)
        except Exception:
            await m.reply_text("ডাউনলোড সম্পন্ন, এখন নতুন নাম দিয়ে আপলোড হচ্ছে...", reply_markup=None)
        await process_file_and_upload(c, m, tmp_out, original_name=new_name, messages_to_delete=[status_msg.id])
    except Exception as e:
        await m.reply_text(f"রিনেম ত্রুটি: {e}")
    finally:
        try:
            TASKS[uid].remove(cancel_event)
        except Exception:
            pass

@app.on_callback_query(filters.regex("cancel_task"))
async def cancel_task_cb(c, cb):
    uid = cb.from_user.id
    if uid in TASKS and TASKS[uid]:
        for ev in list(TASKS[uid]):
            try:
                ev.set()
            except:
                pass
        await cb.answer("অপারেশন বাতিল করা হয়েছে।", show_alert=True)
        try:
            await cb.message.delete()
        except Exception:
            pass
    else:
        await cb.answer("কোনো অপারেশন চলছে না।", show_alert=True)

# ---- main processing and upload ----
async def generate_video_thumbnail(video_path: Path, thumb_path: Path, timestamp_sec: int = 1):
    try:
        cmd = [
            "ffmpeg",
            "-y",
            "-i", str(video_path),
            "-ss", str(timestamp_sec),
            "-vframes", "1",
            "-vf", "scale=320:-1",
            str(thumb_path)
        ]
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        return thumb_path.exists() and thumb_path.stat().st_size > 0
    except Exception as e:
        logger.warning("Thumbnail generate error: %s", e)
        return False

async def convert_to_mkv(in_path: Path, out_path: Path, status_msg: Message):
    try:
        try:
            await status_msg.edit("ভিডিওটি MKV ফরম্যাটে কনভার্ট করা হচ্ছে...", reply_markup=progress_keyboard())
        except Exception:
            await m.reply_text("ভিডিওটি MKV ফরম্যাটে কনভার্ট করা হচ্ছে...", reply_markup=progress_keyboard())
        cmd = [
            "ffmpeg",
            "-i", str(in_path),
            "-codec", "copy",
            str(out_path)
        ]
        
        result = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=1200)
        
        if result.returncode != 0:
            logger.warning("Container conversion failed, attempting full re-encoding: %s", result.stderr)
            try:
                await status_msg.edit("ভিডিওটি MKV ফরম্যাটে পুনরায় এনকোড করা হচ্ছে...", reply_markup=progress_keyboard())
            except Exception:
                await m.reply_text("ভিডিওটি MKV ফরম্যাটে পুনরায় এনকোড করা হচ্ছে...", reply_markup=progress_keyboard())
            cmd_full = [
                "ffmpeg",
                "-i", str(in_path),
                "-c:v", "libx264",
                "-preset", "fast",
                "-crf", "23",
                "-c:a", "copy",
                str(out_path)
            ]
            result_full = subprocess.run(cmd_full, capture_output=True, text=True, check=False, timeout=3600)
            if result_full.returncode != 0:
                raise Exception(f"Full re-encoding failed: {result_full.stderr}")

        if not out_path.exists() or out_path.stat().st_size == 0:
            raise Exception("Converted file not found or is empty.")
        
        return True, None
    except Exception as e:
        logger.error("Video conversion error: %s", e)
        return False, str(e)

def process_dynamic_caption(uid, caption_template):
    # Initialize user state if it doesn't exist
    if uid not in USER_COUNTERS:
        USER_COUNTERS[uid] = {'uploads': 0, 'episode_numbers': {}}

    # Increment upload counter for the current user
    USER_COUNTERS[uid]['uploads'] += 1

    # Episode Number Logic (e.g., [(01) (+1, 3u)])
    episode_matches = re.findall(r"\[\((\d+)\) \(\+(\d+), (\d+)u\)\]", caption_template)
    for match in episode_matches:
        original_placeholder = f"[({match[0]}) (+{match[1]}, {match[2]}u)]"
        start_num = int(match[0])
        increment_val = int(match[1])
        uploads_per_inc = int(match[2])

        # Create a unique key for this specific episode code
        code_key = f"episode_{start_num}_{increment_val}_{uploads_per_inc}"
        if code_key not in USER_COUNTERS[uid]['episode_numbers']:
            USER_COUNTERS[uid]['episode_numbers'][code_key] = start_num
        
        # Calculate the current episode number
        current_uploads = USER_COUNTERS[uid]['uploads']
        episode_number = start_num + ((current_uploads - 1) // uploads_per_inc) * increment_val
        
        # Format the number with leading zeros if necessary
        formatted_episode_number = f"{episode_number:02d}"

        caption_template = caption_template.replace(original_placeholder, f"({formatted_episode_number})", 1)

    # Episode Number Logic (e.g., [01 (+01, 3u)])
    episode_matches_no_paren = re.findall(r"\[(\d+) \(\+(\d+), (\d+)u\)\]", caption_template)
    for match in episode_matches_no_paren:
        original_placeholder = f"[{match[0]} (+{match[1]}, {match[2]}u)]"
        start_num = int(match[0])
        increment_val = int(match[1])
        uploads_per_inc = int(match[2])
        
        code_key = f"episode_{start_num}_{increment_val}_{uploads_per_inc}"
        if code_key not in USER_COUNTERS[uid]['episode_numbers']:
            USER_COUNTERS[uid]['episode_numbers'][code_key] = start_num
        
        current_uploads = USER_COUNTERS[uid]['uploads']
        episode_number = start_num + ((current_uploads - 1) // uploads_per_inc) * increment_val
        
        formatted_episode_number = f"{episode_number:02d}"
        
        caption_template = caption_template.replace(original_placeholder, formatted_episode_number, 1)


    # Quality Cycle Logic (e.g., [re (480p), (720p), (1080p)])
    quality_match = re.search(r"\[re\s*\(.*?\)\]", caption_template)
    if quality_match:
        options_str = quality_match.group(0)
        options_list_str = options_str[options_str.find("(") + 1:options_str.rfind(")")]
        options = [opt.strip().strip("()") for opt in options_list_str.split(',')]
        
        current_index = (USER_COUNTERS[uid]['uploads'] - 1) % len(options)
        current_quality = options[current_index]
        
        caption_template = caption_template.replace(quality_match.group(0), current_quality)

    # New: End of series/special episode logic
    end_matches = re.findall(r"\[End \((\d+[a-zA-Z]*), (\d+)\)\]", caption_template)
    for match in end_matches:
        end_placeholder = f"[End ({match[0]}, {match[1]})]"
        end_episode_num_str = re.sub(r'[^0-9]', '', match[0])
        end_episode_num = int(end_episode_num_str) if end_episode_num_str else 0
        repeat_count = int(match[1])

        current_uploads = USER_COUNTERS[uid]['uploads']

        if current_uploads >= end_episode_num and current_uploads < end_episode_num + repeat_count:
            caption_template = caption_template.replace(end_placeholder, "End")
        else:
            caption_template = caption_template.replace(end_placeholder, "")
    
    return "**" + "\n".join(caption_template.splitlines()) + "**"


async def process_file_and_upload(c: Client, m: Message, in_path: Path, original_name: str = None, messages_to_delete: list = None):
    uid = m.from_user.id
    cancel_event = asyncio.Event()
    TASKS.setdefault(uid, []).append(cancel_event)
    
    upload_path = in_path
    temp_thumb_path = None
    final_caption_template = USER_CAPTIONS.get(uid)

    try:
        final_name = original_name or in_path.name
        is_video = bool(m.video)
        
        if is_video:
            if in_path.suffix.lower() not in {".mp4", ".mkv"}:
                mkv_path = TMP / f"{in_path.stem}.mkv"
                try:
                    status_msg = await m.reply_text(f"ভিডিওটি {in_path.suffix} ফরম্যাটে আছে। MKV এ কনভার্ট করা হচ্ছে...", reply_markup=progress_keyboard())
                except Exception:
                    status_msg = await m.reply_text(f"ভিডিওটি {in_path.suffix} ফরম্যাটে আছে। MKV এ কনভার্ট করা হচ্ছে...", reply_markup=progress_keyboard())
                if messages_to_delete:
                    messages_to_delete.append(status_msg.id)
                ok, err = await convert_to_mkv(in_path, mkv_path, status_msg)
                if not ok:
                    try:
                        await status_msg.edit(f"কনভার্সন ব্যর্থ: {err}\nমূল ফাইলটি আপলোড করা হচ্ছে...", reply_markup=None)
                    except Exception:
                        await m.reply_text(f"কনভার্সন ব্যর্থ: {err}\nমূল ফাইলটি আপলোড করা হচ্ছে...", reply_markup=None)
                else:
                    upload_path = mkv_path
        
        thumb_path = USER_THUMBS.get(uid)
        
        if is_video and not thumb_path:
            temp_thumb_path = TMP / f"thumb_{uid}_{int(datetime.now().timestamp())}.jpg"
            thumb_time_sec = USER_THUMB_TIME.get(uid, 1) # Default to 1 second
            ok = await generate_video_thumbnail(upload_path, temp_thumb_path, timestamp_sec=thumb_time_sec)
            if ok:
                thumb_path = str(temp_thumb_path)

        try:
            status_msg = await m.reply_text("আপলোড শুরু হচ্ছে...", reply_markup=progress_keyboard())
        except Exception:
            status_msg = await m.reply_text("আপলোড শুরু হচ্ছে...", reply_markup=progress_keyboard())
        if messages_to_delete:
            messages_to_delete.append(status_msg.id)

        if cancel_event.is_set():
            try:
                await status_msg.edit("অপারেশন বাতিল করা হয়েছে, আপলোড শুরু করা হয়নি।", reply_markup=None)
            except Exception:
                await m.reply_text("অপারেশন বাতিল করা হয়েছে, আপলোড শুরু করা হয়নি।", reply_markup=None)
            TASKS[uid].remove(cancel_event)
            return
        
        duration_sec = get_video_duration(upload_path) if upload_path.exists() else 0
        
        caption_to_use = final_name
        if final_caption_template:
            caption_to_use = process_dynamic_caption(uid, final_caption_template)

        upload_attempts = 3
        last_exc = None
        for attempt in range(1, upload_attempts + 1):
            try:
                if is_video:
                    await c.send_video(
                        chat_id=m.chat.id,
                        video=str(upload_path),
                        caption=caption_to_use,
                        thumb=thumb_path,
                        duration=duration_sec,
                        supports_streaming=True,
                        parse_mode=ParseMode.MARKDOWN
                    )
                else:
                    await c.send_document(
                        chat_id=m.chat.id,
                        document=str(upload_path),
                        file_name=final_name,
                        caption=caption_to_use,
                        parse_mode=ParseMode.MARKDOWN
                    )
                
                if messages_to_delete:
                    try:
                        await c.delete_messages(chat_id=m.chat.id, message_ids=messages_to_delete)
                    except Exception:
                        pass
                
                last_exc = None
                break
            except Exception as e:
                last_exc = e
                logger.warning("Upload attempt %s failed: %s", attempt, e)
                await asyncio.sleep(2 * attempt)
                if cancel_event.is_set():
                    if messages_to_delete:
                        try:
                            await c.delete_messages(chat_id=m.chat.id, message_ids=messages_to_delete)
                        except Exception:
                            pass
                    break

        if last_exc:
            await m.reply_text(f"আপলোড ব্যর্থ: {last_exc}", reply_markup=None)
    except Exception as e:
        await m.reply_text(f"আপলোডে ত্রুটি: {e}")
    finally:
        try:
            if upload_path != in_path and upload_path.exists():
                upload_path.unlink()
            if in_path.exists():
                in_path.unlink()
            if temp_thumb_path and Path(temp_thumb_path).exists():
                Path(temp_thumb_path).unlink()
            TASKS[uid].remove(cancel_event)
        except Exception:
            pass

# *** সংশোধিত: ব্রডকাস্ট কমান্ড ***
@app.on_message(filters.command("broadcast") & filters.private)
async def broadcast_cmd_no_reply(c, m: Message):
    uid = m.from_user.id
    if not is_admin(uid):
        await m.reply_text("আপনার অনুমতি নেই।")
        return
    if not m.reply_to_message:
        await m.reply_text("ব্রডকাস্ট করতে যেকোনো মেসেজে (ছবি, ভিডিও বা টেক্সট) **রিপ্লাই করে** এই কমান্ড দিন।")
        return

@app.on_message(filters.command("broadcast") & filters.private & filters.reply)
async def broadcast_cmd_reply(c, m: Message):
    uid = m.from_user.id
    if not is_admin(uid):
        await m.reply_text("আপনার অনুমতি নেই।")
        return
    
    source_message = m.reply_to_message
    if not source_message:
        await m.reply_text("ব্রডকাস্ট করার জন্য একটি মেসেজে রিপ্লাই করে এই কমান্ড দিন।")
        return

    await m.reply_text(f"ব্রডকাস্ট শুরু হচ্ছে {len(SUBSCRIBERS)} সাবস্ক্রাইবারে...", quote=True)
    failed = 0
    sent = 0
    for chat_id in list(SUBSCRIBERS):
        if chat_id == m.chat.id:
            continue
        try:
            await c.forward_messages(chat_id=chat_id, from_chat_id=source_message.chat.id, message_ids=source_message.id)
            sent += 1
            await asyncio.sleep(0.08)
        except Exception as e:
            failed += 1
            logger.warning("Broadcast to %s failed: %s", chat_id, e)

    await m.reply_text(f"ব্রডকাস্ট শেষ। পাঠানো: {sent}, ব্যর্থ: {failed}")

# --- Flask Web Server ---
@flask_app.route('/')
def home():
    html_content = """
    <!DOCTYPE-html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Bot Status</title>
        <style>
            body {
                font-family: Arial, sans-serif;
                background-color: #f0f2f5;
                color: #333;
                text-align: center;
                padding-top: 50px;
            }
            .container {
                background-color: #fff;
                padding: 30px;
                border-radius: 10px;
                box-shadow: 0 4px 8px rgba(0,0,0,0.1);
                display: inline-block;
            }
            h1 {
                color: #28a745;
            }
        </style>
    </head>
    <body>
        <div class="container">
            <h1>TA File Share Bot is running! ✅</h1>
            <p>This page confirms that the bot's web server is active.</p>
        </div>
    </body>
    </html>
    """
    return render_template_string(html_content)

# Ping service to keep the bot alive
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
        loop.create_task(periodic_cleanup())
    except RuntimeError:
        pass
    app.run()
