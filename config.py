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
API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")

# --- Database & In-Memory State ---
try:
    client = MongoClient(MONGO_URI)
    db = client.userbot_manager
    config_collection = db.config
    accounts_collection = db.accounts
    logger.info("Successfully connected to MongoDB.")
except Exception as e:
    logger.error(f"Failed to connect to MongoDB: {e}")
    # You might want to exit or handle this more gracefully
    client = None
    db = None
    config_collection = None
    accounts_collection = None

active_userbots = {}
paused_forwarding = set() # This set now controls OTP processing
paused_notifications = set()

# --- State definitions for ConversationHandler ---
PHONE, CODE, PASSWORD, ADD_ACCOUNT = range(4)
