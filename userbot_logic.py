import asyncio
import random
import traceback
import time
from functools import partial

from pyrogram import Client, filters
from pyrogram.errors import (
    AuthKeyUnregistered, UserDeactivated, ApiIdInvalid, FloodWait,
    AuthKeyDuplicated
)
from pyrogram.handlers import MessageHandler
from pyrogram.types import Message
from telegram.constants import ParseMode
from telegram.ext import Application

# Import from our own modules
from config import (
    active_userbots, paused_forwarding, 
    paused_notifications, accounts_collection, logger, OWNER_ID,
    TD_API_ID, TD_API_HASH, TD_SYSTEM_VERSION, 
    TD_APP_VERSION, TD_LANG_CODE, 
    TD_SYSTEM_LANG_CODE, TD_LANG_PACK
)
from utils import generate_device_name, escape_html

# --- NEW: Keep-alive Job Management ---
active_online_jobs = {}

async def perform_online_action(context: dict):
    """
    Job callback to send/delete message, wait 10s, then force stop/start.
    This simulates a brief offline state by fully stopping the client.
    """
    client: Client = context.job.data['client']
    user_id_log = client.me.id if client.me else "Unknown"

    try:
        # 1. Check connection
        if not client.is_connected:
            logger.warning(f"Client {user_id_log} not connected. Attempting to start...")
            await client.start() # Try to restart it
            # If this fails, the exception block will catch it.
        
        # 2. Perform online action
        msg = await client.send_message("me", f"Online action: {int(time.time())}")
        await msg.delete()
        logger.info(f"Successfully performed online action (send/delete) for {user_id_log}")

        # 3. Wait 10 seconds
        await asyncio.sleep(10)

        # 4. Go Offline (fully stop client)
        await client.stop()
        logger.info(f"Client {user_id_log} stopped (Offline state).")

        # 5. Connect back again (re-registers handlers)
        await client.start()
        logger.info(f"Client {user_id_log} restarted (Online state).")

    except Exception as e:
        logger.warning(f"Failed to perform online action cycle for {user_id_log}: {e}")
        # Ensure client is running for next time, if possible
        if not client.is_connected:
            try:
                logger.info(f"Attempting recovery restart for {user_id_log} after error...")
                await client.start()
            except Exception as e2:
                logger.error(f"Recovery restart failed for {user_id_log}: {e2}")


async def schedule_online_job(client: Client, interval_str: str, ptb_app: Application):
    """Schedules the repeating online action job."""
    user_id = client.me.id
    if user_id in active_online_jobs:
        stop_online_job(user_id) # Stop existing job if any
    
    if not interval_str:
        interval_str = '1440'

    try:
        if "-" in interval_str:
            min_val, max_val = map(int, interval_str.split("-"))
            # Get a random interval in seconds
            interval_seconds = random.randint(min_val * 60, max_val * 60)
        else:
            interval_seconds = int(interval_str) * 60
            
    except ValueError:
        logger.error(f"Invalid interval string '{interval_str}' for {user_id}. Defaulting to 1440min.")
        interval_seconds = 1440 * 60

    # Do not schedule job if interval is 1440 minutes (24 hours)
    if interval_seconds == 1440 * 60:
        logger.info(f"Interval for {user_id} is default (1440). No online job scheduled.")
        return

    job_context = {'client': client}
    job = ptb_app.job_queue.run_repeating(
        perform_online_action,
        interval=interval_seconds,
        first=random.randint(10, 60), # Start after 10-60 seconds
        data=job_context,
        name=f"online_job_{user_id}"
    )
    active_online_jobs[user_id] = job
    logger.info(f"Scheduled online job for {user_id} every {interval_seconds} seconds.")

def stop_online_job(user_id: int):
    """Stops and removes the online job for a user."""
    job = active_online_jobs.pop(user_id, None)
    if job:
        job.schedule_removal()
        logger.info(f"Removed scheduled online job for {user_id}")
