import os
import logging
import threading
import subprocess
import requests
import time
from flask import Flask, render_template_string
from telegram import Update
from telegram.ext import (
    Application,
    MessageHandler,
    CommandHandler,
    filters,
    ContextTypes,
)

# --- Configuration and Environment Variables ---
# Set up logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# Load environment variables
BOT_TOKEN = os.getenv("BOT_TOKEN")
API_ID = os.getenv("API_ID")
API_HASH = os.getenv("API_HASH")
ADMIN_ID = os.getenv("ADMIN_ID") # Admin's Telegram User ID (as a string)

# Check for required environment variables
if not all([BOT_TOKEN, API_ID, API_HASH, ADMIN_ID]):
    logger.error("Missing required environment variables. Please set BOT_TOKEN, API_ID, API_HASH, and ADMIN_ID.")
    exit()

# --- Flask Web Server and Ping Service ---
flask_app = Flask(__name__)
PORT = int(os.getenv("PORT", "5000"))
RENDER_EXTERNAL_HOSTNAME = os.getenv("RENDER_EXTERNAL_HOSTNAME")

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
            body { font-family: Arial, sans-serif; background-color: #f0f2f5; color: #333; text-align: center; padding-top: 50px; }
            .container { background-color: #fff; padding: 30px; border-radius: 10px; box-shadow: 0 4px 8px rgba(0,0,0,0.1); display: inline-block; }
            h1 { color: #28a745; }
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
        time.sleep(600)  # Ping every 10 minutes

def run_flask_and_ping():
    flask_thread = threading.Thread(target=lambda: flask_app.run(host="0.0.0.0", port=PORT, use_reloader=False))
    flask_thread.start()
    
    ping_thread = threading.Thread(target=ping_service)
    ping_thread.start()
    logger.info("Flask and Ping services started.")

# --- Telegram Bot Logic ---
# State for video conversions
user_states = {}

def is_admin(update: Update):
    """Checks if the user is the admin."""
    return str(update.effective_user.id) == ADMIN_ID

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("Hello! I am a video conversion bot. I only work for admins.")

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update):
        await update.message.reply_text("This command is for admins only.")
        return

    await update.message.reply_text(
        "Available Commands:\n"
        "/video_convert [width]x[height] - Reduce video size (e.g., /video_convert 640x480)\n"
        "/format_change [format] - Change video format (e.g., /format_change gif)"
    )

async def video_convert_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update):
        await update.message.reply_text("This command is for admins only.")
        return

    if not context.args:
        await update.message.reply_text("Please provide the new resolution (e.g., /video_convert 640x480).")
        return

    resolution = context.args[0]
    user_id = str(update.effective_user.id)
    user_states[user_id] = {"action": "video_convert", "value": resolution}

    await update.message.reply_text(
        f"Send me the video you want to convert to {resolution}. I will process it."
    )

async def format_change_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update):
        await update.message.reply_text("This command is for admins only.")
        return

    if not context.args:
        await update.message.reply_text("Please provide the new format (e.g., /format_change gif).")
        return

    new_format = context.args[0]
    user_id = str(update.effective_user.id)
    user_states[user_id] = {"action": "format_change", "value": new_format}
    
    await update.message.reply_text(
        f"Send me the video you want to change to .{new_format}. I will process it."
    )

async def handle_video(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update):
        await update.message.reply_text("This bot only works for admins.")
        return

    user_id = str(update.effective_user.id)
    if user_id not in user_states:
        await update.message.reply_text("Please select a command first, like /video_convert or /format_change.")
        return

    action = user_states[user_id]["action"]
    value = user_states[user_id]["value"]

    await update.message.reply_text("Video received! Please wait, converting...")

    file_id = update.message.video.file_id
    new_file = await context.bot.get_file(file_id)
    
    input_path = f"input_{file_id}.mp4"
    
    try:
        await new_file.download_to_drive(input_path)
        logger.info(f"Video downloaded to {input_path}")

        ffmpeg_command = ["ffmpeg", "-i", input_path]
        output_path = f"output_{file_id}"
        output_format = "mp4" # default output format

        if action == "video_convert":
            ffmpeg_command.extend(["-vf", f"scale={value}"])
            output_path += ".mp4"
            
        elif action == "format_change":
            output_path += f".{value}"
            output_format = value
        
        ffmpeg_command.append(output_path)
        
        subprocess.run(ffmpeg_command, check=True)
        logger.info(f"Video converted to {output_path}")

        if output_format == "gif":
             await context.bot.send_animation(
                chat_id=update.effective_chat.id,
                animation=open(output_path, "rb"),
                caption="Here is your converted file!"
            )
        else:
            await context.bot.send_video(
                chat_id=update.effective_chat.id,
                video=open(output_path, "rb"),
                caption="Here is your converted video!"
            )
        logger.info("Converted video sent to user.")
        
    except Exception as e:
        logger.error(f"An error occurred: {e}")
        await update.message.reply_text("Sorry, an error occurred during conversion.")
    finally:
        if os.path.exists(input_path):
            os.remove(input_path)
        if os.path.exists(output_path):
            os.remove(output_path)
        user_states.pop(user_id, None)
        logger.info("Temporary files and user state cleaned up.")

def main() -> None:
    """Start the bot."""
    run_flask_and_ping()

    application = Application.builder().token(BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("video_convert", video_convert_command))
    application.add_handler(CommandHandler("format_change", format_change_command))
    application.add_handler(MessageHandler(filters.VIDEO, handle_video))

    application.run_polling()
    logger.info("Bot started and is listening for messages...")

if __name__ == "__main__":
    main()
