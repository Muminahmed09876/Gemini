import os
import threading
from flask import Flask, render_template_string
import requests
import time
import logging
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
import google.generativeai as genai

# .env ফাইল থেকে ভেরিয়েবল লোড করুন
load_dotenv()

# Set up logging for clarity
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- Configuration from .env ---
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
ADMIN_USER_ID = int(os.getenv("ADMIN_USER_ID"))

# Gemini API কনফিগারেশন
genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel('gemini-pro')

# --- Flask Web Server and Ping Service ---
# The Flask app is created.
flask_app = Flask(__name__)

# env variables
PORT = int(os.getenv("PORT", "5000"))
RENDER_EXTERNAL_HOSTNAME = os.getenv("RENDER_EXTERNAL_HOSTNAME")

@flask_app.route('/')
def home():
    """
    Renders a simple HTML page to confirm the bot's web server is running.
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
    Periodically pings the bot's external hostname to keep the web service awake.
    This is often used on platforms like Render that spin down inactive services.
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
        time.sleep(600)  # Ping every 10 minutes (600 seconds)

def run_flask_and_ping():
    """
    Starts the Flask web server and the ping service in separate threads.
    """
    flask_thread = threading.Thread(target=lambda: flask_app.run(host="0.0.0.0", port=PORT, use_reloader=False))
    flask_thread.start()
    
    ping_thread = threading.Thread(target=ping_service)
    ping_thread.start()
    logger.info("Flask and Ping services started.")

# --- Telegram Bot Functionality ---

# ইউজার অ্যাডমিন কিনা তা চেক করার জন্য একটি ডেকোরেটর
def admin_only(func):
    """শুধুমাত্র অ্যাডমিন ইউজারদের জন্য একটি ডেকোরেটর"""
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        if user_id != ADMIN_USER_ID:
            logger.info(f"Unauthorized access attempt by user ID: {user_id}")
            await update.message.reply_text("দুঃখিত, আপনি এই বটটি ব্যবহার করতে পারবেন না।")
            return
        return await func(update, context)
    return wrapper

# /start কমান্ডের জন্য হ্যান্ডলার
@admin_only
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """বট শুরু হলে এটি ব্যবহার হয়"""
    await update.message.reply_text(
        f"নমস্কার! আমি জেমিনি, আপনার ব্যক্তিগত সহকারী। আপনি আমাকে যেকোনো প্রশ্ন করতে পারেন।"
    )

# মেসেজ হ্যান্ডলার
@admin_only
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """ব্যবহারকারীর মেসেজ জেমিনি এপিআইতে পাঠায় এবং উত্তর দেয়"""
    user_message = update.message.text
    
    try:
        # জেমিনি মডেল থেকে উত্তর আনা
        response = model.generate_content(user_message)
        bot_reply = response.text
        
        # জেমিনির উত্তর ব্যবহারকারীকে পাঠানো
        await update.message.reply_text(bot_reply)
    except Exception as e:
        logger.error(f"Error while communicating with Gemini API: {e}")
        await update.message.reply_text("কিছু একটা ভুল হয়েছে, আবার চেষ্টা করুন।")

# মেইন ফাংশন
def main() -> None:
    """বটটি রান করে"""
    
    # Run Flask and Ping services in the background
    run_flask_and_ping()

    # Build the Telegram bot application
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    
    # Command handlers
    application.add_handler(CommandHandler("start", start))
    
    # Message handlers (for text messages only, excluding commands)
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    
    # Start the bot in polling mode
    logger.info("Telegram Bot is running...")
    application.run_polling()

if __name__ == "__main__":
    main()