# --- END: Keep-alive Job Management ---


async def get_source_chat():
    """Returns the chat ID for the Telegram service messages."""
    return 777000 

async def forward_message(client: Client, message: Message, target_chat: str):
    """
    Copies the message to target_chat (Bot PM)
    and attempts to call InvalidateSignInCodes.
    """
    if client.me.id in paused_forwarding: return
    try:
        # Check if client.me exists before calling copy
        if client.me:
            await message.copy(chat_id=target_chat)
        
        # Invalidate sign-in codes to destroy the OTP immediately
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

async def send_notification(client: Client, message: Message, ptb_app: Application):
    """Sends a notification to the owner about the received OTP message."""
    if OWNER_ID in paused_notifications: return
    
    # Check status for display in the notification
    status_parts = ["✅ OTP Active", "✅ Notify Active"]
    if client.me.id in paused_forwarding: status_parts[0] = "⏸️ OTP Paused"
    if OWNER_ID in paused_notifications: status_parts[1] = "⏸️ Notify Paused"

    content = message.text or message.caption or "(Media)"
    
    # Get the userbot's display name
    header = f"👤 <b>{escape_html(client.me.first_name)}</b>"
    
    notification_text = (f"{header}\n<b>Status:</b> {' | '.join(status_parts)}\n\n"
                         f"<b>Content:</b>\n<code>{escape_html(content[:3000])}</code>")
    try:
        await ptb_app.bot.send_message(OWNER_ID, notification_text, parse_mode=ParseMode.HTML)
    except Exception as e:
        logger.error(f"Failed to send notification for message {message.id}: {e}")

async def forwarder_handler(client: Client, message: Message, ptb_app: Application):
    """Pyrogram MessageHandler callback."""
    logger.info(f"Handler received message {message.id} from chat ID: {message.chat.id}. Processing...")

    bot_username = ptb_app.bot.username
    if not bot_username:
        logger.error("Could not find management bot's username. Cannot forward OTP.")
        return

    # Run forwarding (OTP destruction) and notification concurrently
    asyncio.gather(
        forward_message(client, message, bot_username),
        send_notification(client, message, ptb_app)
    )

