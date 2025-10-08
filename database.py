from pymongo import MongoClient, ASCENDING, errors
import logging
from config import MONGO_URI

logger = logging.getLogger(__name__)

try:
    client = MongoClient(MONGO_URI)
    db = client.userbot_manager
    accounts_collection = db.accounts
    # Create a unique index on the custom_name field to prevent duplicates
    accounts_collection.create_index([("custom_name", ASCENDING)], unique=True)
    logger.info("Successfully connected to MongoDB.")
except errors.ConnectionFailure as e:
    logger.critical(f"Could not connect to MongoDB: {e}. The bot cannot run without a database connection.")
    exit()
except Exception as e:
    logger.critical(f"An unexpected error occurred during database setup: {e}")
    exit()
