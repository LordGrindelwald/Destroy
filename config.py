import os
import logging
from logging.handlers import TimedRotatingFileHandler
from pymongo import MongoClient
from dotenv import load_dotenv
from cryptography.fernet import Fernet

# --- Basic Setup & Configuration ---
load_dotenv()

# Configure Logging with Rotation (5 Minutes)
log_formatter = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
root_logger = logging.getLogger()
root_logger.setLevel(logging.INFO)

LOG_FILE_PATH = "app.log"
file_handler = TimedRotatingFileHandler(LOG_FILE_PATH, when="S", interval=300, backupCount=1)
file_handler.setFormatter(log_formatter)
root_logger.addHandler(file_handler)

console_handler = logging.StreamHandler()
console_handler.setFormatter(log_formatter)
root_logger.addHandler(console_handler)

logger = logging.getLogger(__name__)
logger.info("Logging configured with 5-minute rotation for 'app.log'.")


# --- Environment Variables ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
MONGO_URI = os.getenv("MONGO_URI")
OWNER_ID = int(os.getenv("OWNER_ID"))
ENCRYPTION_KEY = os.getenv("ENCRYPTION_KEY")

# --- Encryption Suite ---
cipher_suite = None
if ENCRYPTION_KEY:
    try:
        cipher_suite = Fernet(ENCRYPTION_KEY)
        logger.info("✅ Encryption enabled.")
    except Exception as e:
        logger.critical(f"❌ Invalid ENCRYPTION_KEY: {e}")
else:
    logger.warning("⚠️ No ENCRYPTION_KEY found! Data will be stored in PLAIN TEXT.")


# --- Hardcoded Telegram Desktop Values ---
TD_API_ID = 2040
TD_API_HASH = "b18441a1ff607e10a989891a5462e627"
TD_SYSTEM_VERSION = "Windows 11"
TD_APP_VERSION = "5.2.2 x64"
TD_LANG_CODE = "en"
TD_SYSTEM_LANG_CODE = "en-US"
TD_LANG_PACK = "tdesktop"

# --- Database & In-Memory State ---
try:
    client = MongoClient(
        MONGO_URI, 
        serverSelectionTimeoutMS=5000, 
        connectTimeoutMS=5000, 
        socketTimeoutMS=5000
    )
    # Trigger a connection check
    client.server_info() 
    
    db = client.userbot_manager
    config_collection = db.config
    accounts_collection = db.accounts
    
    # Ensure indexes
    try:
        accounts_collection.create_index("user_id", unique=True)
        # Unique name index (sparse allows nulls, but we usually enforce names)
        accounts_collection.create_index("unique_name", unique=True, sparse=True)
        logger.info("Successfully connected to MongoDB and verified/created indexes.")
    except Exception as e:
        logger.warning(f"Could not create/verify indexes: {e}")

except Exception as e:
    logger.error(f"Failed to connect to MongoDB: {e}")
    client = None
    db = None
    config_collection = None
    accounts_collection = None

# Global state
active_userbots = {}
paused_forwarding = set()
paused_notifications = set()

# --- State definitions for ConversationHandler ---
# Kept here for global reference if needed, though mostly used in handlers/generator
UNIQUE_NAME_GEN, PHONE, CODE, PASSWORD = range(4)
UNIQUE_NAME_PASTE, AWAIT_STRING_PASTE = range(4, 6)
AWAIT_BUTTON, SELECT_ACCOUNTS, AWAIT_INTERVAL = range(6, 9)
AWAIT_BUTTON_REMOVE, SELECT_ACCOUNTS_REMOVE, AWAIT_CONFIRM_REMOVE = range(9, 12)
AWAIT_BUTTON_2FA, SELECT_ACCOUNTS_2FA, AWAIT_DELAY_2FA, AWAIT_PASSWORD_2FA, AWAIT_HINT_2FA, AWAIT_CURRENT_2FA_PASSWORD = range(12, 18)
QR_LOGIN = 18
