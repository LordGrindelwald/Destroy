import re
import random
import os
from functools import wraps
from telegram import Update
from telegram.ext import ContextTypes, ConversationHandler, CommandHandler

# Import from config
from config import OWNER_ID, accounts_collection, logger

# --- NEW FUNCTION ---
def parse_interval(interval_str: str) -> int:
    """
    Parses an interval string ("1440" or "30-90") and returns
    a sleep duration in seconds.
    """
    try:
        if "-" in interval_str:
            min_str, max_str = interval_str.split("-")
            min_val = int(min_str)
            max_val = int(max_str)
            if min_val <= max_val:
                # Return a random value in the range, converted to seconds
                return random.randint(min_val, max_val) * 60
        else:
            # Return the static value, converted to seconds
            return int(interval_str) * 60
    except Exception as e:
        logger.warning(f"Invalid interval string '{interval_str}', defaulting to 1440 mins. Error: {e}")
    
    # Default: 1440 minutes (24 hours)
    return 1440 * 60
# --- END NEW FUNCTION ---


def escape_html(text: str) -> str:
    """Escapes special characters for Telegram HTML parsing."""
    if not isinstance(text, str): text = str(text)
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

def clean_session_string(session_string: str) -> str:
    """Thoroughly cleans the session string."""
    return re.sub(r'[\s\x00-\x1f\x7f-\x9f]', '', session_string)

def _load_device_names():
    """Loads device names from the external file or uses a hardcoded fallback."""
    try:
        # Assumes device_win11 is in the same directory as utils.py (i.e., /app)
        filepath = os.path.join(os.path.dirname(__file__), 'device_win11')
        with open(filepath, 'r') as f:
            names = [line.strip() for line in f if line.strip()]
        if names:
            return names
        else:
            logger.warning("device_win11 file was empty. Using fallback list.")
    except Exception as e:
        logger.error(f"Failed to load device names from file: {e}. Using hardcoded fallback.")
    
    # Hardcoded fallback list (must match the content of device_win11)
    return [
        "MSI B550", "Asus ROG Strix Z690E", "Gigabyte Aorus Master",
        "XPS Desktop", "Hp Pavilion Plus", "Lenovo Legion Tower", "Aurora R13"
    ]

# Load names once when the module is imported
DEVICE_NAMES = _load_device_names()

def generate_device_name():
    """Selects a realistic device name from the loaded list."""
    if not DEVICE_NAMES:
        return "Unknown Desktop"
    return random.choice(DEVICE_NAMES)

# --- Decorator for Owner-Only Access ---
def owner_only(func):
    @wraps(func)
    async def wrapped(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        user_id = update.effective_user.id
        if user_id != OWNER_ID:
            if update.callback_query:
                await update.callback_query.answer("⛔️ You are not authorized for this action.", show_alert=True)
            else:
                await update.message.reply_text("⛔️ You are not authorized for this action.")
            return
        return await func(update, context, *args, **kwargs)
    return wrapped

# --- Helper Function (with the correct bug fix) ---
async def get_account_from_arg(arg: str):
    """
    Finds an account by its user_id or unique_name.
    Returns the full account document from MongoDB.
    """
    if accounts_collection is None:
        return None
        
    account = None
    try:
        # Try to find by user_id first
        user_id = int(arg)
        account = accounts_collection.find_one({"user_id": user_id})
    except ValueError:
        # If not an int, it must be a unique_name
        pass
    
    if account is None:
        # Search by lowercase unique_name
        account = accounts_collection.find_one({"unique_name": arg.lower()})
        
    return account


# --- BUGFIX: Conversation Fallback ---
async def end_conversation_on_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    Generic fallback handler to end any active conversation
    if a new command is received.
    """
    logger.info("New command received, ending active conversation.")
    context.user_data.clear()
    if update.message:
        await update.message.reply_text("✖️ Previous action cancelled by new command. Please send your command again.")
    return ConversationHandler.END

# This list will be shared with session_generator.py
COMMAND_FALLBACKS = [
    CommandHandler("start", end_conversation_on_command),
    CommandHandler("settings", end_conversation_on_command),
    CommandHandler("add", end_conversation_on_command),
    CommandHandler("remove", end_conversation_on_command),
    CommandHandler("rename", end_conversation_on_command),
    CommandHandler("status", end_conversation_on_command),
    CommandHandler("temp", end_conversation_on_command),
    CommandHandler("temp_fwd", end_conversation_on_command),
    CommandHandler("ping", end_conversation_on_command),
    CommandHandler("refresh", end_conversation_on_command),
    CommandHandler("accs", end_conversation_on_command),
    CommandHandler("acc", end_conversation_on_command),
    CommandHandler("toggle_otp_destroy", end_conversation_on_command),
    CommandHandler("restart", end_conversation_on_command),
    CommandHandler("deduplicate_db", end_conversation_on_command),
    CommandHandler("online_interval", end_conversation_on_command),
]
# --- END BUGFIX ---
