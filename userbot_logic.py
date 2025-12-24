import asyncio
import random
import traceback
import time
import gc 
import re
from functools import partial

from pyrogram import Client, filters
from pyrogram.raw.functions.account import InvalidateSignInCodes
from pyrogram.errors import (
    AuthKeyUnregistered, UserDeactivated, ApiIdInvalid, FloodWait,
    AuthKeyDuplicated
)
from pyrogram.handlers import MessageHandler
from pyrogram.types import Message
from telegram.constants import ParseMode
from telegram.ext import Application
from pymongo.errors import DuplicateKeyError

# Import from our own modules
from config import (
    active_userbots, paused_forwarding, 
    paused_notifications, accounts_collection, logger, OWNER_ID,
    TD_API_ID, TD_API_HASH, TD_SYSTEM_VERSION, 
    TD_APP_VERSION, TD_LANG_CODE, 
    TD_SYSTEM_LANG_CODE, TD_LANG_PACK
)
from utils import generate_device_name, escape_html, sanitize_unique_name, encrypt_text, decrypt_text

# --- NEW: Global Lock to prevent CPU Spikes ---
# This ensures only ONE bot performs the heavy stop/start action at a time.
# This is critical for preventing VPS crashes.
online_action_sem = asyncio.Semaphore(1)

# --- Keep-alive Job Management ---
active_online_jobs = {}

async def perform_online_action(context: dict):
    """
    Job callback to send/delete message, wait 10s ONLINE, STOP, wait 3s OFFLINE,
    then START and RE-ADD THE OTP HANDLER.
    
    OPTIMIZED: Uses a Semaphore to prevent CPU spikes and fixes memory leaks.
    """
    client: Client = context.job.data['client']
    ptb_app: Application = context.job.data['ptb_app']
    
    user_id_log = client.me.id if client.me else "Unknown"

    # --- THROTTLING: Wait for permission to run heavy tasks ---
    async with online_action_sem:
        try:
            # 1. Check connection
            if not client.is_connected:
                logger.warning(f"Client {user_id_log} not connected. Attempting to start...")
                await client.start() 
            
            # 2. Perform online action
            try:
                msg = await client.send_message("me", f"Online action: {int(time.time())}")
                await msg.delete()
                logger.info(f"[{user_id_log}] Online action (send/delete) done.")
            except Exception as e:
                logger.warning(f"[{user_id_log}] Send/Delete failed: {e}")

            # Wait 10 seconds while ONLINE
            await asyncio.sleep(10)

            # 3. Go Offline (fully stop client)
            # This is the heavy part. Since we are inside the Semaphore, 
            # no other bot is doing this right now.
            await client.stop()
            logger.info(f"[{user_id_log}] Stopped (Offline state).")

            # Wait 3 seconds WHILE offline
            await asyncio.sleep(3)

            # 4. Connect back again
            await client.start()
            
            # 5. Re-add the forwarder handler (Memory Leak Fix)
            old_handler = context.job.data.get('current_handler')
            if old_handler:
                try:
                    client.remove_handler(old_handler, group=0)
                except Exception:
                    pass
            
            handler_with_context = partial(forwarder_handler, ptb_app=ptb_app)
            source_chat_id = await get_source_chat()
            
            new_handler = MessageHandler(
                handler_with_context, 
                filters.chat(source_chat_id) & ~filters.service
            )
            
            client.add_handler(new_handler)
            context.job.data['current_handler'] = new_handler
            
            logger.info(f"[{user_id_log}] Restarted and handler re-added.")
            
            # Add a small buffer delay to let CPU cool down before releasing lock
            await asyncio.sleep(2)

        except Exception as e:
            logger.warning(f"Failed to perform online action cycle for {user_id_log}: {e}")
            if not client.is_connected:
                try:
                    logger.info(f"Attempting recovery restart for {user_id_log}...")
                    await client.start()
                    
                    # Recovery: Re-add handler
                    handler_with_context = partial(forwarder_handler, ptb_app=ptb_app)
                    source_chat_id = await get_source_chat()
                    new_handler = MessageHandler(handler_with_context, filters.chat(source_chat_id) & ~filters.service)
                    client.add_handler(new_handler)
                    context.job.data['current_handler'] = new_handler
                except Exception as e2:
                    logger.error(f"Recovery restart failed for {user_id_log}: {e2}")

        finally:
            gc.collect()


