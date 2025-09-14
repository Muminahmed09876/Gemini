import logging
import os
import threading
import time
import requests
import google.generativeai as genai
from flask import Flask, render_template_string
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

# Set up logging for clarity
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- Flask Web Server and Ping Service ---
flask_app = Flask(__name__)

# Environment variables
PORT = int(os.getenv("PORT", "5000"))
RENDER_EXTERNAL_HOSTNAME = os.getenv("RENDER_EXTERNAL_HOSTNAME")

@flask_app.route('/')
def home():
    """
    Renders a simple HTML page to confirm the bot's web server is running.
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
            <h1>Gemini Bot is running! ✅</h1>
            <p>This page confirms that the bot's web server is active.</p>
        </div>
    </body>
    </html>
    """
    return render_template_string(html_content)

def ping_service():
    """
    Periodically pings the bot's external hostname to keep the web service awake.
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

# --- Telegram Bot Handlers and Logic ---

# এপিআই কী এবং অ্যাডমিন আইডি সেট করুন
TELEGRAM_BOT_TOKEN = "YOUR_TELEGRAM_BOT_TOKEN"
GEMINI_API_KEY = "YOUR_GEMINI_API_KEY"
ADMIN_TELEGRAM_ID = YOUR_ADMIN_TELEGRAM_ID # আপনার আইডি এখানে সংখ্যা হিসেবে দিন, স্ট্রিং নয়

# জেমিনি মডেল কনফিগার করুন
genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel('gemini-pro')

# অ্যাডমিন-অনলি ফাংশন ডেকোরেটর
def admin_only(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        if user_id == ADMIN_TELEGRAM_ID:
            await func(update, context)
        else:
            await update.message.reply_text("দুঃখিত, এই কমান্ডটি শুধুমাত্র অ্যাডমিনের জন্য।")
    return wrapper

# /start কমান্ড হ্যান্ডলার
@admin_only
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("হ্যালো! আমি একটি জেমিনি-পাওয়ার্ড বট। আপনি আমাকে প্রশ্ন করতে পারেন।")

# মেসেজ হ্যান্ডলার (প্রশ্ন উত্তর দেওয়ার জন্য)
@admin_only
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_query = update.message.text
    try:
        response = model.generate_content(user_query)
        await update.message.reply_text(response.text)
    except Exception as e:
        await update.message.reply_text(f"একটি ত্রুটি ঘটেছে: {e}")

# প্রধান ফাংশন
def main_bot() -> None:
    """Starts the Telegram bot's polling."""
    # অ্যাপ্লিকেশন তৈরি করুন
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    # কমান্ড হ্যান্ডলার যোগ করুন
    application.add_handler(CommandHandler("start", start))

    # মেসেজ হ্যান্ডলার যোগ করুন
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # পোলিং শুরু করুন
    application.run_polling(allowed_updates=Update.ALL_TYPES)

def start_all_services():
    """Starts both the Flask server and the Telegram bot in separate threads."""
    bot_thread = threading.Thread(target=main_bot)
    bot_thread.start()
    
    # Start the Flask web server and ping service
    run_flask_and_ping()

if __name__ == "__main__":
    start_all_services()
