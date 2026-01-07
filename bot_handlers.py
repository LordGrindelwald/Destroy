import asyncio
import sys 
import math 
import os 
from datetime import datetime
from functools import partial
from bson.objectid import ObjectId
from bson.json_util import dumps, loads 
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, User, MessageEntity
from telegram.ext import (
    ContextTypes,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ConversationHandler,
    filters,
)
from telegram.constants import ParseMode
from pyrogram.errors import PasswordHashInvalid, BadRequest, PeerIdInvalid, FloodWait
from pyrogram import Client 

# Import from our own modules
from config import (
    OWNER_ID, accounts_collection, active_userbots, 
    paused_forwarding, paused_notifications, logger,
    UNIQUE_NAME_PASTE, AWAIT_STRING_PASTE,
    AWAIT_BUTTON, SELECT_ACCOUNTS, AWAIT_INTERVAL,
    AWAIT_BUTTON_REMOVE, SELECT_ACCOUNTS_REMOVE, AWAIT_CONFIRM_REMOVE,
    # --- 2FA STATES ---
    AWAIT_BUTTON_2FA, SELECT_ACCOUNTS_2FA, AWAIT_DELAY_2FA, 
    AWAIT_PASSWORD_2FA, AWAIT_HINT_2FA, AWAIT_CURRENT_2FA_PASSWORD,
    # --- TD LIB CONSTANTS ---
    TD_API_ID, TD_API_HASH, TD_SYSTEM_VERSION, 
    TD_APP_VERSION, TD_LANG_CODE, 
    TD_SYSTEM_LANG_CODE, TD_LANG_PACK
)
from utils import (
    owner_only, escape_html, clean_session_string, 
    get_account_from_arg, generate_device_name, COMMAND_FALLBACKS,
    sanitize_unique_name, encrypt_text, decrypt_text 
)
from userbot_logic import (
    start_userbot, start_all_userbots_from_db,
    stop_online_job, schedule_online_job, active_online_jobs
)
from jobs import resume_forwarding_job, resume_all_job
from session_generator import generate_command

# --- Constants ---
ACCOUNTS_PER_PAGE = 16 

# ==============================================================================
#                       DEBUG & REPAIR COMMANDS
# ==============================================================================

@owner_only
async def debug_account_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    DEBUG TOOL: Dumps the raw MongoDB document for a specific account.
    Usage: /debug_acc <unique_name>
    """
    if not context.args:
        await update.message.reply_text("Usage: /debug_acc <unique_name_or_id>")
        return

    identifier = context.args[0]
    account = await get_account_from_arg(identifier)
    
    if not account:
        await update.message.reply_text(f"❌ Account '{identifier}' not found.")
        return

    # Create a safe copy to display (redact session string)
    debug_view = account.copy()
    if "session_string" in debug_view:
        debug_view["session_string"] = debug_view["session_string"][:20] + "..."
    
    # Convert ObjectId to string for JSON serialization
    if "_id" in debug_view:
        debug_view["_id"] = str(debug_view["_id"])

    # Dump to formatted JSON
    json_str = dumps(debug_view, indent=4)
    
    await update.message.reply_html(
        f"🐞 <b>Debug Dump for: {escape_html(identifier)}</b>\n"
        f"<pre>{escape_html(json_str)}</pre>"
    )

@owner_only
async def fix_db_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Force Syncs ALL accounts. 
    1. Uses temporary FILE SESSIONS (fixes 'no such table' error).
    2. Sends '/start' to the Bot USERNAME (fixes 'peer_id_invalid').
    3. Updates DB with real info.
    """
    if accounts_collection is None:
        await update.message.reply_text("⚠️ Database connection error.")
        return
        
    # Ensure we have the bot username to send /start to
    bot_username = context.bot.username
    if not bot_username:
        me_bot = await context.bot.get_me()
        bot_username = me_bot.username

    # Fetch ALL accounts
    all_accounts = await asyncio.to_thread(lambda: list(accounts_collection.find()))
    total_count = len(all_accounts)
    
    status_msg = await update.message.reply_text(
        f"🔄 <b>Starting Deep Repair (Username Mode)...</b>\n\n"
        f"Target: {total_count} accounts.\n"
        f"Handshake Target: @{bot_username}\n"
        f"<i>This creates temp files to resolve usernames safely.</i>", 
        parse_mode=ParseMode.HTML
    )

    fixed_count = 0
    updated_count = 0
    failed_count = 0
    log_lines = []

    for index, acc in enumerate(all_accounts):
        name = acc.get('unique_name', 'Unknown')
        doc_id = str(acc['_id'])
        old_id = acc.get('user_id')
        
        # Temp session file name to allow sqlite DB creation
        session_name = f"repair_{doc_id}"
        
        # Update progress every 5 accounts
        if index % 5 == 0:
            await status_msg.edit_text(
                f"🔄 <b>Deep Repair in Progress...</b>\n"
                f"Processing: {index + 1}/{total_count}\n"
                f"Handshakes: {fixed_count}\n"
                f"DB Updates: {updated_count}\n"
                f"Failures: {failed_count}", 
                parse_mode=ParseMode.HTML
            )

        temp_client = None
        try:
            # 1. Decrypt session
            raw_session = acc.get("session_string")
            if not raw_session:
                log_lines.append(f"❌ {name}: No session string.")
                failed_count += 1
                continue
                
            session = decrypt_text(raw_session)
            
            # 2. Connect using FILE SESSION to enable username resolution
            temp_client = Client(
                name=session_name,
                api_id=TD_API_ID,
                api_hash=TD_API_HASH,
                session_string=session,
                in_memory=False, # FORCE FILE MODE for SQLite username tables
                no_updates=True,
                device_model=acc.get("device_model", "RepairBot"),
                system_version=TD_SYSTEM_VERSION,
                app_version=TD_APP_VERSION,
                lang_code=TD_LANG_CODE,
                system_lang_code=TD_SYSTEM_LANG_CODE,
                lang_pack=TD_LANG_PACK
            )
            
            await temp_client.connect()
            
            # --- CRITICAL FIX: Send to USERNAME ---
            handshake_success = False
            try:
                # Send /start using the public username
                # Since in_memory=False, pyrogram can now resolve this peer
                await temp_client.send_message(chat_id=bot_username, text="/start")
                await asyncio.sleep(1) 
                handshake_success = True
            except Exception as e:
                # Log specific handshake error
                log_lines.append(f"⚠️ {name} Handshake: {str(e)[:40]}")
            # ---------------------------------------------------
            
            me = await temp_client.get_me()
            real_id = int(me.id)
            real_first_name = me.first_name or ""
            real_username = me.username or None
            real_phone = me.phone_number or acc.get('phone_number')
            
            await temp_client.disconnect()
            temp_client = None 

            # 3. DB Updates
            updates = {}
            if old_id != real_id:
                updates["user_id"] = real_id
                log_lines.append(f"🔧 {name}: ID fixed {old_id} -> {real_id}")
            
            if acc.get("first_name") != real_first_name:
                updates["first_name"] = real_first_name
            if acc.get("username") != real_username:
                updates["username"] = real_username
            if acc.get("phone_number") != real_phone:
                updates["phone_number"] = real_phone

            if updates:
                await asyncio.to_thread(
                    accounts_collection.update_one,
                    {"_id": acc['_id']},
                    {"$set": updates}
                )
                updated_count += 1
            
            # 4. Reverse Cache Force (Management Bot looks up User to cache the link)
            if handshake_success:
                try:
                    # Now that the userbot has messaged us, we can look them up
                    await context.bot.get_chat(real_id)
                except Exception:
                    pass 
                fixed_count += 1

        except Exception as e:
            failed_count += 1
            logger.error(f"Fix failed for {name}: {e}")
            log_lines.append(f"❌ {name} Critical: {str(e)[:50]}")
        finally:
            # Ensure client is stopped
            if temp_client and temp_client.is_connected:
                try:
                    await temp_client.disconnect()
                except:
                    pass
            
            # Clean up the temp .session file generated by Pyrogram
            # We must clean this up to prevent disk clutter
            try:
                if os.path.exists(f"{session_name}.session"):
                    os.remove(f"{session_name}.session")
                if os.path.exists(f"{session_name}.session-journal"):
                    os.remove(f"{session_name}.session-journal")
            except Exception:
                pass

    final_text = (
        f"✅ <b>Repair Complete</b>\n"
        f"Scanned: {total_count}\n"
        f"Handshakes Sent: {fixed_count}\n"
        f"DB Updates: {updated_count}\n"
        f"Failures: {failed_count}\n\n" +
        "\n".join(log_lines[:20]) 
    )
    if len(log_lines) > 20:
        final_text += f"\n...and {len(log_lines) - 20} more."
    
    await status_msg.edit_text(final_text, parse_mode=ParseMode.HTML)

# ==============================================================================
#                       BACKUP & RESTORE COMMANDS
# ==============================================================================

@owner_only
async def backup_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Backups the MongoDB accounts collection to a JSON file."""
    if accounts_collection is None:
        await update.message.reply_text("⚠️ Database connection error.")
        return
    
    status_msg = await update.message.reply_text("⏳ Generating backup...")
    
    try:
        # Run DB fetching in thread
        data = await asyncio.to_thread(lambda: list(accounts_collection.find()))
        
        # Serialize to JSON (bson.json_util handles ObjectId and datetime)
        json_data = dumps(data, indent=2)
        
        file_path = "userbot_backup.json"
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(json_data)
            
        await update.message.reply_document(
            document=open(file_path, "rb"),
            filename=f"backup_{datetime.now().strftime('%Y%m%d_%H%M')}.json",
            caption=f"📦 <b>Full Database Backup</b>\n\nContains {len(data)} accounts.",
            parse_mode=ParseMode.HTML
        )
        
        os.remove(file_path)
        await status_msg.delete()
        
    except Exception as e:
        logger.error(f"Backup failed: {e}")
        await status_msg.edit_text(f"❌ Backup failed: {e}")

@owner_only
async def restore_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Restores the database from a JSON file (replacing existing data)."""
    msg = update.message
    
    # Check if a file is attached or replied to
    document = msg.document
    if not document and msg.reply_to_message:
        document = msg.reply_to_message.document
        
    if not document:
        await msg.reply_text("❌ Please send this command with a backup JSON file (or reply to one).")
        return
        
    if accounts_collection is None:
        await msg.reply_text("⚠️ Database connection error.")
        return
        
    status_msg = await msg.reply_text("⏳ Downloading and verifying backup...")
    
    file_path = "temp_restore.json"
    try:
        telegram_file = await document.get_file()
        await telegram_file.download_to_drive(file_path)
        
        with open(file_path, "r", encoding="utf-8") as f:
            # bson.json_util.loads converts strings back to ObjectIds
            data = loads(f.read())
            
        if not isinstance(data, list):
            await status_msg.edit_text("❌ Invalid backup file format (Root must be a list).")
            return
            
        await status_msg.edit_text(f"⚠️ <b>Restoring {len(data)} accounts...</b>\n\nExisting data will be wiped.", parse_mode=ParseMode.HTML)
        
        # Perform Restore
        await asyncio.to_thread(accounts_collection.delete_many, {})
        if data:
            await asyncio.to_thread(accounts_collection.insert_many, data)
            
        await status_msg.edit_text(f"✅ <b>Restore Successful!</b>\n\nRestored {len(data)} accounts.\nPlease /restart the bot to apply changes.", parse_mode=ParseMode.HTML)
        
    except Exception as e:
        logger.error(f"Restore failed: {e}")
        await status_msg.edit_text(f"❌ Restore failed: {e}")
    finally:
        if os.path.exists(file_path):
            os.remove(file_path)

