# -*- coding: utf-8 -*-

import logging
import os
import asyncio
import threading
import requests
import time
from pyrogram import Client, filters, idle
from pyrogram.types import Message
from moviepy.editor import VideoFileClip
from moviepy.video.io.ffmpeg_tools import ffmpeg_extract_subclip
from flask import Flask, render_template_string

# Logging সেট আপ করুন
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- Flask Web Server and Ping Service ---
flask_app = Flask(__name__)

# এনভায়রনমেন্ট ভেরিয়েবল থেকে তথ্য লোড করুন
PORT = int(os.getenv("PORT", "5000"))
RENDER_EXTERNAL_HOSTNAME = os.getenv("RENDER_EXTERNAL_HOSTNAME")

@flask_app.route('/')
def home():
    """
    একটি সাধারণ HTML পৃষ্ঠা রেন্ডার করে যা বটের ওয়েব সার্ভার চালু আছে কিনা তা নিশ্চিত করে।
    """
    html_content = """
    <!DOCTYPE html>
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

def ping_service():
    """
    পর্যায়ক্রমে বটের এক্সটার্নাল হোস্টনেম-এ পিং করে ওয়েব সার্ভিসকে সচল রাখে।
    """
    if not RENDER_EXTERNAL_HOSTNAME:
        logger.info("Render URL সেট করা নেই। পিং সার্ভিস নিষ্ক্রিয় করা হলো।")
        return

    url = f"http://{RENDER_EXTERNAL_HOSTNAME}"
    while True:
        try:
            response = requests.get(url, timeout=10)
            logger.info(f"Pinged {url} | স্ট্যাটাস কোড: {response.status_code}")
        except requests.exceptions.RequestException as e:
            logger.error(f"পিং করার সময় ত্রুটি: {url}: {e}")
        time.sleep(600)  # প্রতি ১০ মিনিটে পিং করুন (৬০০ সেকেন্ড)

def run_flask_and_ping():
    """
    ফ্ল্যাঙ্ক ওয়েব সার্ভার এবং পিং সার্ভিস আলাদা থ্রেডে চালু করে।
    """
    flask_thread = threading.Thread(target=lambda: flask_app.run(host="0.0.0.0", port=PORT, use_reloader=False))
    flask_thread.start()
    
    ping_thread = threading.Thread(target=ping_service)
    ping_thread.start()
    logger.info("ফ্ল্যাঙ্ক এবং পিং সার্ভিস শুরু হয়েছে।")

# --- বট অ্যাপ্লিকেশন ---
# এনভায়রনমেন্ট ভেরিয়েবল থেকে বট, API, এবং অ্যাডমিন তথ্য লোড করুন।
try:
    API_ID = int(os.getenv("API_ID"))
    API_HASH = os.getenv("API_HASH")
    BOT_TOKEN = os.getenv("BOT_TOKEN")
    ADMIN_ID = int(os.getenv("ADMIN_ID"))
except (ValueError, TypeError) as e:
    logger.error(f"এনভায়রনমেন্ট ভেরিয়েবল লোড করতে ত্রুটি: {e}")
    logger.error("নিশ্চিত করুন যে API_ID এবং ADMIN_ID সংখ্যা এবং বাকিগুলো স্ট্রিং হিসেবে সেট করা আছে।")
    exit()

# ভিডিও ফাইলগুলো অস্থায়ীভাবে সংরক্ষণ করার জন্য একটি ডিরেক্টরি তৈরি করুন
TEMP_DIR = "temp_videos"
if not os.path.exists(TEMP_DIR):
    os.makedirs(TEMP_DIR)

# Initialize the Pyrogram client
app = Client(
    "video_converter_bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN
)

async def progress_callback(current, total):
    """Callback function for showing download progress."""
    logger.info(f"Downloaded {current * 100 / total:.1f}%")

async def convert_video(input_path: str, output_path: str, message: Message):
    """
    Converts and compresses a video file with advanced settings.
    This function is run in a separate thread to avoid blocking the bot.
    """
    try:
        await message.reply_text("ভিডিওটি প্রসেস করা হচ্ছে। অনুগ্রহ করে অপেক্ষা করুন... ⏳")
        clip = VideoFileClip(input_path)
        
        # Determine new resolution (e.g., scale to 720p if original is larger)
        target_height = 720
        if clip.h > target_height:
            resized_clip = clip.resize(height=target_height)
        else:
            resized_clip = clip
            
        resized_clip.write_videofile(
            output_path, 
            codec="libx264",
            audio_codec="aac",
            bitrate="1000k", # Adjust bitrate to control file size and quality
            preset="medium"  # 'ultrafast', 'fast', 'medium', 'slow' for trade-off between speed and size
        )
        
        clip.close()
        resized_clip.close()
        return True
    except Exception as e:
        logger.error(f"Error during video conversion: {e}")
        return False

@app.on_message(filters.command("start") & filters.private)
async def start_command(client: Client, message: Message):
    """Handles the /start command."""
    await message.reply_text(
        "স্বাগতম! আমাকে একটি ভিডিও পাঠান এবং আমি এর আকার ছোট করে দেব। "
        "আপনি যদি ভিডিওর কিছু অংশ কাটতে চান, তাহলে ভিডিওর সাথে `/cut <start> <end>` লিখে পাঠান।"
    )

@app.on_message(filters.command("cut") & filters.private)
async def cut_command(client: Client, message: Message):
    """Handles the /cut command to trim videos."""
    if not message.reply_to_message or not message.reply_to_message.video:
        await message.reply_text("অনুগ্রহ করে একটি ভিডিওর উত্তরে `/cut <শুরু> <শেষ>` লিখে পাঠান।")
        return
    
    try:
        _, start_time, end_time = message.text.split()
        start_time, end_time = int(start_time), int(end_time)
        if start_time < 0 or end_time < 0 or start_time >= end_time:
            await message.reply_text("সময়সীমাটি সঠিক নয়। শুরু সময় অবশ্যই শেষ সময়ের থেকে কম হতে হবে।")
            return
    except (ValueError, IndexError):
        await message.reply_text("ব্যবহার: `/cut <শুরু_সময়> <শেষ_সময়>` (সেকেন্ডে)")
        return
        
    await message.reply_text("ভিডিওটি কাটা হচ্ছে... ✂️")
    
    input_video = await message.reply_to_message.download(file_name=os.path.join(TEMP_DIR, f"{message.reply_to_message.video.file_id}_original.mp4"), progress=progress_callback)
    output_video = os.path.join(TEMP_DIR, f"{message.reply_to_message.video.file_id}_cut.mp4")
    
    try:
        await asyncio.to_thread(
            ffmpeg_extract_subclip, input_video, start_time, end_time, targetname=output_video
        )
        await message.reply_video(output_video, caption="আপনার কাটা ভিডিওটি এখানে।")
    except Exception as e:
        logger.error(f"Error while cutting video: {e}")
        await message.reply_text("ভিডিও কাটতে সমস্যা হয়েছে।")
    finally:
        os.remove(input_video)
        if os.path.exists(output_video):
            os.remove(output_video)

@app.on_message(filters.video & filters.private)
async def video_handler(client: Client, message: Message):
    """Handles video messages to compress them."""
    await message.reply_text("ভিডিও পেয়েছি! এটি কম্প্রেস করা হচ্ছে... 🎬")
    
    input_video = await message.download(file_name=os.path.join(TEMP_DIR, f"{message.video.file_id}_original.mp4"), progress=progress_callback)
    output_video = os.path.join(TEMP_DIR, f"{message.video.file_id}_compressed.mp4")
    
    success = await convert_video(input_video, output_video, message)
    
    if success and os.path.exists(output_video):
        await message.reply_video(output_video, caption="আপনার কম্প্রেস করা ভিডিওটি এখানে। ✨")
    else:
        await message.reply_text("ভিডিও কম্প্রেস করতে সমস্যা হয়েছে।")
    
    # Cleanup
    if os.path.exists(input_video):
        os.remove(input_video)
    if os.path.exists(output_video):
        os.remove(output_video)

def main() -> None:
    """বট এবং ওয়েব সার্ভিস শুরু করুন।"""
    run_flask_and_ping()
    try:
        app.run()
    except (BotMethodInvalid, AuthKeyUnregistered) as e:
        logger.error(f"অপ্রমাণিত টোকেন বা API হ্যাশ: {e}")
        logger.error("আপনার বট টোকেন বা API তথ্য সঠিক আছে কিনা তা নিশ্চিত করুন।")
    except Exception as e:
        logger.error(f"একটি অপ্রত্যাশিত ত্রুটি ঘটেছে: {e}")

if __name__ == "__main__":
    main()
