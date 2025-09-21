import os
import logging
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ConversationHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# --- Basic Setup ---
load_dotenv()
logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN")
OWNER_ID = int(os.getenv("OWNER_ID", 0))

# --- Conversation States ---
STATE_ONE = range(1)

# --- The Owner Check Wrapper ---
async def owner_only(update: Update, context: ContextTypes.DEFAULT_TYPE, command_handler):
    """A wrapper to restrict command access to the owner."""
    user_id = update.effective_user.id
    logger.info(f"Owner check for user {user_id}. OWNER_ID is {OWNER_ID}.")
    if user_id != OWNER_ID:
        logger.warning(f"Unauthorized access denied for {user_id}.")
        await update.message.reply_text("⛔️ You are not authorized.")
        return
    await command_handler(update, context)

# --- Test Commands ---
async def ping_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """A simple command that anyone can run."""
    logger.info("Executing /ping command.")
    await update.message.reply_text("Pong! (Public)")

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """A simple owner-only command."""
    logger.info("Executing /start command.")
    await update.message.reply_text("Start command works! (Owner only)")

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Another simple owner-only command."""
    logger.info("Executing /status command.")
    await update.message.reply_text("Status command works! (Owner only)")

async def convo_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Starts a simple conversation."""
    logger.info("Executing /convo command.")
    await update.message.reply_text("Conversation started. Send me any message.")
    return STATE_ONE

async def convo_end(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Ends the simple conversation."""
    logger.info("Executing conversation end step.")
    await update.message.reply_text("Conversation ended successfully.")
    return ConversationHandler.END

def main():
    """Starts the minimal debug bot."""
    application = Application.builder().token(BOT_TOKEN).build()

    # Conversation handler for testing
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("convo", lambda u, c: owner_only(u, c, convo_start))],
        states={STATE_ONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, convo_end)]},
        fallbacks=[],
    )

    # Note the difference in how handlers are added
    application.add_handler(CommandHandler("ping", ping_command)) # Public
    application.add_handler(CommandHandler("start", lambda u, c: owner_only(u, c, start_command))) # Owner only
    application.add_handler(CommandHandler("status", lambda u, c: owner_only(u, c, status_command))) # Owner only
    application.add_handler(conv_handler) # Conversation test

    logger.info("DEBUG BOT is starting...")
    application.run_polling()

if __name__ == "__main__":
    main()