@owner_only
async def encrypt_past_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Encrypts any plain-text session strings in the database."""
    if accounts_collection is None:
        await update.message.reply_text("⚠️ Database connection error.")
        return
        
    status_msg = await update.message.reply_text("🔐 Scanning database for unencrypted sessions...")
    
    try:
        # Fetch all accounts
        all_accounts = await asyncio.to_thread(lambda: list(accounts_collection.find()))
        encrypted_count = 0
        
        for acc in all_accounts:
            raw_session = acc.get("session_string")
            if raw_session and not raw_session.startswith("gAAAAA"):
                # It doesn't look like a Fernet token, let's encrypt it
                new_session = encrypt_text(raw_session)
                
                # Double check it actually changed
                if new_session != raw_session:
                    await asyncio.to_thread(
                        accounts_collection.update_one,
                        {"_id": acc["_id"]},
                        {"$set": {"session_string": new_session}}
                    )
                    encrypted_count += 1
        
        await status_msg.edit_text(f"✅ <b>Encryption Complete</b>\n\nSuccessfully encrypted {encrypted_count} old accounts.", parse_mode=ParseMode.HTML)
        
    except Exception as e:
        logger.error(f"Encryption scan failed: {e}")
        await status_msg.edit_text(f"❌ Error: {e}")

# ==============================================================================
#                       STANDARD COMMAND HANDLERS
# ==============================================================================

@owner_only
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_html(
        "Personal Account Manager."
    )

@owner_only
async def restart_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Gracefully stops the application and triggers a container restart (exit code 1)."""
    
    if update.callback_query:
        await update.callback_query.answer("Restarting...")
        message_context = update.callback_query.message
    else:
        message_context = update.message
        
    await message_context.reply_text("🔄 Restarting service now...")
    
    # --- NEW: Stop all running jobs ---
    logger.info(f"Stopping {len(active_online_jobs)} online jobs...")
    for user_id in list(active_online_jobs.keys()):
        stop_online_job(user_id)
    # --- End ---
    
    if active_userbots:
        logger.info(f"Stopping {len(active_userbots)} userbot clients before restart...")
        stop_tasks = [client.stop() for client in active_userbots.values() if client.is_connected]
        await asyncio.gather(*stop_tasks, return_exceptions=True)
        active_userbots.clear()
        
    logger.info("Triggering application shutdown.")
    await context.application.stop_running() 
    
    sys.exit(1)


