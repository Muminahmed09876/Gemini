# এটি একটি সম্পূর্ণ টেলিগ্রাম বট স্ক্রিপ্ট যা শুধুমাত্র একজন নির্দিষ্ট অ্যাডমিন ব্যবহারকারীর জন্য কাজ করবে।
# বটটি Gemini API ব্যবহার করে কথোপকথনের উত্তর দেয়।
# এতে একটি Flask ওয়েব সার্ভার এবং একটি পিং সার্ভিস যোগ করা হয়েছে যা বটকে সক্রিয় রাখে।
# সমস্ত সংবেদনশীল তথ্য (.env ফাইল থেকে) পরিবেশ ভেরিয়েবল হিসাবে লোড করা হয়।

# কোডটি চালানোর আগে, নিচের লাইব্রেরিগুলো অবশ্যই ইনস্টল করতে হবে:
# pip install -r requirements.txt

import os
import threading
from flask import Flask, render_template_string
import requests
import time
import logging
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
# সঠিক জেমিনি লাইব্রেরিটি স্পষ্টভাবে ইম্পোর্ট করা হয়েছে
import google.generativeai as genai

# .env ফাইল থেকে পরিবেশ ভেরিয়েবল লোড করা হয়
load_dotenv()

# লগিং সেট আপ করা হয়েছে যাতে আপনি কনসোলে বটের কার্যক্রম দেখতে পারেন।
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
                    level=logging.INFO)
logger = logging.getLogger(__name__)

# সংবেদনশীল তথ্য পরিবেশ ভেরিয়েবল থেকে পড়া হয়।
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
try:
    ADMIN_USER_ID = int(os.getenv("ADMIN_USER_ID"))
except (ValueError, TypeError):
    logging.error("ADMIN_USER_ID is not set or is not an integer. Please check your .env file.")
    ADMIN_USER_ID = None

# জেমিনি এপিআই-এর জন্য নির্দিষ্ট মডেল।
GEMINI_MODEL = "gemini-2.5-flash-preview-05-20"
genai.configure(api_key=GEMINI_API_KEY)
client = genai.Client()

# --- Flask Web Server and Ping Service ---
# Flask অ্যাপ তৈরি করা হয়েছে।
flask_app = Flask(__name__)

# পরিবেশ ভেরিয়েবল থেকে পোর্ট এবং হোস্টনাম নেওয়া হয়।
PORT = int(os.getenv("PORT", "5000"))
RENDER_EXTERNAL_HOSTNAME = os.getenv("RENDER_EXTERNAL_HOSTNAME")

@flask_app.route('/')
def home():
    """
    বটের ওয়েব সার্ভার চালু আছে কিনা তা নিশ্চিত করতে একটি সাধারণ HTML পৃষ্ঠা রেন্ডার করে।
    """
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

def ping_service():
    """
    ওয়েব সার্ভিসকে সক্রিয় রাখতে নির্দিষ্ট সময় পর পর এর বাহ্যিক হোস্টনামে পিং করে।
    """
    if not RENDER_EXTERNAL_HOSTNAME:
        logger.info("Render URL is not set. Ping service is disabled.")
        return

    url = f"http://{RENDER_EXTERNAL_HOSTNAME}"
    while True:
        try:
            response = requests.get(url, timeout=10)
            logger.info(f"Pinged {url} | Status Code: {response.status_code}")
        except requests.exceptions.RequestException as e:
            logger.error(f"Error pinging {url}: {e}")
        time.sleep(600)  # প্রতি ১০ মিনিট পর পর পিং করা হয় (৬০০ সেকেন্ড)

def run_flask_and_ping():
    """
    ফ্ল্যাস্ক ওয়েব সার্ভার এবং পিং সার্ভিস আলাদা থ্রেডে চালু করে।
    """
    flask_thread = threading.Thread(target=lambda: flask_app.run(host="0.0.0.0", port=PORT, use_reloader=False))
    flask_thread.start()
    
    ping_thread = threading.Thread(target=ping_service)
    ping_thread.start()
    logger.info("Flask and Ping services started.")

# --- Telegram Bot Functions ---
def is_admin(update: Update) -> bool:
    """
    যাচাই করে যে মেসেজটি একজন অ্যাডমিন ব্যবহারকারীর থেকে এসেছে কিনা।
    """
    if not ADMIN_USER_ID:
        return False
        
    if update.effective_user:
        return update.effective_user.id == ADMIN_USER_ID
    return False

async def get_gemini_response(prompt_text: str) -> str:
    """
    জেমিনি এপিআই-তে একটি প্রম্পট পাঠায় এবং টেক্সট উত্তর ফিরিয়ে দেয়।
    """
    try:
        if not GEMINI_API_KEY:
            return "Gemini API Key সেট করা হয়নি।"

        response = client.models.generate_content(
            model=GEMINI_MODEL, 
            contents=prompt_text
        )
        
        return response.text
    except Exception as e:
        logging.error(f"Error calling Gemini API: {e}")
        return "দুঃখিত, বর্তমানে জেমিনি এপিআই-এর সাথে সংযোগ স্থাপন করা যাচ্ছে না।"
    
    return "এপিআই থেকে কোনো উত্তর পাওয়া যায়নি।"

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /start কমান্ড এলে স্বাগত মেসেজ পাঠায়।
    """
    if is_admin(update):
        await update.message.reply_text("স্বাগতম! আপনি একজন অ্যাডমিন। আপনি আমাকে যেকোনো প্রশ্ন করতে পারেন।")
    else:
        await update.message.reply_text("দুঃখিত, এই বটটি শুধুমাত্র অ্যাডমিনদের জন্য।")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    ব্যবহারকারীর মেসেজ পরিচালনা করে। শুধুমাত্র অ্যাডমিন ব্যবহারকারীরাই উত্তর পাবেন।
    """
    if not is_admin(update):
        await update.message.reply_text("দুঃখিত, আপনার এই বটটি ব্যবহারের অনুমতি নেই।")
        logging.info(f"Unauthorized access attempt by user ID: {update.effective_user.id}")
        return

    user_message = update.message.text
    logging.info(f"অ্যাডমিন মেসেজ: {user_message}")
    
    await update.message.chat.send_action(action="typing")
    
    gemini_response = await get_gemini_response(user_message)
    
    await update.message.reply_text(gemini_response)

def run_bot() -> None:
    """
    বট চালানোর প্রধান ফাংশন।
    """
    if not TELEGRAM_BOT_TOKEN:
        logging.error("TELEGRAM_BOT_TOKEN is not set. Please check your .env file.")
        return

    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    
    logging.info("বট চালু হয়েছে।")
    application.run_polling()

# --- Main Application Entry Point ---
if __name__ == "__main__":
    # বট এবং ওয়েব সার্ভারকে আলাদা থ্রেডে চালানো হয়
    bot_thread = threading.Thread(target=run_bot)
    bot_thread.start()
    
    run_flask_and_ping()