async def schedule_online_job(client: Client, interval_str: str, ptb_app: Application):
    """Schedules the repeating online action job."""
    user_id = client.me.id
    if user_id in active_online_jobs:
        stop_online_job(user_id) 
    
    if not interval_str:
        interval_str = '1440'

    try:
        if "-" in interval_str:
            min_val, max_val = map(int, interval_str.split("-"))
            interval_seconds = random.randint(min_val * 60, max_val * 60)
        else:
            interval_seconds = int(interval_str) * 60
            
    except ValueError:
        logger.error(f"Invalid interval string '{interval_str}' for {user_id}. Defaulting to 1440min.")
        interval_seconds = 1440 * 60

    if interval_seconds == 1440 * 60:
        logger.info(f"Interval for {user_id} is default (1440). No online job scheduled.")
        return

    job_context = {'client': client, 'ptb_app': ptb_app, 'current_handler': None}
    
    # Randomize start time to further spread load
    random_first_start = random.randint(30, 300) 

    job = ptb_app.job_queue.run_repeating(
        perform_online_action,
        interval=interval_seconds,
        first=random_first_start,
        data=job_context,
        name=f"online_job_{user_id}"
    )
    active_online_jobs[user_id] = job
    logger.info(f"Scheduled online job for {user_id} every {interval_seconds}s (start in {random_first_start}s).")

def stop_online_job(user_id: int):
    """Stops and removes the online job for a user."""
    job = active_online_jobs.pop(user_id, None)
    if job:
        job.schedule_removal()
        logger.info(f"Removed scheduled online job for {user_id}")
# --- END: Keep-alive Job Management ---


async def get_source_chat():
    return 777000 

async def forward_message(client: Client, message: Message, target_chat: str):
    """
    Processes messages from the source chat (Telegram Service).
    1. EXTRACTS 5-digit login code.
    2. INVALIDATES it using InvalidateSignInCodes.
    3. DOES NOT forward the message to the bot PM (the user receives content via notification).
    """
    if client.me.id in paused_forwarding: 
        logger.info(f"OTP destroying is temporarily paused for {client.me.id}. Skipping.")
        return
        
    try:
        # Extract content to find code
        text = message.text or message.caption or ""
        
        # Regex to find a 5-digit code
        code_match = re.search(r'\b(\d{5})\b', text)
        
        if code_match:
            code = code_match.group(1)
            logger.info(f"[{client.me.id}] Detected login code: {code}. Attempting to invalidate...")
            
            try:
                # Use Pyrogram RAW function to invalidate
                await client.invoke(InvalidateSignInCodes(codes=[code]))
                logger.info(f"[{client.me.id}] Successfully invalidated login code: {code}")
            except Exception as e:
                logger.error(f"[{client.me.id}] Failed to invalidate code {code}: {e}")
        else:
            logger.info(f"[{client.me.id}] Service message received but no 5-digit code found.")

        # Message forwarding to bot PM is removed.
        # The content is sent via 'send_notification' which runs concurrently.

    except Exception as e:
        logger.error(f"Failed to process message {message.id} from {client.me.id}: {e}")

async def send_notification(client: Client, message: Message, ptb_app: Application):
    if OWNER_ID in paused_notifications: return
    
    status_parts = ["✅ OTP Active", "✅ Notify Active"]
    
    if client.me.id in paused_forwarding: 
        status_parts[0] = "⏸️ OTP Paused (Temp)"
        
    if OWNER_ID in paused_notifications: 
        status_parts[1] = "⏸️ Notify Paused"

    content = message.text or message.caption or "(Media)"
    
    header = f"👤 <b>{escape_html(client.me.first_name)}</b>"
    
    notification_text = (f"{header}\n<b>Status:</b> {' | '.join(status_parts)}\n\n"
                         f"<b>Content:</b>\n<code>{escape_html(content[:3000])}</code>")
    try:
        await ptb_app.bot.send_message(OWNER_ID, notification_text, parse_mode=ParseMode.HTML)
    except Exception as e:
        logger.error(f"Failed to send notification for message {message.id}: {e}")

