import os
import logging
from logging.handlers import TimedRotatingFileHandler
from pymongo import MongoClient
from dotenv import load_dotenv

# --- Basic Setup & Configuration ---
load_dotenv()

# Configure Logging with Rotation (5 Minutes)
# This prevents the log file from growing indefinitely.
log_formatter = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
root_logger = logging.getLogger()
root_logger.setLevel(logging.INFO)

# Rotate every 300 seconds (5 minutes). 
# backupCount=1 keeps one backup file, ensuring we don't store logs older than ~5-10 mins.
LOG_FILE_PATH = "app.log"
file_handler = TimedRotatingFileHandler(LOG_FILE_PATH, when="S", interval=300, backupCount=1)
file_handler.setFormatter(log_formatter)
root_logger.addHandler(file_handler)

# Console handler for Docker/VPS logs
console_handler = logging.StreamHandler()
console_handler.setFormatter(log_formatter)
root_logger.addHandler(console_handler)

logger = logging.getLogger(__name__)
logger.info("Logging configured with 5-minute rotation for 'app.log'.")


# --- Environment Variables ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
MONGO_URI = os.getenv("MONGO_URI")
OWNER_ID = int(os.getenv("OWNER_ID"))

# --- Hardcoded Telegram Desktop Values with exact names ---
TD_API_ID = 2040
TD_API_HASH = "b18441a1ff607e10a989891a5462e627"
TD_SYSTEM_VERSION = "Windows 11"
TD_APP_VERSION = "5.2.2 x64"
TD_LANG_CODE = "en"
TD_SYSTEM_LANG_CODE = "en-US"
TD_LANG_PACK = "tdesktop"

# --- Database & In-Memory State ---
try:
    # Set timeouts to prevent hangs on dead connections
    client = MongoClient(
        MONGO_URI, 
        serverSelectionTimeoutMS=5000, 
        connectTimeoutMS=5000, 
        socketTimeoutMS=5000
    )
    # Force a connection check to catch errors *now*
    client.server_info() 
    
    db = client.userbot_manager
    config_collection = db.config
    accounts_collection = db.accounts
    
    # --- FIX: Ensure database indexes to prevent duplicates ---
    try:
        accounts_collection.create_index("user_id", unique=True)
        accounts_collection.create_index("unique_name", unique=True, sparse=True)
        logger.info("Successfully connected to MongoDB and verified/created indexes.")
    except Exception as e:
        logger.warning(f"Could not create/verify indexes: {e}")
    # --- END FIX ---

except Exception as e:
    logger.error(f"Failed to connect to MongoDB: {e}")
    client = None
    db = None
    config_collection = None
    accounts_collection = None

active_userbots = {}
paused_forwarding = set()
paused_notifications = set()

# --- State definitions for ConversationHandler ---
# Session Generator
UNIQUE_NAME_GEN, PHONE, CODE, PASSWORD = range(4)

# Paste String
UNIQUE_NAME_PASTE, AWAIT_STRING_PASTE = range(4, 6)

# Online Interval
AWAIT_BUTTON, SELECT_ACCOUNTS, AWAIT_INTERVAL = range(6, 9)

# --- Remove Conversation ---
AWAIT_BUTTON_REMOVE, SELECT_ACCOUNTS_REMOVE, AWAIT_CONFIRM_REMOVE = range(9, 12)

# --- 2FA Conversation ---
AWAIT_BUTTON_2FA, SELECT_ACCOUNTS_2FA, AWAIT_DELAY_2FA, AWAIT_PASSWORD_2FA, AWAIT_HINT_2FA = range(12, 17)
