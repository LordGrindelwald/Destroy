import os
import logging
from pymongo import MongoClient
from dotenv import load_dotenv

# --- Basic Setup & Configuration ---
load_dotenv()
logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# --- Environment Variables ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
MONGO_URI = os.getenv("MONGO_URI")
OWNER_ID = int(os.getenv("OWNER_ID"))

# --- MODIFIED: Hardcoded Telegram Desktop Values with exact names ---
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
    logger.info("Successfully connected to MongoDB and verified connection.")
except Exception as e:
    logger.error(f"Failed to connect to MongoDB: {e}")
    client = None
    db = None
    config_collection = None
    accounts_collection = None

active_userbots = {}
paused_forwarding = set() # This set now controls OTP processing
paused_notifications = set()

# --- State definitions for ConversationHandler ---
# Session Generator
UNIQUE_NAME_GEN, PHONE, CODE, PASSWORD = range(4)

# Paste String
UNIQUE_NAME_PASTE, AWAIT_STRING_PASTE = range(4, 6)

# Online Interval
AWAIT_BUTTON, SELECT_ACCOUNTS, AWAIT_INTERVAL = range(6, 9)
