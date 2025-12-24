import re
import random
import os
from functools import wraps
from telegram import Update
from telegram.ext import ContextTypes, ConversationHandler, CommandHandler

# Import from config
from config import OWNER_ID, accounts_collection, logger, cipher_suite

# --- Encryption Helpers ---
def encrypt_text(text: str) -> str:
    """Encrypts a string. Returns the original text if encryption is disabled."""
    if not text or not cipher_suite:
        return text
    try:
        return cipher_suite.encrypt(text.encode()).decode()
    except Exception as e:
        logger.error(f"Encryption failed: {e}")
        return text

def decrypt_text(text: str) -> str:
    """Decrypts a string. Returns original text if it's not encrypted or fails."""
    if not text or not cipher_suite:
        return text
    try:
        # Fernet tokens usually start with gAAAAA
        if not text.startswith("gAAAAA"): 
            return text
        return cipher_suite.decrypt(text.encode()).decode()
    except Exception as e:
        logger.error(f"Decryption failed: {e}")
        return text
# --- End Encryption Helpers ---

def sanitize_unique_name(name: str) -> str:
    if not name:
        return f"unnamed{random.randint(1000,9999)}"
    # Allow alphanumeric only
    clean = re.sub(r'[^a-zA-Z0-9]', '', name).lower()
    if not clean:
        return f"user{random.randint(1000,9999)}"
    return clean

def parse_interval(interval_str: str) -> int:
    try:
        if "-" in interval_str:
            min_str, max_str = interval_str.split("-")
            min_val = int(min_str)
            max_val = int(max_str)
            if min_val <= max_val:
                return random.randint(min_val, max_val) * 60
        else:
            return int(interval_str) * 60
    except Exception as e:
        logger.warning(f"Invalid interval string '{interval_str}', defaulting to 1440 mins. Error: {e}")
    # Default 24 hours
    return 1440 * 60

def escape_html(text: str) -> str:
    if not isinstance(text, str): text = str(text)
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

def clean_session_string(session_string: str) -> str:
    """Removes spaces, newlines, and invisible characters."""
    return re.sub(r'[\s\x00-\x1f\x7f-\x9f]', '', session_string)

def _load_device_names():
    try:
        filepath = os.path.join(os.path.dirname(__file__), 'device_win11')
        with open(filepath, 'r') as f:
            names = [line.strip() for line in f if line.strip()]
        if names: return names
        else: logger.warning("device_win11 file was empty. Using fallback list.")
    except Exception as e:
        logger.error(f"Failed to load device names from file: {e}. Using hardcoded fallback.")
    
    return [
        "MSI B550", "Asus ROG Strix Z690E", "Gigabyte Aorus Master",
        "XPS Desktop", "Hp Pavilion Plus", "Lenovo Legion Tower", "Aurora R13"
    ]

DEVICE_NAMES = _load_device_names()

def generate_device_name():
    if not DEVICE_NAMES: return "Unknown Desktop"
    return random.choice(DEVICE_NAMES)

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

async def get_account_from_arg(arg: str):
    """
    Tries to find an account by User ID (int) first, then by Unique Name (str).
    Returns the document or None.
    """
    if accounts_collection is None: return None
    
    account = None
    # 1. Try ID
    try:
        user_id = int(arg)
        account = accounts_collection.find_one({"user_id": user_id})
    except ValueError:
        pass
    
    # 2. Try Name
    if account is None:
        clean_arg = sanitize_unique_name(arg)
        account = accounts_collection.find_one({"unique_name": clean_arg})
        
    return account

async def end_conversation_on_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    Universal fallback function that clears user_data and ends any active conversation
    when a new command (like /start, /help) is issued.
    """
    logger.info("New command received, ending active conversation.")
    context.user_data.clear()
    if update.message:
        await update.message.reply_text("✖️ Previous action cancelled by new command. Please send your command again.")
    return ConversationHandler.END

# Universal list of fallbacks to apply to ALL ConversationHandlers
COMMAND_FALLBACKS = [
    CommandHandler("start", end_conversation_on_command),
    CommandHandler("settings", end_conversation_on_command),
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
    CommandHandler("backup", end_conversation_on_command),
    CommandHandler("restore", end_conversation_on_command),
    CommandHandler("encrpast", end_conversation_on_command),
]
