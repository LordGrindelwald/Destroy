import re
import random
from functools import wraps
from telegram import Update
from telegram.ext import ContextTypes

# Import OWNER_ID from config
from config import OWNER_ID

def escape_html(text: str) -> str:
    """Escapes special characters for Telegram HTML parsing."""
    if not isinstance(text, str): text = str(text)
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

def clean_session_string(session_string: str) -> str:
    """Thoroughly cleans the session string."""
    return re.sub(r'[\s\x00-\x1f\x7f-\x9f]', '', session_string)

def generate_device_name():
    """Generates a realistic device name from a predefined list."""
    device_names = [
        "MSI B550", "Asus ROG Strix Z690E", "Gigabyte Aorus Master",
        "XPS Desktop", "Hp Pavilion Plus", "Lenovo Legion Tower", "Aurora R13"
    ]
    return random.choice(device_names)

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
