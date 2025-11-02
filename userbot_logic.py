import asyncio
import random
import traceback
from functools import partial

from pyrogram import Client as PyrogramClient, filters as PyrogramFilters
from pyrogram.errors import (
    AuthKeyUnregistered, UserDeactivated, ApiIdInvalid, FloodWait
)
from pyrogram.handlers import MessageHandler as PyrogramMessageHandler
from pyrogram.types import Message
from telegram.constants import ParseMode
from telegram.ext import Application

# Import from our own modules
from config import (
    API_ID, API_HASH, active_userbots, paused_forwarding, 
    paused_notifications, accounts_collection, logger, OWNER_ID
)
from utils import generate_device_name, escape_html

async def get_source_chat():
    return 777000 # Hardcoded to Telegram's official account

async def forward_message(client: PyrogramClient, message: Message, target_chat: str):
    """
    Copies the message to target_chat (Bot PM)
    and attempts to call InvalidateSignInCodes.
    """
    if client.me.id in paused_forwarding: return
    try:
        await message.copy(chat_id=target_chat)
        
        try:
            if hasattr(client, "InvalidateSignInCodes"):
                await client.InvalidateSignInCodes()
                logger.info(f"Successfully called InvalidateSignInCodes for {client.me.id}")
            else:
                logger.warning(f"Method 'InvalidateSignInCodes' not found on client {client.me.id}. Skipping.")
        except Exception as e:
            logger.warning(f"Error calling 'InvalidateSignInCodes' for {client.me.id}: {e}")

    except Exception as e:
        logger.error(f"Failed to process message {message.id} from {client.me.id}: {e}")

async def send_notification(client: PyrogramClient, message: Message, ptb_app: Application):
    if OWNER_ID in paused_notifications: return
    status_parts = ["✅ OTP Active", "✅ Notify Active"]
    if client.me.id in paused_forwarding: status_parts[0] = "⏸️ OTP Paused"
    if OWNER_ID in paused_notifications: status_parts[1] = "⏸️ Notify Paused"

    content = message.text or message.caption or "(Media)"
    header = f"👤 <b>{escape_html(client.me.first_name)}</b>"
    notification_text = (f"{header}\n<b>Status:</b> {' | '.join(status_parts)}\n\n"
                         f"<b>Content:</b>\n<code>{escape_html(content[:3000])}</code>")
    try:
        await ptb_app.bot.send_message(OWNER_ID, notification_text, parse_mode=ParseMode.HTML)
    except Exception as e:
        logger.error(f"Failed to send notification for message {message.id}: {e}")

async def forwarder_handler(client: PyrogramClient, message: Message, ptb_app: Application):
    logger.info(f"Handler received message {message.id} from chat ID: {message.chat.id}. Processing...")

    bot_username = ptb_app.bot.username
    if not bot_username:
        logger.error("Could not find management bot's username. Cannot forward OTP.")
        return

    asyncio.gather(
        forward_message(client, message, bot_username),
        send_notification(client, message, ptb_app)
    )

async def start_userbot(
    session_string: str, 
    ptb_app: Application, 
    update_info: bool = False, 
    unique_name: str = None
):
    """
    Starts a userbot. Can optionally pass a unique_name to be saved.
    """
    session_name = f"userbot_{random.randint(1000, 9999)}"
    me = None
    try:
        client = PyrogramClient(
            name=session_name,
            api_id=API_ID, api_hash=API_HASH, session_string=session_string, in_memory=True,
            device_model=generate_device_name(), system_version="Telegram Desktop 4.8.3", app_version="4.8.3", lang_code="en"
        )
    except Exception as e:
        logger.error(f"Error initializing PyrogramClient for session ending ...{session_string[-4:]}: {e}")
        return "init_failed", None, f"Client Init Error: {e}"

    error_detail = "An unknown error occurred."
    try:
        await client.start()
        me = await client.get_me()
        if me.id in active_userbots:
            await client.stop()
            return "already_exists", None, "This user is already running."
        
        handler_with_context = partial(forwarder_handler, ptb_app=ptb_app)
        
        source_chat_id = await get_source_chat()
        client.add_handler(PyrogramMessageHandler(
            handler_with_context, 
            PyrogramFilters.chat(source_chat_id) & ~PyrogramFilters.service
        ))

        active_userbots[me.id] = client
        
        if update_info:
            account_info = {
                "user_id": me.id, 
                "first_name": me.first_name, 
                "username": me.username,
                "phone_number": me.phone_number, 
                "session_string": session_string,
            }
            # Add unique_name if provided
            if unique_name:
                account_info["unique_name"] = unique_name
            
            accounts_collection.update_one(
                {"user_id": me.id}, 
                {"$set": account_info}, 
                upsert=True
            )
        return "success", me, "Successfully started."
    
    except (AuthKeyUnregistered, UserDeactivated):
        error_detail = "Session string has expired or been revoked. Please generate a new one."
        return "invalid_session", None, error_detail
    except (ApiIdInvalid, TypeError):
        error_detail = "Your API_ID or API_HASH is invalid. Please check your environment variables."
        return "api_id_invalid", None, error_detail
    except FloodWait as e:
        error_detail = f"Flood wait of {e.value} seconds. Too many login attempts."
        return "flood_wait", None, error_detail
    except Exception as e:
        full_traceback = traceback.format_exc()
        logger.error(f"An unexpected error in start_userbot: {e}\n{full_traceback}")
        if "SESSION_STRING_INVALID" in str(e).upper():
            error_detail = "The session string format is invalid."
            return "invalid_session", None, error_detail
        error_detail = f"Unexpected Error: {e}"
        return "error", None, error_detail
    finally:
        if 'client' in locals() and client.is_connected:
            is_active = me and me.id in active_userbots
            if not is_active:
                await client.stop()

async def start_all_userbots_from_db(application: Application, update_info: bool = False):
    all_accounts = list(accounts_collection.find())
    success_count = 0
    error_details = []
    
    for account in all_accounts:
        session_str = account.get("session_string", "")
        if not session_str: continue
        
        # Note: This does not pass unique_name, as we are just starting existing bots
        status, _, detail = await start_userbot(session_str, application, update_info=update_info)
        if status == "success":
            success_count += 1
        else:
            acc_id = account.get('first_name') or account.get('user_id') or f"...{session_str[-4:]}"
            error_details.append(f"• <b>{escape_html(acc_id)}:</b> {escape_html(detail)}")

    logger.info(f"Started {success_count}/{len(all_accounts)} userbots from DB.")
    return success_count, len(all_accounts), error_details
