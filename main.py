import os
import logging
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# --- Basic Setup ---
load_dotenv()
logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# Read the two most important variables
BOT_TOKEN = os.getenv("BOT_TOKEN")
OWNER_ID_FROM_ENV = os.getenv("OWNER_ID")

# --- The Test Command ---
async def test_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """A simple command to diagnose the issue."""
    user_id = update.effective_user.id
    
    # This will be printed in your Koyeb logs
    logger.info(f"Received /test from user ID: {user_id}")
    logger.info(f"OWNER_ID from environment is: {OWNER_ID_FROM_ENV}")

    # This will be sent back to you in Telegram
    await update.message.reply_text(
        f"Hello! Your User ID is:\n`{user_id}`\n\n"
        f"The OWNER_ID set in my environment is:\n`{OWNER_ID_FROM_ENV}`\n\n"
        f"These two numbers must be exactly the same for the main bot to work.",
        parse_mode="Markdown"
    )

def main():
    """Starts a minimal bot for testing."""
    if not BOT_TOKEN:
        logger.error("BOT_TOKEN not found! Please check your environment variables.")
        return
        
    application = Application.builder().token(BOT_TOKEN).build()
    application.add_handler(CommandHandler("test", test_command))
    
    logger.info("Debug bot is starting...")
    application.run_polling()

if __name__ == "__main__":
    main()
