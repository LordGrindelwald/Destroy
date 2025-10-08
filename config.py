import os
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

# --- Essential Configuration ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
MONGO_URI = os.getenv("MONGO_URI")
OWNER_ID = int(os.getenv("OWNER_ID"))
API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")

# --- Bot & Userbot Settings ---
# Spoofed device parameters for safer logins during session generation
SPOOFED_PARAMS = {
    "device_model": "SM-G998B",
    "system_version": "SDK 33",
    "app_version": "9.5.3",
    "lang_code": "en",
    "system_lang_code": "en-US"
}

# --- In-memory State ---
# This dictionary will hold active Pyrogram clients: {user_id: {"client": PyrogramClient, "custom_name": str}}
active_userbots = {}

# --- ConversationHandler States ---
(
    # Interactive Add Conversation
    GET_CUSTOM_NAME, GET_PHONE_NUMBER, GET_PHONE_CODE, GET_2FA_PASSWORD,
    # Add via String Conversation
    GET_STRING_NAME, GET_SESSION_STRING,
    # Menu Selection & Actions
    SELECT_ACCOUNT_SINGLE,
    # Session Termination
    TERMINATE_SESSION_CONFIRM,
    # Account Removal
    REMOVE_ACCOUNT_CONFIRM
) = range(9)