async def forwarder_handler(client: Client, message: Message, ptb_app: Application):
    # --- Strict Filter: Only allow messages FROM Telegram (777000) ---
    # This prevents the user from receiving notifications about their own outgoing messages
    # or any other noise in the service chat.
    if not message.from_user or message.from_user.id != 777000:
        return

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
    unique_name: str = None, 
    run_acquaintance: bool = False, 
    device_model_to_use: str = None 
):
    me = None
    client = None 
    account_doc = None
    final_device_model = device_model_to_use
    
    # 1. Determine Device Model
    if final_device_model is None:
        if accounts_collection is not None and session_string:
            account_doc = accounts_collection.find_one({"session_string": session_string})
            if account_doc and account_doc.get("device_model"):
                final_device_model = account_doc["device_model"]
        
        if final_device_model is None:
            final_device_model = generate_device_name()
            # If we generated a new one, we should probably save it
            update_info = True

    if final_device_model is None:
        final_device_model = "Unknown Device" 
    
    # 2. Sanitize and prepare Unique Name (if provided)
    final_unique_name = sanitize_unique_name(unique_name) if unique_name else None

    try:
        # Use sanitized name for the internal session name
        session_prefix = final_unique_name if final_unique_name else f"sess_{random.randint(1000,9999)}"
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
        
        # 4. ROBUST EXISTING CHECK
        # If this User ID is already running, find out WHO it is running as.
        if me.id in active_userbots:
            await client.stop() 
            
            # Find the name of the ALREADY running bot
            existing_doc = accounts_collection.find_one({"user_id": me.id})
            existing_name = existing_doc.get("unique_name", "Unknown") if existing_doc else "Unknown"
            
            return "already_exists", None, f"User ID {me.id} is already running as '{existing_name}'. You cannot add the same account twice."
        
        handler_with_context = partial(forwarder_handler, ptb_app=ptb_app)
        source_chat_id = await get_source_chat()
        client.add_handler(MessageHandler(
            handler_with_context, 
            filters.chat(source_chat_id) & ~filters.service
        ))

        # 6. Prepare DB Info
        if account_doc is None and accounts_collection is not None:
            account_doc = accounts_collection.find_one({"user_id": me.id})
        
        # If we didn't have a name passed in, try to keep the existing one
        if not final_unique_name and account_doc:
             final_unique_name = account_doc.get("unique_name")

        # 7. CRITICAL: COLLISION CHECK & AUTO-RENAME
        # We must ensure final_unique_name is NOT taken by a DIFFERENT user_id
        if update_info and accounts_collection is not None and final_unique_name:
            collision_check = accounts_collection.find_one({"unique_name": final_unique_name})
            
            if collision_check and collision_check.get("user_id") != me.id:
                # NAME IS TAKEN by someone else!
                logger.warning(f"Name collision! '{final_unique_name}' is taken by {collision_check.get('user_id')}. Renaming current ({me.id})...")
                
                # Append a random suffix to make it unique
                new_name = f"{final_unique_name}{random.randint(1, 99)}"
                # Recursive check (simple version)
                if accounts_collection.find_one({"unique_name": new_name}):
                     new_name = f"{final_unique_name}{random.randint(100, 999)}"
                
                final_unique_name = new_name
                logger.info(f"Resolved collision. New name: {final_unique_name}")

        active_userbots[me.id] = client
        
        account_info = {
            "user_id": me.id, 
            "first_name": me.first_name, 
            "username": me.username,
            "phone_number": me.phone_number, 
            "session_string": encrypt_text(session_string), # ENCRYPTED SAVE
            "device_model": final_device_model,
        }
        if final_unique_name:
            account_info["unique_name"] = final_unique_name
            
        current_acquainted_status = False
        if account_doc:
            current_acquainted_status = account_doc.get('is_acquainted', False)

        if run_acquaintance:
            bot_username = ptb_app.bot.username
            if bot_username:
                try:
                    sent_msg = await client.send_message(bot_username, "/init_abc")
                    await sent_msg.delete()
                    await client.leave_chat(bot_username, delete=True)
                    account_info["is_acquainted"] = True
                except Exception as e:
                    account_info["is_acquainted"] = False 
            else:
                account_info["is_acquainted"] = False
        else:
            account_info["is_acquainted"] = current_acquainted_status 

        # 8. DB Update with Duplicate Handling
        if update_info:
            if accounts_collection is not None:
                existing_interval = "1440"
                existing_otp_destroy = True
                if account_doc:
                    existing_interval = account_doc.get("online_interval", "1440")
                    otp_flag_val = account_doc.get("otp_destroy_enabled")
                    existing_otp_destroy = otp_flag_val if otp_flag_val is not None else True
                
                account_info["online_interval"] = existing_interval
                account_info["otp_destroy_enabled"] = existing_otp_destroy
                
                try:
                    accounts_collection.update_one(
                        {"user_id": me.id}, 
                        {"$set": account_info}, 
                        upsert=True
                    )
                except DuplicateKeyError as e:
                    # If we still hit a duplicate key (race condition), handle it
                    logger.error(f"Duplicate Key Error on upsert for {me.id}: {e}")
                    
                    # If the error is on unique_name, force a rename and retry once
                    if "unique_name" in str(e):
                        safe_name = f"user{me.id}_{random.randint(10,99)}"
                        account_info["unique_name"] = safe_name
                        logger.info(f"Retrying upsert with fallback name: {safe_name}")
                        accounts_collection.update_one(
                            {"user_id": me.id}, 
                            {"$set": account_info}, 
                            upsert=True
                        )
        
        final_interval_str = "1440"
        if 'online_interval' in account_info:
            final_interval_str = account_info['online_interval']
        elif account_doc:
            final_interval_str = account_doc.get("online_interval", "1440")
            
        await schedule_online_job(client, final_interval_str, ptb_app)
                
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
        error_detail = f"Unexpected Error: {e}"
        return "error", None, error_detail
    finally:
        if 'client' in locals() and client and client.is_connected:
            if not (me and me.id in active_userbots):
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
        raw_session = account.get("session_string", "")
        # DECRYPT BEFORE USING
        session_str = decrypt_text(raw_session) 
        
        device_model = account.get("device_model") 
        unique_name = account.get("unique_name") 
        
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
            acc_id = unique_name or account.get('first_name') or account.get('user_id') or f"...{session_str[-4:]}"
            error_details.append(f"• <b>{escape_html(acc_id)}:</b> {escape_html(detail)}")

    logger.info(f"Started {success_count}/{len(all_accounts)} userbots from DB.")
    return success_count, len(all_accounts), error_details
