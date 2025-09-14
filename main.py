import os
import logging
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
import google.generativeai as genai

# .env ফাইল থেকে ভেরিয়েবল লোড করুন
load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
ADMIN_USER_ID = int(os.getenv("ADMIN_USER_ID"))

# Gemini API কনফিগারেশন
genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel('gemini-pro')

# লগিং সেটআপ
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

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
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    
    # কমান্ড হ্যান্ডলার
    application.add_handler(CommandHandler("start", start))
    
    # মেসেজ হ্যান্ডলার (শুধুমাত্র টেক্সট মেসেজের জন্য)
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    
    # বটটি পোলিং মোডে চালু করা
    logger.info("Bot is running...")
    application.run_polling()

if __name__ == "__main__":
    main()