@owner_only
async def settings_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    keyboard = [
        [InlineKeyboardButton("👤 Manage Accounts", callback_data="manage_accounts")],
        [InlineKeyboardButton("➕ Add New Account", callback_data="call_add_command")] # <-- This now calls gen_conv
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    message_text = (
        "<b>Accounts Dashboard</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
    )

    if update.callback_query:
        await update.callback_query.edit_message_text(message_text, parse_mode=ParseMode.HTML, reply_markup=reply_markup)
    else:
        await update.message.reply_html(message_text, reply_markup=reply_markup)

# --- /add command is now handled by gen_conv in session_generator.py ---
# --- /remove command is now a ConversationHandler below ---

@owner_only
async def rename_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles /rename <identifier> <new_name>"""
    
    if accounts_collection is None:
        await update.message.reply_text("⚠️ Database connection is not available. Please check logs.")
        return

    if len(context.args) != 2:
        await update.message.reply_text("Usage: /rename <user_id_or_name> <new_unique_name>")
        return
        
    identifier = context.args[0]
    # --- FIX: Sanitize input ---
    new_name = sanitize_unique_name(context.args[1])
    
    account = await get_account_from_arg(identifier)
    if not account:
        await update.message.reply_text(f"⚠️ Account '<code>{escape_html(identifier)}</code>' not found.", parse_mode=ParseMode.HTML)
        return

    # Run blocking DB calls in a thread
    existing_with_name = await asyncio.to_thread(
        accounts_collection.find_one, 
        {"unique_name": new_name}
    )
    
    if existing_with_name and existing_with_name["_id"] != account["_id"]:
        await update.message.reply_text(f"⚠️ The name <code>{escape_html(new_name)}</code> is already taken by account <code>{existing_with_name.get('user_id')}</code>.", parse_mode=ParseMode.HTML)
        return
    
    # Run blocking DB calls in a thread
    await asyncio.to_thread(
        accounts_collection.update_one,
        {"_id": account["_id"]}, # Use _id to be specific
        {"$set": {"unique_name": new_name}}
    )
    
    await update.message.reply_text(
        f"✔️ Account <b>{escape_html(account.get('first_name'))}</b> "
        f"(<code>{account['user_id']}</code>) renamed to <code>{escape_html(new_name)}</code>.",
        parse_mode=ParseMode.HTML
    )

@owner_only
async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    total_bots = 0
    if accounts_collection is not None:
        # Run blocking DB calls in a thread
        total_bots = await asyncio.to_thread(
            accounts_collection.count_documents, 
            {}
        )
        
    running_bots = len(active_userbots)
    running_jobs = len(active_online_jobs)
    
    status_text = (f"<b>Bot Status</b>\n"
                   f"━━━━━━━━━━━━━━━━━━━━\n"
                   f"<b>Accounts Active:</b> {running_bots}/{total_bots}\n"
                   f"<b>Online Jobs Active:</b> {running_jobs}\n"
                   f"<b>Paused OTP Destruction:</b> {len(paused_forwarding)} bots (temp)\n"
                   f"<b>Paused OTP Forwarding:</b> {'Yes' if OWNER_ID in paused_notifications else 'No'}\n")
    await update.message.reply_html(status_text)

@owner_only
async def temp_pause_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Pauses a single account's OTP Destruction temporarily."""
    if not context.args:
        await update.message.reply_text("Usage: /temp <user_id_or_name>")
        return
        
    if accounts_collection is None:
        await update.message.reply_text("⚠️ Database connection is not available. Please check logs.")
        return

    try:
        identifier = context.args[0]
        # get_account_from_arg is already async (but it shouldn't be, it's blocking)
        # We'll assume it's fast enough for now to avoid rewriting utils.py
        account = await get_account_from_arg(identifier)
        
        if not account:
            await update.message.reply_text(f"⚠️ Account '<code>{escape_html(identifier)}</code>' not found.", parse_mode=ParseMode.HTML)
            return

        user_id_to_pause = account['user_id']
        
        # --- NEW: Check if permanently disabled ---
        if not account.get("otp_destroy_enabled", True):
            await update.message.reply_text(f"⚠️ OTP Destruction is permanently disabled for <code>{escape_html(account.get('first_name'))}</code>. /temp command is not applicable.", parse_mode=ParseMode.HTML)
            return
        # --- END NEW ---

        if user_id_to_pause not in active_userbots:
            await update.message.reply_text("User ID found but bot is not active.")
            return

        pause_id = f"{user_id_to_pause}_{int(datetime.now().timestamp())}"
        context.bot_data[pause_id] = False 

        paused_forwarding.add(user_id_to_pause)
        
        keyboard = [[InlineKeyboardButton("Pause Notifications", callback_data=f"pause_notify_{pause_id}")]]
        message = await update.message.reply_text(f"✅ Paused OTP destruction for <code>{escape_html(account.get('first_name'))}</code> (<code>{user_id_to_pause}</code>) for 5 minutes.",
                                                  reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML)
        
        context.application.job_queue.run_once(
            callback=resume_forwarding_job,
            when=300, 
            data={'user_id': user_id_to_pause, 'pause_id': pause_id, 'message_id': message.message_id},
            name=f"resume_{pause_id}"
        )

    except (IndexError, ValueError):
        await update.message.reply_text("Usage: /temp <user_id_or_name>")

@owner_only
async def temp_pause_all(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Pauses all userbots' OTP Destruction and forwarding."""
    for user_id in active_userbots.keys():
        paused_forwarding.add(user_id)
    paused_notifications.add(OWNER_ID)
    await update.message.reply_text("✅ Paused all OTP Destruction and forwarding for 5 minutes.")
    
    context.application.job_queue.run_once(resume_all_job, 300, name="resume_all")

@owner_only
async def ping_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    start_time = datetime.now()
    message = await update.message.reply_text("Pinging...")
    end_time = datetime.now()
    latency = (end_time - start_time).microseconds / 1000
    await message.edit_text(f"🏓 Pong!\nLatency: {latency:.2f} ms")

@owner_only
async def refresh_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text("🔄 Stopping all accounts...")
    
    # --- NEW: Stop all running jobs ---
    logger.info(f"Stopping {len(active_online_jobs)} online jobs...")
    for user_id in list(active_online_jobs.keys()):
        stop_online_job(user_id)
    # --- End ---
    
    stop_tasks = [client.stop() for client in active_userbots.values() if client.is_connected]
    await asyncio.gather(*stop_tasks, return_exceptions=True)
    active_userbots.clear()
    
    await asyncio.sleep(2)

    await msg.edit_text("🔄 Restarting and refreshing account info...")
    
    total_bots = 0
    if accounts_collection is not None:
        # Run blocking DB calls in a thread
        total_bots = await asyncio.to_thread(
            accounts_collection.count_documents,
            {}
        )
    else:
        await msg.edit_text("⚠️ Database connection is not available. Cannot refresh.")
        return
        
    _, _, errors = await start_all_userbots_from_db(
        context.application, 
        update_info=True
    )
    
    running_bots = len(active_userbots)
    final_message = f"✅ <b>Refresh Complete</b>\nStarted {running_bots}/{total_bots} accounts."

    if errors:
        error_message = "\n\n❌ <b>Errors Encountered:</b>\n" + "\n".join(errors)
        if len(final_message) + len(error_message) > 4096:
            await msg.edit_text(final_message, parse_mode=ParseMode.HTML)
            await update.message.reply_html(error_message)
        else:
            final_message += error_message
            await msg.edit_text(final_message, parse_mode=ParseMode.HTML)
    else:
        await msg.edit_text(final_message, parse_mode=ParseMode.HTML)

@owner_only
async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """General cancel command, clears user_data but not conv handler."""
    context.user_data.clear()
    await update.message.reply_text("Action cancelled.")

# ==============================================================================
#                       ACCOUNTS LIST & MENU HANDLERS
# ==============================================================================

@owner_only
async def accounts_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Handles /accs command.
    REVERTED: Clean HTML style. No debug IDs.
    Strictly casts ID to int to ensure link creation.
    """
    if accounts_collection is None:
        await update.message.reply_html("⚠️ Database connection is not available. Please check logs.")
        return
        
    # Run blocking DB calls in a thread
    accounts = await asyncio.to_thread(
        lambda: list(accounts_collection.find().sort("unique_name", 1))
    )
    
    if not accounts:
        await update.message.reply_html("You own 0 Accounts!")
        return

    # --- Helper to send chunks ---
    async def send_smart_chunks(header, items):
        """
        Sends a list of items in chunks.
        Splits if:
        1. Character count exceeds 4000 (Telegram limit is 4096).
        2. Item count exceeds 50 (User preference).
        """
        current_chunk = header
        current_count = 0 
        
        for item in items:
            if (len(current_chunk) + len(item) + 1 > 4000) or (current_count >= 50):
                await update.message.reply_html(current_chunk, disable_web_page_preview=True)
                current_chunk = item
                current_count = 1
            else:
                if current_count == 0 and current_chunk == "":
                     current_chunk = item
                else:
                     current_chunk += "\n" + item
                current_count += 1
                
        if current_chunk:
            await update.message.reply_html(current_chunk, disable_web_page_preview=True)
    # -----------------------------

    # --- DETAILED VIEW ---
    if context.args and context.args[0] == "-de":
        header_text = "👤 <b>Your Managed Accounts (Detailed):</b>\n"
        items = []

        for acc in accounts:
            user_id = acc.get('user_id')
            unique_name = escape_html(acc.get('unique_name', ''))
            first_name = escape_html(acc.get('first_name', ''))
            username_str = f"@{escape_html(acc.get('username'))}" if acc.get('username') else 'N/A'
            phone_str = f"+{escape_html(acc.get('phone_number'))}" if acc.get('phone_number') else 'N/A'
            device_model = escape_html(acc.get('device_model', 'N/A'))
            online_interval = acc.get('online_interval', '1440') 

            # Standard Mention Logic
            display_name = unique_name or first_name or f"ID: {user_id}"
            mention = display_name
            
            if user_id:
                try:
                    uid_int = int(user_id)
                    mention = f'<a href="tg://user?id={uid_int}">{display_name}</a>'
                except:
                    pass

            if unique_name and unique_name != display_name:
                mention += f" ({unique_name})"

            entry_text = (
                f"{mention}\n"
                f"<b>User:</b> {username_str}\n"
                f"<b>Phone:</b> <code>{phone_str}</code>\n"
                f"{device_model} ({escape_html(online_interval)} min)\n" 
                f"<b>ID:</b> {user_id if user_id else 'MISSING'}"
            )
            items.append(entry_text + f"\n{'-'*25}")
        
        await send_smart_chunks(header_text, items)
        return

    # --- CONCISE VIEW (Default) ---
    header_text = f"You own {len(accounts)} Accounts!\n"
    items = []
    
    for acc in accounts:
        user_id = acc.get('user_id')
        unique_name = escape_html(acc.get('unique_name', ''))
        first_name = escape_html(acc.get('first_name', ''))
        
        display_name = unique_name or first_name or f"ID: {user_id}"
        mention = display_name
        
        if user_id:
            try:
                uid_int = int(user_id)
                mention = f'<a href="tg://user?id={uid_int}">{display_name}</a>'
            except:
                pass

        phone = acc.get('phone_number')
        phone_str = f"+<code>{escape_html(phone)}</code>" if phone else "No Phone"
        
        interval = acc.get('online_interval', '1440')
        interval_str = f" (⌚ {interval}m)" if interval != '1440' else ""
        
        items.append(f"👉 {mention}: {phone_str}{interval_str}")

    await send_smart_chunks(header_text, items)


@owner_only
async def accounts_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    if accounts_collection is None:
        await query.edit_message_text("⚠️ Database connection is not available. Please check logs.")
        return
        
    # Run blocking DB calls in a thread
    accounts = await asyncio.to_thread(
        lambda: list(accounts_collection.find().sort("unique_name", 1))
    )
    
    base_text = "👤 <b>Your Managed Accounts:</b>\n\n"
    text_parts = []

    if not accounts:
        base_text += "No accounts have been added yet.\n\n"
        base_text += "ℹ️ <i>Run /refresh to update details.</i>"
    else:
        for acc in accounts:
            user_id = acc.get('user_id')
            
            raw_first_name = acc.get('first_name')
            raw_unique_name = acc.get('unique_name')
            raw_username = acc.get('username')
            raw_phone = acc.get('phone_number') 
            online_interval = acc.get('online_interval', '1440') 

            first_name = escape_html(raw_first_name) if raw_first_name else None
            unique_name = escape_html(raw_unique_name) if raw_unique_name else None
            username_str = f"@{escape_html(raw_username)}" if raw_username else 'N/A'
            phone_str = f"+{escape_html(raw_phone)}" if raw_phone else 'N/A'
            
            device_model = acc.get('device_model', 'N/A')
            
            display = unique_name or first_name or f"ID: {user_id}"
            
            mention = display
            if user_id:
                try:
                    uid_int = int(user_id)
                    mention = f'<a href="tg://user?id={uid_int}">{display}</a>'
                except:
                    pass

            entry_text = (
                f"{mention}\n"
                f"<b>User:</b> {username_str}\n"
                f"<b>Phone:</b> {phone_str}\n"
                f"{escape_html(device_model)}"
                f"  ({escape_html(online_interval)} min)\n"
                f"<b>ID:</b> {user_id if user_id else 'N/A'}"
            )
            text_parts.append(entry_text)

    final_text = base_text + f"\n{'-'*25}\n".join(text_parts)

    # --- FIX: Removed "Back to Settings" button ---
    keyboard = []
    reply_markup = InlineKeyboardMarkup(keyboard)

    try:
        await query.edit_message_text(
            text=final_text,
            parse_mode=ParseMode.HTML,
            reply_markup=reply_markup,
            disable_web_page_preview=True 
        )
    except Exception as e:
        if "Message is too long" in str(e):
             await query.edit_message_text(
                "⚠️ <b>Too many accounts to display in this menu.</b>\n"
                "Please use the <code>/accs</code> or <code>/accs -de</code> command instead, "
                "which supports multi-message splitting.",
                parse_mode=ParseMode.HTML,
                reply_markup=reply_markup
             )
        else:
             logger.error(f"Error in accounts_menu: {e}")

# --- execute_remove_account is now part of the remove_conv ---

@owner_only
async def set_next_step(update: Update, context: ContextTypes.DEFAULT_TYPE, step: str, text: str):
    query = update.callback_query
    await query.answer()
    context.user_data.clear() 
    
    persistent_device_model = generate_device_name()
    context.user_data['persistent_device_model'] = persistent_device_model
    context.user_data['next_step'] = step
    
    await query.edit_message_text(text)

@owner_only
async def pause_notifications_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Callback for the 'Pause Notifications' button."""
    query = update.callback_query
    await query.answer()
    
    try:
        pause_id = query.data.split("pause_notify_", 1)[1]
    except IndexError:
        await query.edit_message_text(f"{query.message.text}\n\n<i>Error: Invalid pause data.</i>", parse_mode=ParseMode.HTML)
        return

    if pause_id not in context.bot_data:
        await query.answer("This pause has expired or is invalid.", show_alert=True)
        await query.edit_message_text(f"{query.message.text}\n\n<i>This pause has expired.</i>", parse_mode=ParseMode.HTML, reply_markup=None)
        return

    paused_notifications.add(OWNER_ID)
    context.bot_data[pause_id] = True 
    
    await query.edit_message_text(
        f"{query.message.text}\n\n<i>✅ Notifications also paused for the remainder of the 5-minute window.</i>",
        parse_mode=ParseMode.HTML,
        reply_markup=None
    )

# --- Message Handler (for multiple strings) ---

async def handle_text_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Handles text input for flows that use context.user_data['next_step'].
    """
    if update.effective_user.id != OWNER_ID:
        return
        
    step = context.user_data.get('next_step')
    persistent_device_model = context.user_data.get('persistent_device_model')

    if step != 'awaiting_multiple_accounts':
        return

    del context.user_data['next_step']
    
    text = update.message.text
    session_strings = [clean_session_string(s) for s in text.replace(",", " ").replace("\n", " ").split() if s.strip()]
    msg = await update.message.reply_text(f"Processing {len(session_strings)} strings...")
    
    if accounts_collection is None:
        await msg.edit_text("⚠️ Database connection is not available. Cannot add accounts.")
        context.user_data.clear()
        return

    success, fail = 0, 0
    for session in session_strings:
        status, _, detail = await start_userbot(
            session, 
            context.application, 
            update_info=True,
            run_acquaintance=True,
            device_model_to_use=persistent_device_model
        )
        if status == "success": success += 1
        else: fail += 1
        
    context.user_data.clear()
    await msg.edit_text(f"Batch complete! ✅ Added: {success}, ❌ Failed: {fail}")
    await asyncio.sleep(3); await settings_command(update, context)


# --- BUGFIX: New self-contained cancel function ---
@owner_only
async def cancel_paste_conv(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancels the paste string conversation."""
    context.user_data.clear()
    if update.message:
        await update.message.reply_text("✖️ Paste account process cancelled.")
    return ConversationHandler.END
# --- END BUGFIX ---

# --- NEW Paste Single String Conversation Handler ---

@owner_only
async def prompt_for_unique_name_paste(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Entry point for pasting a single string. Asks for unique name."""
    query = update.callback_query
    await query.answer()
    
    persistent_device_model = generate_device_name()
    context.user_data['persistent_device_model'] = persistent_device_model
    
    await query.message.reply_text("Please send a unique name (e.g., 'main_acct') for this new account. Send /cancel to stop.")
    return UNIQUE_NAME_PASTE

@owner_only
async def get_unique_name_for_paste(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Saves unique name and asks for session string."""
    
    # --- FIX: Sanitize input ---
    raw_input = update.message.text.strip().split()[0]
    unique_name = sanitize_unique_name(raw_input)
    
    if accounts_collection is None:
        await update.message.reply_text("⚠️ Database connection is not available. Please /cancel and try again.")
        return ConversationHandler.END

    # Run blocking DB calls in a thread
    account = await asyncio.to_thread(
        accounts_collection.find_one, 
        {"unique_name": unique_name}
    )
    if account:
        await update.message.reply_text(f"The name '{unique_name}' is already taken. Please choose another one.")
        return UNIQUE_NAME_PASTE 
        
    context.user_data['unique_name'] = unique_name
    await update.message.reply_text(f"Name set to: <b>{unique_name}</b>\nGreat. Now please paste the session string.", parse_mode=ParseMode.HTML)
    return AWAIT_STRING_PASTE

@owner_only
async def get_session_string_and_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Gets session string, adds account, and ends conversation."""
    session_string = clean_session_string(update.message.text)
    unique_name = context.user_data.get('unique_name')
    persistent_device_model = context.user_data.get('persistent_device_model') 
    
    msg = await update.message.reply_text("⏳ Processing session string...")
    
    if accounts_collection is None:
        await msg.edit_text("⚠️ Database connection is not available. Cannot add account.")
        context.user_data.clear()
        return ConversationHandler.END
        
    status, user_info, detail = await start_userbot(
        session_string, 
        context.application, 
        update_info=True, 
        unique_name=unique_name,
        run_acquaintance=True,
        device_model_to_use=persistent_device_model
    )
    
    if status == "success":
        await msg.edit_text(f"✅ Account <code>{escape_html(user_info.first_name)}</code> (<code>{escape_html(unique_name)}</code>) added successfully!", parse_mode=ParseMode.HTML)
    else:
        await msg.edit_text(f"⚠️ Error adding account: {detail}")
        
    context.user_data.clear()
    return ConversationHandler.END

# --- Define the ConversationHandler ---
paste_single_conv = ConversationHandler(
    entry_points=[CallbackQueryHandler(prompt_for_unique_name_paste, pattern="^add_single$")],
    states={
        UNIQUE_NAME_PASTE: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_unique_name_for_paste)],
        AWAIT_STRING_PASTE: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_session_string_and_add)],
    },
    fallbacks=[
        CommandHandler("cancel", cancel_paste_conv), # <-- BUGFIX
        *COMMAND_FALLBACKS # <-- BUGFIX
    ],
    conversation_timeout=300,
)


# --- REBUILT: Online Interval Flow ---

@owner_only
async def online_interval_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    (Image 1) Sends the initial /online_interval command response.
    """
    keyboard = [[InlineKeyboardButton("OnlineInterval settings ⌚️⚙️", callback_data="oi_start_selection")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_html(
        "Click the button to select the account(s) for changing online interval.",
        reply_markup=reply_markup
    )
    return AWAIT_BUTTON


async def draw_account_selection_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    (Images 2, 3, 4) Draws the paginated multi-select account menu.
    """
    query = update.callback_query
    
    all_account_ids = context.user_data.get('all_account_ids', [])
    selected_accounts = context.user_data.get('selected_accounts', set())
    current_page = context.user_data.get('current_page', 0)
    
    if not all_account_ids:
        if query: await query.answer("Error: Account list not found.", show_alert=True)
        return ConversationHandler.END

    total_accounts = len(all_account_ids)
    total_pages = math.ceil(total_accounts / ACCOUNTS_PER_PAGE)
    
    start_index = current_page * ACCOUNTS_PER_PAGE
    end_index = start_index + ACCOUNTS_PER_PAGE
    page_account_ids = all_account_ids[start_index:end_index]
    
    page_accounts = []
    if accounts_collection is not None:
        # Run blocking DB calls in a thread
        page_accounts = await asyncio.to_thread(
            lambda: list(accounts_collection.find(
                {"user_id": {"$in": page_account_ids}},
                {"first_name": 1, "user_id": 1, "unique_name": 1, "online_interval": 1}
            ))
        )
    
    account_map = {acc['user_id']: acc for acc in page_accounts}
    sorted_page_accounts = [account_map[uid] for uid in page_account_ids if uid in account_map]

    keyboard = []
    
    control_row1 = [
        InlineKeyboardButton(f"Select all ({total_accounts}) 🗂️", callback_data="oi_select_all"),
        InlineKeyboardButton(f"Unselect all ({len(selected_accounts)}) 🗑️", callback_data="oi_unselect_all"),
    ]
    control_row2 = [
        InlineKeyboardButton("Select page 📖", callback_data="oi_select_page"),
        InlineKeyboardButton("Unselect page ❌", callback_data="oi_unselect_page"),
    ]
    keyboard.append(control_row1)
    keyboard.append(control_row2)

    account_buttons = []
    for acc in sorted_page_accounts:
        user_id = acc['user_id']
        name = escape_html(acc.get('first_name', acc.get('unique_name', str(user_id))))
        interval = acc.get('online_interval', '1440') 
        
        is_selected = user_id in selected_accounts
        
        # --- EMOJI FIX ---
        prefix = ""
        if is_selected:
            prefix = "✅"
        elif interval != '1440':
            prefix = "⌚️"
        
        button_text = f"{prefix} {name} ({interval}m)".strip()
        # --- END EMOJI FIX ---
        
        callback = f"oi_toggle_{user_id}"
        account_buttons.append(InlineKeyboardButton(button_text, callback_data=callback))

    for i in range(0, len(account_buttons), 2):
        keyboard.append(account_buttons[i:i+2])
        
    keyboard.append([InlineKeyboardButton("Done selecting 👌", callback_data="oi_done")])

    page_buttons = []
    if current_page > 0:
        page_buttons.append(InlineKeyboardButton("⬅️ Prev", callback_data="oi_prev_page"))
    page_buttons.append(InlineKeyboardButton(f"Page {current_page + 1}/{total_pages}", callback_data="oi_noop"))
    if current_page < total_pages - 1:
        page_buttons.append(InlineKeyboardButton("Next ➡️", callback_data="oi_next_page"))
    keyboard.append(page_buttons)

    # --- FIX: Add Cancel Button ---
    # --- BUGFIX: Changed callback_data to not conflict with state pattern ---
    keyboard.append([InlineKeyboardButton("« Cancel", callback_data="cancel_oi_conv")])
    # --- END FIX ---

    reply_markup = InlineKeyboardMarkup(keyboard)
    
    message_text = (
        f"<b>Account selection [multi]</b>\n"
        f"Page: {current_page + 1} / {total_pages}\n"
        f"Selected: {len(selected_accounts)} / {total_accounts}"
    )
    
    # This function is only called from callbacks, so we always edit
    try:
        await query.edit_message_text(message_text, parse_mode=ParseMode.HTML, reply_markup=reply_markup)
    except Exception as e:
        logger.warning(f"Error editing message in draw_account_selection_menu: {e}")
        
    return SELECT_ACCOUNTS


@owner_only
async def online_interval_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    (Image 2) Entry point for the ConversationHandler.
    Fetches all accounts, sets up user_data, and draws the menu.
    """
    query = update.callback_query
    await query.answer()
    
    if accounts_collection is None:
        await query.edit_message_text("⚠️ Database connection is not available. Please check logs.")
        return ConversationHandler.END

    # Run blocking DB calls in a thread
    all_accounts = await asyncio.to_thread(
        lambda: list(accounts_collection.find({}, {"user_id": 1}))
    )
    if not all_accounts:
        await query.edit_message_text("There are no accounts to configure. Please /add one first.")
        return ConversationHandler.END
        
    all_account_ids = [acc['user_id'] for acc in all_accounts]
    
    context.user_data.clear()
    context.user_data['all_account_ids'] = all_account_ids
    context.user_data['selected_accounts'] = set()
    context.user_data['current_page'] = 0
    
    return await draw_account_selection_menu(update, context)


@owner_only
async def handle_account_selection_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    (Image 3) Handles all button presses within the account selection menu.
    """
    query = update.callback_query
    await query.answer()
    
    data = query.data
    
    all_account_ids = context.user_data.get('all_account_ids', [])
    selected_accounts = context.user_data.get('selected_accounts', set())
    current_page = context.user_data.get('current_page', 0)
    
    if data.startswith("oi_toggle_"):
        user_id = int(data.split("_")[2])
        if user_id in selected_accounts:
            selected_accounts.discard(user_id)
        else:
            selected_accounts.add(user_id)
            
    elif data == "oi_select_all":
        selected_accounts.update(all_account_ids)
        
    elif data == "oi_unselect_all":
        selected_accounts.clear()

    elif data == "oi_select_page":
        start_index = current_page * ACCOUNTS_PER_PAGE
        end_index = start_index + ACCOUNTS_PER_PAGE
        page_account_ids = all_account_ids[start_index:end_index]
        selected_accounts.update(page_account_ids)
        
    elif data == "oi_unselect_page":
        start_index = current_page * ACCOUNTS_PER_PAGE
        end_index = start_index + ACCOUNTS_PER_PAGE
        page_account_ids = set(all_account_ids[start_index:end_index])
        selected_accounts.difference_update(page_account_ids)

    elif data == "oi_next_page":
        total_pages = math.ceil(len(all_account_ids) / ACCOUNTS_PER_PAGE)
        if current_page < total_pages - 1:
            context.user_data['current_page'] = current_page + 1
            
    elif data == "oi_prev_page":
        if current_page > 0:
            context.user_data['current_page'] = current_page - 1
            
    elif data == "oi_noop":
        return SELECT_ACCOUNTS 
        
    elif data == "oi_done":
        if not selected_accounts:
            await query.answer("⚠️ Please select at least one account.", show_alert=True)
            return SELECT_ACCOUNTS 
        
        await query.edit_message_text(
            "💭 Send the new interval now (in minutes, max. 1440; 24 hours)\n\n"
            "ℹ️ You can send minutes like this: <code>1-140</code> to make me set a random number "
            "in the range, e.g. from 1 to 140 minutes.\n\n"
            "If you want to reset the value to default (1440; 24 hours), send /default.",
            parse_mode=ParseMode.HTML
        )
        return AWAIT_INTERVAL

    context.user_data['selected_accounts'] = selected_accounts
    return await draw_account_selection_menu(update, context)


@owner_only
async def handle_interval_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    (Step 6) Handles the text input for the interval.
    """
    user_input = update.message.text.strip()
    selected_accounts = context.user_data.get('selected_accounts', set())
    
    if accounts_collection is None:
        await update.message.reply_text("⚠️ Database connection error.")
        context.user_data.clear()
        return ConversationHandler.END

    if not selected_accounts:
        await update.message.reply_text("⚠️ No accounts were selected. Action cancelled.")
        context.user_data.clear()
        return ConversationHandler.END
        
    interval_to_set = None
    if "-" in user_input:
        parts = user_input.split("-")
        if len(parts) == 2:
            try:
                min_val = int(parts[0])
                max_val = int(parts[1])
                if 0 < min_val <= max_val <= 1440:
                    interval_to_set = f"{min_val}-{max_val}"
            except ValueError: pass
    else:
        try:
            val = int(user_input)
            if 0 < val <= 1440:
                interval_to_set = str(val)
        except ValueError: pass

    if interval_to_set is None:
        await update.message.reply_text("Invalid format. Please send a number (e.g., 60) or a range (e.g., 30-90) between 1 and 1440.")
        return AWAIT_INTERVAL 

    # Run blocking DB calls in a thread
    await asyncio.to_thread(
        accounts_collection.update_many,
        {"user_id": {"$in": list(selected_accounts)}},
        {"$set": {"online_interval": interval_to_set}}
    )
    
    # --- NEW: Update running jobs ---
    for user_id in selected_accounts:
        stop_online_job(user_id) # Stop old job
        if user_id in active_userbots:
            client = active_userbots[user_id]
            await schedule_online_job(client, interval_to_set, context.application)
    # --- End ---
    
    await update.message.reply_text("✅ Saved online interval settings.")
    context.user_data.clear()
    return ConversationHandler.END


@owner_only
async def set_interval_default(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    (Step 6, default) Handles /default command for interval.
    """
    selected_accounts = context.user_data.get('selected_accounts', set())
    
    if accounts_collection is None:
        await update.message.reply_text("⚠️ Database connection error.")
        context.user_data.clear()
        return ConversationHandler.END

    if not selected_accounts:
        await update.message.reply_text("⚠️ No accounts were selected. Action cancelled.")
        context.user_data.clear()
        return ConversationHandler.END

    interval_to_set = "1440"
    # Run blocking DB calls in a thread
    await asyncio.to_thread(
        accounts_collection.update_many,
        {"user_id": {"$in": list(selected_accounts)}},
        {"$set": {"online_interval": interval_to_set}}
    )
    
    # --- NEW: Stop running jobs ---
    for user_id in selected_accounts:
        stop_online_job(user_id) # Stop old job
        # No need to reschedule, 1440 means no job
    # --- End ---
    
    await update.message.reply_text("✅ Saved online interval settings (reset to 1440).")
    context.user_data.clear()
    return ConversationHandler.END


@owner_only
async def cancel_interval_conv(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancels the interval selection conversation."""
    context.user_data.clear()
    
    # --- FIX: Edit message on cancel ---
    if update.callback_query:
        await update.callback_query.answer()
        try:
            await update.callback_query.edit_message_text("✖️ Online interval process cancelled.")
        except Exception:
            # Fallback if editing fails
            await update.callback_query.message.reply_text("✖️ Online interval process cancelled.")
    else:
        await update.message.reply_text("✖️ Online interval process cancelled.")
    # --- END FIX ---
            
    return ConversationHandler.END


# --- Define the ConversationHandler ---
online_interval_conv = ConversationHandler(
    entry_points=[
        CommandHandler("online_interval", online_interval_start)
    ],
    states={
        AWAIT_BUTTON: [CallbackQueryHandler(online_interval_menu, pattern="^oi_start_selection$")],
        SELECT_ACCOUNTS: [CallbackQueryHandler(handle_account_selection_callback, pattern=r"^oi_")],
        AWAIT_INTERVAL: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, handle_interval_input),
            CommandHandler("default", set_interval_default)
        ],
    },
    fallbacks=[
        CommandHandler("cancel", cancel_interval_conv),
        CallbackQueryHandler(cancel_interval_conv, pattern="^cancel$"),
        # --- FIX: Add specific cancel button handler ---
        CallbackQueryHandler(cancel_interval_conv, pattern="^cancel_oi_conv$"),
        *COMMAND_FALLBACKS # <-- BUGFIX
    ],
    conversation_timeout=600,
)

# --- NEW: /acc <name> command ---

@owner_only
async def account_detail_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles /acc <identifier> - Shows detailed info for one account."""
    
    if accounts_collection is None:
        await update.message.reply_text("⚠️ Database connection is not available. Please check logs.")
        return

    if not context.args:
        await update.message.reply_text("Usage: /acc <user_id_or_name>")
        return
        
    identifier = context.args[0]
    account = await get_account_from_arg(identifier)
    
    if not account:
        await update.message.reply_text(f"⚠️ Account '<code>{escape_html(identifier)}</code>' not found.", parse_mode=ParseMode.HTML)
        return

    # Gather details
    unique_name = escape_html(account.get('unique_name', 'N/A'))
    first_name = escape_html(account.get('first_name', 'N/A'))
    phone = f"+{escape_html(account.get('phone_number', 'N/A'))}" if account.get('phone_number') else 'N/A'
    user_id = account.get('user_id')
    
    interval = account.get('online_interval', '1440')
    interval_str = f"every {interval} minutes" if interval != '1440' else "every 24 hours (default)"
    
    # OTP Forwarding (Notifications to Owner)
    otp_fwd_status = "💭 active" if OWNER_ID not in paused_notifications else "⏸️ paused (globally)"
    
    # OTP Destroying (Permanent Flag)
    otp_destroy_enabled = account.get("otp_destroy_enabled", True)
    otp_destroy_status = "💥💣🔢 active" if otp_destroy_enabled else "❌ DISABLED"
    
    # OTP Destroying (Temporary Pause)
    if otp_destroy_enabled and user_id in paused_forwarding:
        otp_destroy_status = "⏸️ paused (temporarily)"
    
    # --- NEW: Show 2FA Status ---
    two_fa_pwd = account.get("two_fa_password")
    two_fa_status = "🔐 Stored" if two_fa_pwd else "⚠️ Not stored (Manual entry required for auto-updates)"

    text_parts = [
        f"<b>Account Settings for ID:</b> [<code>{unique_name}</code>]\n",
        f"<b>Profile:</b> {first_name}",
        f"<b>Phone:</b> <code>{phone}</code>\n",
        f"<b>User ID:</b> <code>{user_id}</code>",
        f"<b>Owner:</b> <code>{OWNER_ID}</code> (you)\n",
        f"⌚ <b>Online interval:</b> {interval_str}",
        f"✌️ Manage online interval with /online_interval\n",
        "<b>OTP preferences:</b>",
        "--- OTP forwarding ---",
        f"{otp_fwd_status}",
        "------------",
        "--- OTP destroying ---",
        f"{otp_destroy_status}",
        "------------",
        f"<b>2FA Password:</b> {two_fa_status}",
    ]
    
    if otp_destroy_enabled:
        text_parts.append(f"-> Want to log in? -> /temp {unique_name}")
    
    text_parts.append(f"Manage OTP destroying with /toggle_otp_destroy {unique_name}")
    text_parts.append("------------")

    await update.message.reply_html("\n".join(text_parts))

@owner_only
async def toggle_otp_destroy_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles /toggle_otp_destroy <identifier> - Toggles permanent OTP destruction."""
    
    if accounts_collection is None:
        await update.message.reply_text("⚠️ Database connection is not available. Please check logs.")
        return

    if not context.args:
        await update.message.reply_text("Usage: /toggle_otp_destroy <user_id_or_name>")
        return
        
    identifier = context.args[0]
    account = await get_account_from_arg(identifier)
    
    if not account:
        await update.message.reply_text(f"⚠️ Account '<code>{escape_html(identifier)}</code>' not found.", parse_mode=ParseMode.HTML)
        return

    # Toggle the boolean value
    current_status = account.get("otp_destroy_enabled", True)
    new_status = not current_status
    
    # Run blocking DB calls in a thread
    await asyncio.to_thread(
        accounts_collection.update_one,
        {"_id": account["_id"]},
        {"$set": {"otp_destroy_enabled": new_status}}
    )
    
    status_text = "ENABLED" if new_status else "DISABLED"
    await update.message.reply_html(
        f"✅ OTP Destruction for <b>{escape_html(account.get('first_name'))}</b> "
        f"(<code>{escape_html(account.get('unique_name'))}</code>) is now permanently <b>{status_text}</b>."
    )


# --- /remove ConversationHandler ---

@owner_only
async def remove_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Entry point for /remove.
    Handles both /remove (menu) and /remove <name> (direct).
    """
    
    # --- NEW FEATURE: Handle /remove <name> ---
    if context.args:
        # --- NOTIMPLEMENTEDERROR FIX ---
        if accounts_collection is None:
            await update.message.reply_html("⚠️ Database connection is not available. Please check logs.")
            return ConversationHandler.END
            
        identifier = context.args[0]
        account = await get_account_from_arg(identifier)
        
        if not account:
            await update.message.reply_text(f"⚠️ Account '<code>{escape_html(identifier)}</code>' not found.", parse_mode=ParseMode.HTML)
            return ConversationHandler.END
            
        user_id = account.get('user_id')
        if not user_id:
            await update.message.reply_text(f"⚠️ Account '<code>{escape_html(identifier)}</code>' has no user_id. Cannot remove.", parse_mode=ParseMode.HTML)
            return ConversationHandler.END

        # Store the single selected account
        context.user_data['selected_accounts'] = {user_id}
        
        # Go straight to confirmation
        unique_name = account.get('unique_name')
        name = escape_html(unique_name) if unique_name else f"ID: {user_id}"
        
        text = (
            f"<b>FINAL CONFIRMATION</b>\n\n"
            f"Are you sure you want to permanently remove this account?\n"
            f"• {name}\n\n"
            "This action is <b>IRREVERSIBLE</b>."
        )
        keyboard = [
            [InlineKeyboardButton("✅ Yes, Remove It", callback_data="acct_rm_confirm_yes")],
            [InlineKeyboardButton("❌ No, Cancel", callback_data="acct_rm_confirm_no")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await update.message.reply_html(text, reply_markup=reply_markup)
        return AWAIT_CONFIRM_REMOVE # Skip to the confirmation state

    # --- Original Flow: /remove (no args) ---
    keyboard = [[InlineKeyboardButton("Select Accounts to Remove 🗑️", callback_data="acct_rm_start")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_html(
        "Click the button to select the account(s) for removal.",
        reply_markup=reply_markup
    )
    return AWAIT_BUTTON_REMOVE


@owner_only
async def remove_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Callback for the start button.
    Fetches all accounts, sets up user_data, and draws the menu.
    """
    query = update.callback_query
    await query.answer()
    
    if accounts_collection is None:
        await query.edit_message_text("⚠️ Database connection is not available. Please check logs.")
        return ConversationHandler.END

    # Run blocking DB calls in a thread
    all_accounts = await asyncio.to_thread(
        lambda: list(accounts_collection.find({}, {"user_id": 1}))
    )
    if not all_accounts:
        await query.edit_message_text("There are no accounts to remove. Please /add one first.")
        return ConversationHandler.END
        
    all_account_ids = [acc['user_id'] for acc in all_accounts if acc.get('user_id')]
    
    context.user_data.clear()
    context.user_data['all_account_ids'] = all_account_ids
    context.user_data['selected_accounts'] = set()
    context.user_data['current_page'] = 0
    
    await draw_account_selection_menu_remove(query, context)
    return SELECT_ACCOUNTS_REMOVE


async def draw_account_selection_menu_remove(update_or_query: Update | CallbackQueryHandler, context: ContextTypes.DEFAULT_TYPE):
    """
    Draws the paginated multi-select account menu for REMOVAL.
    """
    if hasattr(update_or_query, 'callback_query') and update_or_query.callback_query:
        query = update_or_query.callback_query
    elif hasattr(update_or_query, 'data'):
        query = update_or_query
    else:
        query = None
        
    all_account_ids = context.user_data.get('all_account_ids', [])
    selected_accounts = context.user_data.get('selected_accounts', set())
    current_page = context.user_data.get('current_page', 0)
    
    if not all_account_ids:
        text = "Error: Account list not found."
        if query: await query.answer(text, show_alert=True)
        return ConversationHandler.END

    total_accounts = len(all_account_ids)
    total_pages = math.ceil(total_accounts / ACCOUNTS_PER_PAGE)
    
    start_index = current_page * ACCOUNTS_PER_PAGE
    end_index = start_index + ACCOUNTS_PER_PAGE
    page_account_ids = all_account_ids[start_index:end_index]
    
    page_accounts = []
    # --- NOTIMPLEMENTEDERROR FIX ---
    if accounts_collection is not None:
        # Run blocking DB calls in a thread
        page_accounts = await asyncio.to_thread(
            lambda: list(accounts_collection.find(
                {"user_id": {"$in": page_account_ids}},
                {"first_name": 1, "user_id": 1, "unique_name": 1}
            ))
        )
    
    account_map = {acc['user_id']: acc for acc in page_accounts}
    sorted_page_accounts = [account_map[uid] for uid in page_account_ids if uid in account_map]

    keyboard = []
    
    control_row1 = [
        InlineKeyboardButton(f"Select all ({total_accounts}) 🗂️", callback_data="acct_rm_select_all"),
        InlineKeyboardButton(f"Unselect all ({len(selected_accounts)}) 🗑️", callback_data="acct_rm_unselect_all"),
    ]
    control_row2 = [
        InlineKeyboardButton("Select page 📖", callback_data="acct_rm_select_page"),
        InlineKeyboardButton("Unselect page ❌", callback_data="acct_rm_unselect_page"),
    ]
    keyboard.append(control_row1)
    keyboard.append(control_row2)

    account_buttons = []
    for acc in sorted_page_accounts:
        user_id = acc['user_id']
        unique_name = acc.get('unique_name')
        name = escape_html(unique_name) if unique_name else f"ID: {user_id}"
        
        is_selected = user_id in selected_accounts
        prefix = "✅" if is_selected else "🗑️"
        button_text = f"{prefix} {name}".strip()
        
        callback = f"acct_rm_toggle_{user_id}"
        account_buttons.append(InlineKeyboardButton(button_text, callback_data=callback))

    for i in range(0, len(account_buttons), 2):
        keyboard.append(account_buttons[i:i+2])
        
    # --- START OF BUTTON FIX ---
    # We use a unique callback_data to avoid any pattern conflicts
    keyboard.append([InlineKeyboardButton("Done selecting 👌", callback_data="dnrm_done_select")])
    # --- END OF BUTTON FIX ---

    page_buttons = []
    if current_page > 0:
        page_buttons.append(InlineKeyboardButton("⬅️ Prev", callback_data="acct_rm_prev_page"))
    page_buttons.append(InlineKeyboardButton(f"Page {current_page + 1}/{total_pages}", callback_data="acct_rm_noop"))
    if current_page < total_pages - 1:
        page_buttons.append(InlineKeyboardButton("Next ➡️", callback_data="acct_rm_next_page"))
    keyboard.append(page_buttons)

    keyboard.append([InlineKeyboardButton("« Cancel", callback_data="cancel_rm_conv")])

    reply_markup = InlineKeyboardMarkup(keyboard)
    
    message_text = (
        f"<b>Select Accounts to REMOVE</b>\n"
        f"Page: {current_page + 1} / {total_pages}\n"
        f"Selected: {len(selected_accounts)} / {total_accounts}"
    )
    
    if query:
        try:
            await query.edit_message_text(message_text, parse_mode=ParseMode.HTML, reply_markup=reply_markup)
        except Exception as e:
            logger.warning(f"Error editing message in draw_account_selection_menu_remove: {e}")
        
    return SELECT_ACCOUNTS_REMOVE


# --- START: "DONE" BUTTON FIX (Reverting to 2-handler system) ---
@owner_only
async def handle_remove_done_selecting(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the 'Done Selecting' button press (dnrm_done_select)."""
    query = update.callback_query
    await query.answer()
    
    selected_accounts = context.user_data.get('selected_accounts', set())
    
    if not selected_accounts:
        await query.answer("⚠️ Please select at least one account.", show_alert=True)
        return SELECT_ACCOUNTS_REMOVE # Stay in this state
    
    account_names = []
    # --- NOTIMPLEMENTEDERROR FIX ---
    if accounts_collection is not None:
        # Run blocking DB calls in a thread
        selected_docs = await asyncio.to_thread(
            lambda: list(accounts_collection.find(
                {"user_id": {"$in": list(selected_accounts)}},
                {"first_name": 1, "unique_name": 1, "user_id": 1}
            ))
        )
        for acc in selected_docs:
            unique_name = acc.get('unique_name')
            user_id = acc.get('user_id')
            name = escape_html(unique_name) if unique_name else f"ID: {user_id}"
            account_names.append(f"• {name}")
    else:
        await query.edit_message_text("⚠️ Database connection error. Cannot proceed.")
        return ConversationHandler.END
    
    names_list_str = '\n'.join(account_names)
    text = (
        f"<b>FINAL CONFIRMATION</b>\n\n"
        f"Are you sure you want to permanently remove these {len(selected_accounts)} accounts?\n"
        f"{names_list_str}\n\n"
        "This action is <b>IRREVERSIBLE</b>."
    )

    keyboard = [
        [InlineKeyboardButton("✅ Yes, Remove Them", callback_data="acct_rm_confirm_yes")],
        [InlineKeyboardButton("❌ No, Cancel", callback_data="acct_rm_confirm_no")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=reply_markup)
    return AWAIT_CONFIRM_REMOVE # Move to next state


@owner_only
async def handle_account_selection_callback_remove(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles all *other* button presses (acct_rm_)."""
    query = update.callback_query
    await query.answer()
    
    data = query.data
    
    all_account_ids = context.user_data.get('all_account_ids', [])
    selected_accounts = context.user_data.get('selected_accounts', set())
    current_page = context.user_data.get('current_page', 0)
    
    if data.startswith("acct_rm_toggle_"):
        user_id = int(data.split("_")[3])
        if user_id in selected_accounts:
            selected_accounts.discard(user_id)
        else:
            selected_accounts.add(user_id)
            
    elif data == "acct_rm_select_all":
        selected_accounts.update(all_account_ids)
        
    elif data == "acct_rm_unselect_all":
        selected_accounts.clear()

    elif data == "acct_rm_select_page":
        start_index = current_page * ACCOUNTS_PER_PAGE
        end_index = start_index + ACCOUNTS_PER_PAGE
        page_account_ids = all_account_ids[start_index:end_index]
        selected_accounts.update(page_account_ids)
        
    elif data == "acct_rm_unselect_page":
        start_index = current_page * ACCOUNTS_PER_PAGE
        end_index = start_index + ACCOUNTS_PER_PAGE
        page_account_ids = set(all_account_ids[start_index:end_index])
        selected_accounts.difference_update(page_account_ids)

    elif data == "acct_rm_next_page":
        total_pages = math.ceil(len(all_account_ids) / ACCOUNTS_PER_PAGE)
        if current_page < total_pages - 1:
            context.user_data['current_page'] = current_page + 1
            
    elif data == "acct_rm_prev_page":
        if current_page > 0:
            context.user_data['current_page'] = current_page - 1
            
    elif data == "acct_rm_noop":
        return SELECT_ACCOUNTS_REMOVE
        
    context.user_data['selected_accounts'] = selected_accounts
    return await draw_account_selection_menu_remove(query, context)
# --- END: "DONE" BUTTON FIX ---


# --- START OF "INSTANT" HANG FIX ---
async def _delete_account_in_background(user_id, account_doc_id):
    """Helper function to run the blocking DB deletion in the background."""
    # --- NOTIMPLEMENTEDERROR FIX ---
    if accounts_collection is None:
        logger.error(f"Background delete failed for {user_id}: DB not connected.")
        return
        
    try:
        # Run the blocking DB call in a separate thread
        result = await asyncio.to_thread(
            accounts_collection.delete_one, 
            {"_id": account_doc_id}
        )
        if result.deleted_count > 0:
            logger.info(f"Background delete successful for user_id {user_id} (doc_id {account_doc_id}).")
        else:
            logger.warning(f"Background delete for {user_id} (doc_id {account_doc_id}) removed 0 documents.")
    except Exception as e:
        logger.error(f"Background delete failed for {user_id} (doc_id {account_doc_id}): {e}")
# --- END OF "INSTANT" HANG FIX ---


@owner_only
async def handle_remove_confirmation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the final Yes/No confirmation for removal."""
    query = update.callback_query
    await query.answer()
    
    if query.data == "acct_rm_confirm_no":
        await query.edit_message_text("✖️ Remove process cancelled.")
        context.user_data.clear()
        return ConversationHandler.END

    # User clicked YES (acct_rm_confirm_yes)
    selected_accounts = context.user_data.get('selected_accounts', set())
    if not selected_accounts:
        await query.edit_message_text("Error: No accounts selected. Action cancelled.")
        context.user_data.clear()
        return ConversationHandler.END
        
    # --- "INSTANT" FIX: Removed "Scheduling" message ---
    await query.edit_message_text(f"🔄 Removing {len(selected_accounts)} accounts...")
    
    removed_accounts_display = []
    
    for user_id in selected_accounts:
        account = None
        # --- NOTIMPLEMENTEDERROR FIX ---
        if accounts_collection is not None:
            # We still need to fetch the account to get its _id,
            # but this is a read operation (find_one) and should be fast.
            account = await asyncio.to_thread(
                accounts_collection.find_one, 
                {"user_id": user_id},
                {"_id": 1, "unique_name": 1} # Only fetch what we need
            )
        
        # Stop any running online jobs
        stop_online_job(user_id)
        
        # "Fire-and-forget" stop for the Pyrogram client
        if user_id in active_userbots:
            client_to_stop = active_userbots.pop(user_id) # Pop it immediately
            asyncio.create_task(client_to_stop.stop())
            logger.info(f"Scheduled client {user_id} for background stop.")
            
        # --- "INSTANT" FIX: "Fire-and-forget" the database deletion ---
        if account:
            account_doc_id = account["_id"]
            # This runs the delete in the background. The bot does NOT wait for it.
            asyncio.create_task(_delete_account_in_background(user_id, account_doc_id))
            
            name = escape_html(account.get('unique_name') or f"ID: {user_id}")
            # The message confirms the *action* (removal), not the *result*
            removed_accounts_display.append(f"☑️ {name} removal initiated.")
        else:
            logger.warning(f"Could not find account doc for user_id {user_id} to delete.")
        # --- END OF "INSTANT" FIX ---
            
    final_message = "\n".join(removed_accounts_display)
    if not final_message:
        final_message = "No accounts were found to remove."
        
    # Show the "removal initiated" message immediately
    await query.edit_message_text(final_message)
    context.user_data.clear()
    return ConversationHandler.END


@owner_only
async def cancel_remove_conv(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancels the remove selection conversation."""
    context.user_data.clear()
    
    if update.callback_query:
        await update.callback_query.answer()
        try:
            await update.callback_query.edit_message_text("✖️ Remove process cancelled.")
        except Exception:
            await update.callback_query.message.reply_text("✖️ Remove process cancelled.")
    else:
        await update.message.reply_text("✖️ Remove process cancelled.")
            
    return ConversationHandler.END


# --- Define the ConversationHandler ---
remove_conv = ConversationHandler(
    entry_points=[
        CommandHandler("remove", remove_start) # Now handles /remove AND /remove <name>
    ],
    states={
        AWAIT_BUTTON_REMOVE: [CallbackQueryHandler(remove_menu, pattern="^acct_rm_start$")],
        
        # --- START OF BUTTON FIX (Reverted to 2-handler) ---
        SELECT_ACCOUNTS_REMOVE: [
            # This specific pattern for the "Done" button MUST come first
            CallbackQueryHandler(handle_remove_done_selecting, pattern=r"^dnrm_done_select$"),
            
            # This pattern handles all other buttons (toggle, page, select all)
            CallbackQueryHandler(handle_account_selection_callback_remove, pattern=r"^acct_rm_")
        ],
        # --- END OF BUTTON FIX ---
        
        AWAIT_CONFIRM_REMOVE: [CallbackQueryHandler(handle_remove_confirmation, pattern=r"^acct_rm_confirm_")],
    },
    fallbacks=[
        CommandHandler("cancel", cancel_remove_conv),
        CallbackQueryHandler(cancel_remove_conv, pattern="^cancel$"),
        CallbackQueryHandler(cancel_remove_conv, pattern="^cancel_rm_conv$"),
        *COMMAND_FALLBACKS
    ],
    conversation_timeout=600,
)


# --- Deduplication Command (Now with non-blocking DB calls) ---

@owner_only
async def deduplicate_db_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Finds and removes duplicate account entries from the database.
    Keeps the *first* entry found for each duplicate group and deletes the rest.
    """
    msg = await update.message.reply_text("🔄 Stopping all accounts before deduplication...")
    
    if accounts_collection is None:
        await msg.edit_text("⚠️ Database connection is not available. Please check logs.")
        return

    # 1. Stop all running jobs
    logger.info(f"Stopping {len(active_online_jobs)} online jobs...")
    for user_id in list(active_online_jobs.keys()):
        stop_online_job(user_id)
    
    # 2. Stop all running clients
    if active_userbots:
        logger.info(f"Stopping {len(active_userbots)} userbot clients...")
        stop_tasks = []
        for client in active_userbots.values():
            if client.is_connected:
                stop_tasks.append(client.stop())
        await asyncio.gather(*stop_tasks, return_exceptions=True)
        active_userbots.clear()
    
    await msg.edit_text("Bots stopped. 🤖 Now searching for duplicates (this may take a moment)...")
    
    try:
        # --- NON-BLOCKING FIX ---
        # Sort by _id to ensure "first" is consistent
        all_accounts = await asyncio.to_thread(
            lambda: list(accounts_collection.find().sort([("_id", 1)]))
        )
        # --- END NON-BLOCKING FIX ---
        
        seen_user_ids = set()
        seen_unique_names = set()
        ids_to_delete = []
        
        uid_deleted_count = 0
        name_deleted_count = 0
        
        logger.info(f"[Deduplicate] Checking {len(all_accounts)} total documents...")
        
        for acc in all_accounts:
            doc_id = acc['_id']
            user_id = acc.get('user_id')
            unique_name = acc.get('unique_name')
            
            delete_this_doc = False
            
            # 1. Check user_id duplicates
            if user_id is not None:
                if user_id in seen_user_ids:
                    logger.warning(f"[Deduplicate] Found user_id duplicate: {user_id}. Marking {doc_id} for deletion.")
                    delete_this_doc = True
                    uid_deleted_count += 1
                else:
                    seen_user_ids.add(user_id)
            
            # 2. Check unique_name duplicates (case and whitespace insensitive)
            if unique_name is not None:
                name_key = str(unique_name).strip().lower()
                
                if name_key: # Ensure it's not an empty string
                    if name_key in seen_unique_names:
                        logger.warning(f"[Deduplicate] Found unique_name duplicate: '{name_key}'. Marking {doc_id} for deletion.")
                        if not delete_this_doc: # Only count if not already marked
                            name_deleted_count += 1
                        delete_this_doc = True
                    else:
                        seen_unique_names.add(name_key)
            
            if delete_this_doc:
                ids_to_delete.append(doc_id)

        total_deleted = 0
        if ids_to_delete:
            # Get unique list of doc IDs to delete
            unique_ids_to_delete = list(set(ids_to_delete))
            
            await msg.edit_text(
                f"Found {uid_deleted_count} duplicates by user_id.\n"
                f"Found {name_deleted_count} duplicates by unique_name.\n"
                f"Total unique documents to delete: {len(unique_ids_to_delete)}.\n\n"
                "🔄 Removing from database..."
            )
            
            # --- NON-BLOCKING FIX ---
            result = await asyncio.to_thread(
                accounts_collection.delete_many, 
                {"_id": {"$in": unique_ids_to_delete}}
            )
            total_deleted = result.deleted_count
            # --- END NON-BLOCKING FIX ---
            logger.info(f"[Deduplicate] Successfully deleted {total_deleted} documents.")
        
        # --- 5. Report ---
        final_message = (
            f"✅ <b>Deduplication Complete</b>\n"
            f"Removed {uid_deleted_count} duplicates by user_id.\n"
            f"Removed {name_deleted_count} duplicates by unique_name.\n"
            f"<b>Total documents deleted: {total_deleted}</b>"
        )
        await msg.edit_text(final_message, parse_mode=ParseMode.HTML)
        
        await update.message.reply_html(
            "Database is now clean.\n\n"
            "Please run /restart now to reload the bot and apply the new database indexes."
        )

    except Exception as e:
        logger.error(f"Error during deduplication: {e}")
        await msg.edit_text(f"An error occurred: {e}")

# --- 2FA Config ---

@owner_only
async def update_2fa_password_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if accounts_collection is None:
        await update.message.reply_text("⚠️ Database connection is not available.")
        return

    if len(context.args) < 2:
        await update.message.reply_text("Usage: /update2fa <user_id_or_name> <new_password>")
        return
    
    identifier = context.args[0]
    new_password = context.args[1] 
    
    account = await get_account_from_arg(identifier)
    
    if not account:
        await update.message.reply_text(f"⚠️ Account '<code>{escape_html(identifier)}</code>' not found.", parse_mode=ParseMode.HTML)
        return
        
    await asyncio.to_thread(
        accounts_collection.update_one,
        {"_id": account["_id"]},
        {"$set": {"two_fa_password": new_password}}
    )
    
    await update.message.reply_html(
        f"✅ Updated stored 2FA password for <b>{escape_html(account.get('first_name'))}</b> (<code>{account.get('user_id')}</code>)."
    )

# --- 2FA CONVERSATION ---

@owner_only
async def two_fa_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Entry point for /2fas command."""
    keyboard = [[InlineKeyboardButton("Select Accounts for 2FA 🔐", callback_data="2fa_start_selection")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_html(
        "<b>2FA Configuration Manager</b>\n\n"
        "Click the button below to select accounts for updating their Two-Step Verification password.",
        reply_markup=reply_markup
    )
    return AWAIT_BUTTON_2FA

@owner_only
async def two_fa_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Initializes the selection menu."""
    query = update.callback_query
    await query.answer()
    
    if accounts_collection is None:
        await query.edit_message_text("⚠️ Database connection is not available.")
        return ConversationHandler.END

    # Get all accounts from DB
    all_accounts = await asyncio.to_thread(
        lambda: list(accounts_collection.find({}, {"user_id": 1}))
    )
    if not all_accounts:
        await query.edit_message_text("No accounts found.")
        return ConversationHandler.END
        
    all_account_ids = [acc['user_id'] for acc in all_accounts]
    
    context.user_data.clear()
    context.user_data['all_account_ids'] = all_account_ids
    context.user_data['selected_accounts'] = set()
    context.user_data['current_page'] = 0
    
    await draw_account_selection_menu_2fa(query, context)
    return SELECT_ACCOUNTS_2FA

async def draw_account_selection_menu_2fa(query, context: ContextTypes.DEFAULT_TYPE):
    """Draws the account selection menu (reusing style from other menus)."""
    all_account_ids = context.user_data.get('all_account_ids', [])
    selected_accounts = context.user_data.get('selected_accounts', set())
    current_page = context.user_data.get('current_page', 0)
    
    total_accounts = len(all_account_ids)
    total_pages = math.ceil(total_accounts / ACCOUNTS_PER_PAGE)
    
    start_index = current_page * ACCOUNTS_PER_PAGE
    end_index = start_index + ACCOUNTS_PER_PAGE
    page_account_ids = all_account_ids[start_index:end_index]
    
    page_accounts = []
    if accounts_collection is not None:
        page_accounts = await asyncio.to_thread(
            lambda: list(accounts_collection.find(
                {"user_id": {"$in": page_account_ids}},
                {"first_name": 1, "user_id": 1, "unique_name": 1}
            ))
        )
    
    account_map = {acc['user_id']: acc for acc in page_accounts}
    sorted_page_accounts = [account_map[uid] for uid in page_account_ids if uid in account_map]

    keyboard = []
    
    # Control Row 1
    keyboard.append([
        InlineKeyboardButton(f"Select all ({total_accounts}) 🗂️", callback_data="2fa_select_all"),
        InlineKeyboardButton(f"Unselect all ({len(selected_accounts)}) 🗑️", callback_data="2fa_unselect_all"),
    ])
    
    # Account Buttons
    account_buttons = []
    for acc in sorted_page_accounts:
        user_id = acc['user_id']
        name = escape_html(acc.get('unique_name') or acc.get('first_name') or str(user_id))
        is_selected = user_id in selected_accounts
        prefix = "✅" if is_selected else "🔐"
        
        # Mark if active or not (optional visual cue)
        if user_id not in active_userbots:
            name += " (Offline)"
            
        account_buttons.append(InlineKeyboardButton(f"{prefix} {name}", callback_data=f"2fa_toggle_{user_id}"))

    for i in range(0, len(account_buttons), 2):
        keyboard.append(account_buttons[i:i+2])

    # Done Button
    keyboard.append([InlineKeyboardButton("Done selecting 👌", callback_data="2fa_done_select")])

    # Navigation
    page_buttons = []
    if current_page > 0:
        page_buttons.append(InlineKeyboardButton("⬅️ Prev", callback_data="2fa_prev_page"))
    page_buttons.append(InlineKeyboardButton(f"Page {current_page + 1}/{total_pages}", callback_data="2fa_noop"))
    if current_page < total_pages - 1:
        page_buttons.append(InlineKeyboardButton("Next ➡️", callback_data="2fa_next_page"))
    keyboard.append(page_buttons)
    
    keyboard.append([InlineKeyboardButton("« Cancel", callback_data="cancel_2fa_conv")])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    message_text = f"<b>Select Accounts for 2FA</b>\nSelected: {len(selected_accounts)} / {total_accounts}"
    
    try:
        await query.edit_message_text(message_text, parse_mode=ParseMode.HTML, reply_markup=reply_markup)
    except Exception:
        pass

@owner_only
async def handle_account_selection_callback_2fa(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles button clicks on the 2FA selection menu."""
    query = update.callback_query
    await query.answer()
    data = query.data
    
    all_account_ids = context.user_data.get('all_account_ids', [])
    selected_accounts = context.user_data.get('selected_accounts', set())
    current_page = context.user_data.get('current_page', 0)

    if data == "2fa_done_select":
        if not selected_accounts:
            await query.answer("Please select at least one account.", show_alert=True)
            return SELECT_ACCOUNTS_2FA
            
        # Check if more than one account is selected
        if len(selected_accounts) > 1:
            await query.edit_message_text(
                "⏱️ <b>Delay Configuration</b>\n\n"
                "Please send the delay (in seconds) between changing 2FA for each account.\n"
                "Default is <b>5</b> seconds.",
                parse_mode=ParseMode.HTML
            )
            return AWAIT_DELAY_2FA
        else:
            # Skip delay for single account
            context.user_data['2fa_delay'] = 0
            await ask_for_2fa_password(query, context)
            return AWAIT_PASSWORD_2FA

    # Toggle Logic
    elif data.startswith("2fa_toggle_"):
        user_id = int(data.split("_")[2])
        if user_id in selected_accounts: selected_accounts.discard(user_id)
        else: selected_accounts.add(user_id)
    elif data == "2fa_select_all": selected_accounts.update(all_account_ids)
    elif data == "2fa_unselect_all": selected_accounts.clear()
    
    # Pagination Logic
    elif data == "2fa_next_page": context.user_data['current_page'] += 1
    elif data == "2fa_prev_page": context.user_data['current_page'] -= 1
    
    context.user_data['selected_accounts'] = selected_accounts
    await draw_account_selection_menu_2fa(query, context)
    return SELECT_ACCOUNTS_2FA

async def ask_for_2fa_password(messageable, context):
    """Helper to send the password prompt."""
    text = (
        "💭🔐 <b>Send the new 2FA password</b> for your selected accounts\n\n"
        "ℹ️ You can reply with <code>#empty#</code> to disable 2FA password."
    )
    if isinstance(messageable, Update):
        await messageable.message.reply_html(text)
    else: # It's a callback query or message object
        if hasattr(messageable, 'edit_message_text'):
            await messageable.edit_message_text(text, parse_mode=ParseMode.HTML)
        else:
            await messageable.reply_html(text)

@owner_only
async def handle_2fa_delay_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the delay input."""
    text = update.message.text.strip()
    delay = 5
    if text.isdigit():
        delay = int(text)
    
    context.user_data['2fa_delay'] = delay
    await ask_for_2fa_password(update, context)
    return AWAIT_PASSWORD_2FA

@owner_only
async def handle_2fa_password_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the password input."""
    password_input = update.message.text.strip()
    context.user_data['new_2fa_password'] = password_input
    
    await update.message.reply_html(
        "💭 <b>Send the new 2FA HINT</b> 💡 for your selected accounts\n\n"
        "ℹ️ You can reply with <code>#empty#</code> to set no 2FA HINT."
    )
    return AWAIT_HINT_2FA

@owner_only
async def handle_2fa_hint_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the hint input and initiates the processing queue."""
    hint_input = update.message.text.strip()
    
    # Store settings
    context.user_data['new_2fa_hint'] = hint_input
    selected_accounts = context.user_data.get('selected_accounts', set())
    
    # Initialize Queue
    context.user_data['pending_2fa_ids'] = list(selected_accounts)
    context.user_data['2fa_results'] = []
    context.user_data['current_2fa_user_id'] = None # Used for retries
    
    await update.message.reply_text(f"🚀 Starting 2FA update for {len(selected_accounts)} accounts...")
    
    # Start Processing
    return await process_2fa_queue(update, context)

async def process_2fa_queue(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Processes the queue of accounts for 2FA updates.
    Returns:
      - ConversationHandler.END if finished.
      - AWAIT_CURRENT_2FA_PASSWORD if an interruption occurs.
    """
    results = context.user_data.get('2fa_results', [])
    
    delay = context.user_data.get('2fa_delay', 5)
    new_password = context.user_data.get('new_2fa_password')
    hint_input = context.user_data.get('new_2fa_hint')
    
    disable_mode = (new_password == "#empty#")
    target_password = new_password if not disable_mode else None
    target_hint = hint_input if hint_input != "#empty#" else None

    # Loop until we run out of accounts OR we hit an interruption
    # --- FIX: Refactored loop to correctly manage retry IDs ---
    while True:
        pending_ids = context.user_data.get('pending_2fa_ids', [])
        current_retry_id = context.user_data.get('current_2fa_user_id')
        
        if not pending_ids and not current_retry_id:
            break
        
        # Determine which user to process
        if current_retry_id:
            user_id = current_retry_id
        else:
            user_id = pending_ids.pop(0)
            # Add delay only if it's a new account from the queue (not the first one)
            if len(results) > 0 and delay > 0:
                await asyncio.sleep(delay)

        account_name = f"ID: {user_id}"
        
        if user_id not in active_userbots:
            results.append(f"❌ {account_name}: Bot is OFFLINE.")
            context.user_data['current_2fa_user_id'] = None # Reset retry
            continue

        client = active_userbots[user_id]
        if client.me:
             account_name = escape_html(client.me.first_name)

        # Get stored password
        try:
            account_doc = await asyncio.to_thread(
                accounts_collection.find_one,
                {"user_id": user_id}
            )
        except Exception:
            account_doc = None
        current_db_pwd = account_doc.get("two_fa_password") if account_doc else None
        
        try:
            if disable_mode:
                if current_db_pwd:
                    await client.disable_cloud_password(password=current_db_pwd)
                    results.append(f"✅ {account_name}: 2FA Disabled.")
                else:
                    # Try without password
                    try:
                        await client.disable_cloud_password() 
                        results.append(f"✅ {account_name}: 2FA Disabled (No pwd required).")
                    except (PasswordHashInvalid, BadRequest):
                        # INTERRUPTION NEEDED
                        context.user_data['current_2fa_user_id'] = user_id # Set for retry
                        await update.message.reply_html(
                            f"🔐 <b>Current 2FA Password Required</b>\n\n"
                            f"I cannot disable 2FA for account <b>{account_name}</b> because the stored password is missing or incorrect.\n\n"
                            f"Please send the <b>CURRENT</b> 2FA password for this account to continue.\n"
                            f"<i>(Send /skip to skip this account)</i>"
                        )
                        return AWAIT_CURRENT_2FA_PASSWORD # Pause execution here

            else:
                # Enabling/Changing
                if current_db_pwd:
                    # --- FIX: Changed hint=target_hint to new_hint=target_hint ---
                    await client.change_cloud_password(current_password=current_db_pwd, new_password=target_password, new_hint=target_hint)
                    results.append(f"✅ {account_name}: 2FA Changed.")
                else:
                    # Try enabling
                    try:
                        await client.enable_cloud_password(password=target_password, hint=target_hint)
                        results.append(f"✅ {account_name}: 2FA Enabled.")
                    except (BadRequest, Exception) as inner_e:
                        if "PASSWORD_ALREADY_ENABLED" in str(inner_e) or "cloud password" in str(inner_e).lower():
                             # INTERRUPTION NEEDED
                             context.user_data['current_2fa_user_id'] = user_id # Set for retry
                             await update.message.reply_html(
                                f"🔐 <b>Current 2FA Password Required</b>\n\n"
                                f"2FA is already enabled for <b>{account_name}</b>, but I don't have the stored password to change it.\n\n"
                                f"Please send the <b>CURRENT</b> 2FA password for this account to continue.\n"
                                f"<i>(Send /skip to skip this account)</i>"
                             )
                             return AWAIT_CURRENT_2FA_PASSWORD # Pause execution here
                        else:
                             raise inner_e

            # If success, update stored password
            final_stored_pwd = target_password if not disable_mode else None
            await asyncio.to_thread(
                accounts_collection.update_one,
                {"user_id": user_id},
                {"$set": {"two_fa_password": final_stored_pwd}}
            )
            # Clear retry ID since success
            context.user_data['current_2fa_user_id'] = None

        except PasswordHashInvalid:
             # INTERRUPTION NEEDED (Wrong Password)
             context.user_data['current_2fa_user_id'] = user_id
             await update.message.reply_html(
                f"🔐 <b>Incorrect 2FA Password</b>\n\n"
                f"The stored password for <b>{account_name}</b> was incorrect.\n\n"
                f"Please send the <b>CORRECT CURRENT</b> 2FA password to continue.\n"
                f"<i>(Send /skip to skip this account)</i>"
             )
             return AWAIT_CURRENT_2FA_PASSWORD
             
        except Exception as e:
            results.append(f"⚠️ {account_name}: {e}")
            context.user_data['current_2fa_user_id'] = None

    # Loop Finished
    final_text = "<b>2FA Batch Update Complete</b>\n\n" + "\n".join(results)
    if len(final_text) > 4000:
        final_text = final_text[:4000] + "\n... (truncated)"
        
    await update.message.reply_html(final_text)
    context.user_data.clear()
    return ConversationHandler.END

@owner_only
async def hard_delete_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Forcefully removes ALL documents with a specific user_id.
    Cleans Active Memory (RAM) AND Database.
    Usage: /nuke <user_id>
    """
    if not context.args:
        await update.message.reply_text("Usage: /nuke <user_id>")
        return

    try:
        target_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ Please provide a valid integer User ID.")
        return

    messages = []
    
    # 1. NUCLEAR CLEANUP: Active Memory (RAM)
    # Check if this ID is currently stuck in the running bot list
    if target_id in active_userbots:
        try:
            client = active_userbots[target_id]
            if client.is_connected:
                await client.stop()
            del active_userbots[target_id]
            messages.append(f"🧠 <b>Memory:</b> Removed stuck active session.")
        except Exception as e:
            messages.append(f"🧠 <b>Memory:</b> Error stopping session: {e}")
    else:
        messages.append("🧠 <b>Memory:</b> No active session found.")

    # 2. Cleanup: Scheduled Jobs
    # Stop any online/offline ping jobs for this ID
    if target_id in active_online_jobs:
        stop_online_job(target_id)
        messages.append("⌚ <b>Jobs:</b> Stopped background tasks.")

    # 3. NUCLEAR CLEANUP: Database
    if accounts_collection is not None:
        # Perform Hard Delete
        result = await asyncio.to_thread(
            accounts_collection.delete_many,
            {"user_id": target_id}
        )
        if result.deleted_count > 0:
             messages.append(f"💽 <b>Database:</b> Deleted {result.deleted_count} records.")
        else:
             messages.append("💽 <b>Database:</b> No records found.")
    else:
        messages.append("⚠️ <b>Database:</b> Connection failed.")

    # Send summary
    final_text = f"☢️ <b>NUCLEAR REPORT for {target_id}</b>\n\n" + "\n".join(messages)
    await update.message.reply_html(final_text)

@owner_only
async def handle_current_2fa_password_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Receives the missing 2FA password, updates DB, and resumes the queue.
    """
    password = update.message.text.strip()
    user_id = context.user_data.get('current_2fa_user_id')
    
    if not user_id:
        await update.message.reply_text("Error: Lost track of the account ID. Aborting.")
        return ConversationHandler.END
        
    # Update DB with the provided password
    await asyncio.to_thread(
        accounts_collection.update_one,
        {"user_id": user_id},
        {"$set": {"two_fa_password": password}}
    )
    
    await update.message.reply_text("✅ Password saved. Retrying...")
    
    # Resume Queue (The user_id is still set in current_2fa_user_id, so it will retry)
    return await process_2fa_queue(update, context)

@owner_only
async def skip_current_2fa_account(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Skipts the current account in the 2FA queue."""
    user_id = context.user_data.get('current_2fa_user_id')
    results = context.user_data.get('2fa_results', [])
    
    if user_id:
        results.append(f"⏩ ID {user_id}: Skipped by user.")
        context.user_data['current_2fa_user_id'] = None # Clear so loop moves to next
    
    await update.message.reply_text("⏩ Account skipped.")
    return await process_2fa_queue(update, context)

@owner_only
async def cancel_2fa_conv(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancels the 2FA conversation."""
    context.user_data.clear()
    msg = "✖️ 2FA configuration cancelled."
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text(msg)
    else:
        await update.message.reply_text(msg)
    return ConversationHandler.END

# --- 2FA Conversation Handler Definition ---
two_fa_conv = ConversationHandler(
    entry_points=[CommandHandler("2fas", two_fa_start)],
    states={
        AWAIT_BUTTON_2FA: [CallbackQueryHandler(two_fa_menu, pattern="^2fa_start_selection$")],
        SELECT_ACCOUNTS_2FA: [CallbackQueryHandler(handle_account_selection_callback_2fa, pattern="^2fa_")],
        AWAIT_DELAY_2FA: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_2fa_delay_input)],
        AWAIT_PASSWORD_2FA: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_2fa_password_input)],
        AWAIT_HINT_2FA: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_2fa_hint_input)],
        # --- NEW STATE ---
        AWAIT_CURRENT_2FA_PASSWORD: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, handle_current_2fa_password_input),
            CommandHandler("skip", skip_current_2fa_account)
        ]
    },
    fallbacks=[
        CommandHandler("cancel", cancel_2fa_conv),
        CallbackQueryHandler(cancel_2fa_conv, pattern="^cancel_2fa_conv$"),
        *COMMAND_FALLBACKS
    ],
    conversation_timeout=600
)