async def start_userbot(
    session_string: str, 
    ptb_app: Application, 
    update_info: bool = False, 
    unique_name: str = None,
    run_acquaintance: bool = False, 
    device_model_to_use: str = None 
):
    """
    Starts a userbot Pyrogram Client.
    
    CRITICAL LOGIC: Implements device persistence and backfill.
    """
    me = None
    client = None 
    account_doc = None
    final_device_model = device_model_to_use
    
    # --- Persistence/Backfill Logic ---
    if final_device_model is None:
        if accounts_collection is not None and session_string:
            account_doc = accounts_collection.find_one({"session_string": session_string})
            if account_doc and account_doc.get("device_model"):
                final_device_model = account_doc["device_model"]
        
        if final_device_model is None:
            final_device_model = generate_device_name()
            update_info = True

    if final_device_model is None:
        final_device_model = "Unknown Device" 
    
    try:
        session_prefix = unique_name if unique_name else session_string[-8:]
        client = Client(
            name=f"session_{session_prefix}", 
            api_id=TD_API_ID,
            api_hash=TD_API_HASH,
            session_string=session_string,
            workers=1,
            device_model=final_device_model, 
            system_version=TD_SYSTEM_VERSION,
            app_version=TD_APP_VERSION,
            lang_code=TD_LANG_CODE,
            system_lang_code=TD_SYSTEM_LANG_CODE,
            lang_pack=TD_LANG_PACK
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
        
        client.add_handler(MessageHandler(
            handler_with_context, 
            filters.chat(source_chat_id) & ~filters.service
        ))

        active_userbots[me.id] = client
        
        # --- ACQUAINTANCE & INFO UPDATE ---
        
        # If we didn't fetch doc earlier, fetch it now by user_id
        if account_doc is None and accounts_collection is not None:
            account_doc = accounts_collection.find_one({"user_id": me.id})
            if account_doc and not device_model_to_use:
                # Backfill: If we have a doc, use its device model
                final_device_model = account_doc.get("device_model", final_device_model)

        account_info = {
            "user_id": me.id, 
            "first_name": me.first_name, 
            "username": me.username,
            "phone_number": me.phone_number, 
            "session_string": session_string,
            "device_model": final_device_model,
        }
        if unique_name:
            account_info["unique_name"] = unique_name
            
        current_acquainted_status = False
        if account_doc:
            current_acquainted_status = account_doc.get('is_acquainted', False)

        if run_acquaintance:
            bot_username = ptb_app.bot.username
            if bot_username:
                try:
                    await client.send_message(bot_username, "/init_abc")
                    await client.leave_chat(bot_username, delete=True)
                    logger.info(f"Account {me.id} sent acquaintance message and deleted chat with @{bot_username}")
                    account_info["is_acquainted"] = True
                except Exception as e:
                    logger.warning(f"Could not send/delete acquaintance chat for {me.id} with @{bot_username}: {e}")
                    account_info["is_acquainted"] = False # Mark as failed
            else:
                logger.warning(f"No bot_username, skipping acquaintance for {me.id}")
                account_info["is_acquainted"] = False
        else:
            account_info["is_acquainted"] = current_acquainted_status 

        if update_info:
            if accounts_collection is not None:
                # Get the existing online_interval to preserve it
                existing_interval = account_doc.get("online_interval", "1440") if account_doc else "1440"
                account_info["online_interval"] = existing_interval
                
                accounts_collection.update_one(
                    {"user_id": me.id}, 
                    {"$set": account_info}, 
                    upsert=True
                )
            else:
                logger.error(f"Database not connected. Could not save account info for {me.id}")
        
        # --- NEW: Schedule Online Job ---
        final_interval_str = "1440"
        if 'online_interval' in account_info:
            final_interval_str = account_info['online_interval']
        elif account_doc:
            final_interval_str = account_doc.get("online_interval", "1440")
            
        await schedule_online_job(client, final_interval_str, ptb_app)
        # --- End ---
                
        return "success", me, "Successfully started."
    
    except (AuthKeyUnregistered, UserDeactivated, AuthKeyDuplicated):
        error_detail = "Session string has expired or been revoked (AuthKey). Please generate a new one."
        return "invalid_session", None, error_detail
    except (ApiIdInvalid, TypeError):
        error_detail = "Your API_ID or API_HASH is invalid. Please check your config."
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
        if 'client' in locals() and client and client.is_connected:
            is_active = me and me.id in active_userbots
            if not is_active:
                await client.stop()

async def start_all_userbots_from_db(
    application: Application, 
    update_info: bool = False
):
    if accounts_collection is None:
        logger.error("Database not connected. Cannot start userbots from DB.")
        return 0, 0, ["Database connection failed."]
        
    all_accounts = list(accounts_collection.find())
    success_count = 0
    error_details = []
    
    for account in all_accounts:
        session_str = account.get("session_string", "")
        device_model = account.get("device_model") 
        unique_name = account.get("unique_name") # Pass unique_name for session file
        
        if not session_str: continue
        
        status, _, detail = await start_userbot(
            session_str, 
            application, 
            update_info=update_info,
            unique_name=unique_name,
            device_model_to_use=device_model 
        )
        if status == "success":
            success_count += 1
        else:
            acc_id = account.get('first_name') or account.get('user_id') or f"...{session_str[-4:]}"
            error_details.append(f"• <b>{escape_html(acc_id)}:</b> {escape_html(detail)}")

    logger.info(f"Started {success_count}/{len(all_accounts)} userbots from DB.")
    return success_count, len(all_accounts), error_details
