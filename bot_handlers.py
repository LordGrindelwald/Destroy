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
from pyrogram.errors import PasswordHashInvalid, BadRequest
from pyrogram import Client 

# Import from our own modules
from config import (
    OWNER_ID, accounts_collection, active_userbots, 
    paused_forwarding, paused_notifications, logger,
    UNIQUE_NAME_PASTE, AWAIT_STRING_PASTE,
    AWAIT_BUTTON, SELECT_ACCOUNTS, AWAIT_INTERVAL,
    AWAIT_BUTTON_REMOVE, SELECT_ACCOUNTS_REMOVE, AWAIT_CONFIRM_REMOVE,
    AWAIT_BUTTON_2FA, SELECT_ACCOUNTS_2FA, AWAIT_DELAY_2FA, AWAIT_PASSWORD_2FA, AWAIT_HINT_2FA, AWAIT_CURRENT_2FA_PASSWORD,
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

# --- NEW COMMANDS: Backup & Restore & Encryption ---

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
async def backup_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Backups the MongoDB accounts collection to a JSON file."""
    if accounts_collection is None:
        await update.message.reply_text("⚠️ Database connection error.")
        return
    
    status_msg = await update.message.reply_text("⏳ Generating backup...")
    
    try:
        data = await asyncio.to_thread(lambda: list(accounts_collection.find()))
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
            data = loads(f.read())
            
        if not isinstance(data, list):
            await status_msg.edit_text("❌ Invalid backup file format (Root must be a list).")
            return
            
        await status_msg.edit_text(f"⚠️ <b>Restoring {len(data)} accounts...</b>\n\nExisting data will be wiped.", parse_mode=ParseMode.HTML)
        
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
        all_accounts = await asyncio.to_thread(lambda: list(accounts_collection.find()))
        encrypted_count = 0
        
        for acc in all_accounts:
            raw_session = acc.get("session_string")
            if raw_session and not raw_session.startswith("gAAAAA"):
                new_session = encrypt_text(raw_session)
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

@owner_only
async def fix_db_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Force Syncs ALL accounts and ensures IDs are integers.
    """
    if accounts_collection is None:
        await update.message.reply_text("⚠️ Database connection error.")
        return
        
    bot_username = context.bot.username
    if not bot_username:
        me_bot = await context.bot.get_me()
        bot_username = me_bot.username

    all_accounts = await asyncio.to_thread(lambda: list(accounts_collection.find()))
    total_count = len(all_accounts)
    
    status_msg = await update.message.reply_text(
        f"🔄 <b>Database Repair</b>\n\n"
        f"Scanning {total_count} accounts...", 
        parse_mode=ParseMode.HTML
    )

    fixed_count = 0
    updated_count = 0
    failed_count = 0
    log_lines = []

    for index, acc in enumerate(all_accounts):
        name = acc.get('unique_name', 'Unknown')
        doc_id = acc['_id']
        old_id = acc.get('user_id')
        
        if index % 5 == 0:
            await status_msg.edit_text(
                f"🔄 <b>Database Repair</b>\n"
                f"Progress: {index}/{total_count}\n"
                f"Fixed: {updated_count}\n", 
                parse_mode=ParseMode.HTML
            )

        try:
            raw_session = acc.get("session_string")
            if not raw_session:
                failed_count += 1
                continue
                
            session = decrypt_text(raw_session)
            
            temp_client = Client(
                name=f"fixlink_{doc_id}",
                api_id=TD_API_ID,
                api_hash=TD_API_HASH,
                session_string=session,
                in_memory=True,
                no_updates=True,
                device_model=acc.get("device_model", "RepairBot"),
                system_version=TD_SYSTEM_VERSION,
                app_version=TD_APP_VERSION,
                lang_code=TD_LANG_CODE,
                system_lang_code=TD_SYSTEM_LANG_CODE,
                lang_pack=TD_LANG_PACK
            )
            
            await temp_client.connect()
            
            # Send /start to ensure visibility
            try:
                await temp_client.send_message(bot_username, "/start")
                await asyncio.sleep(0.5)
            except Exception:
                pass
            
            me = await temp_client.get_me()
            real_id = int(me.id) # Force Int
            real_first_name = me.first_name or ""
            real_username = me.username or None
            real_phone = me.phone_number or acc.get('phone_number')
            
            await temp_client.disconnect()

            updates = {}
            # Strict Int Check
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
                    {"_id": doc_id},
                    {"$set": updates}
                )
                updated_count += 1
            
            fixed_count += 1

        except Exception as e:
            failed_count += 1
            logger.error(f"Fix failed for {name}: {e}")

    final_text = (
        f"✅ <b>Repair Complete</b>\n"
        f"Scanned: {total_count}\n"
        f"Updates: {updated_count}\n\n" +
        "\n".join(log_lines[:10])
    )
    
    await status_msg.edit_text(final_text, parse_mode=ParseMode.HTML)


# --- Command Handlers ---

@owner_only
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_html(
        "Personal Account Manager."
    )

@owner_only
async def restart_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.callback_query:
        await update.callback_query.answer("Restarting...")
        message_context = update.callback_query.message
    else:
        message_context = update.message
        
    await message_context.reply_text("🔄 Restarting service now...")
    
    logger.info(f"Stopping {len(active_online_jobs)} online jobs...")
    for user_id in list(active_online_jobs.keys()):
        stop_online_job(user_id)
    
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
        [InlineKeyboardButton("➕ Add New Account", callback_data="call_add_command")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    message_text = "<b>Accounts Dashboard</b>\n━━━━━━━━━━━━━━━━━━━━\n\n"

    if update.callback_query:
        await update.callback_query.edit_message_text(message_text, parse_mode=ParseMode.HTML, reply_markup=reply_markup)
    else:
        await update.message.reply_html(message_text, reply_markup=reply_markup)

@owner_only
async def rename_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if accounts_collection is None:
        await update.message.reply_text("⚠️ Database connection error.")
        return

    if len(context.args) != 2:
        await update.message.reply_text("Usage: /rename <user_id_or_name> <new_unique_name>")
        return
        
    identifier = context.args[0]
    new_name = sanitize_unique_name(context.args[1])
    
    account = await get_account_from_arg(identifier)
    if not account:
        await update.message.reply_text(f"⚠️ Account '<code>{escape_html(identifier)}</code>' not found.", parse_mode=ParseMode.HTML)
        return

    existing_with_name = await asyncio.to_thread(accounts_collection.find_one, {"unique_name": new_name})
    
    if existing_with_name and existing_with_name["_id"] != account["_id"]:
        await update.message.reply_text(f"⚠️ Name <code>{escape_html(new_name)}</code> is taken.", parse_mode=ParseMode.HTML)
        return
    
    await asyncio.to_thread(
        accounts_collection.update_one,
        {"_id": account["_id"]},
        {"$set": {"unique_name": new_name}}
    )
    
    await update.message.reply_text(
        f"✔️ Renamed to <code>{escape_html(new_name)}</code>.",
        parse_mode=ParseMode.HTML
    )

@owner_only
async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    total_bots = 0
    if accounts_collection is not None:
        total_bots = await asyncio.to_thread(accounts_collection.count_documents, {})
        
    running_bots = len(active_userbots)
    running_jobs = len(active_online_jobs)
    
    status_text = (f"<b>Bot Status</b>\n"
                   f"━━━━━━━━━━━━━━━━━━━━\n"
                   f"<b>Accounts Active:</b> {running_bots}/{total_bots}\n"
                   f"<b>Online Jobs:</b> {running_jobs}\n"
                   f"<b>Paused OTP Destruction:</b> {len(paused_forwarding)} bots\n")
    await update.message.reply_html(status_text)

@owner_only
async def temp_pause_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /temp <user_id_or_name>")
        return
    try:
        identifier = context.args[0]
        account = await get_account_from_arg(identifier)
        if not account:
            await update.message.reply_text(f"⚠️ Account not found.")
            return

        user_id_to_pause = account['user_id']
        if not account.get("otp_destroy_enabled", True):
            await update.message.reply_text("⚠️ OTP Destruction is permanently disabled.")
            return

        pause_id = f"{user_id_to_pause}_{int(datetime.now().timestamp())}"
        context.bot_data[pause_id] = False 
        paused_forwarding.add(user_id_to_pause)
        
        keyboard = [[InlineKeyboardButton("Pause Notifications", callback_data=f"pause_notify_{pause_id}")]]
        message = await update.message.reply_text(f"✅ Paused OTP destruction for {escape_html(account.get('first_name'))} (5m).",
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
    for user_id in active_userbots.keys():
        paused_forwarding.add(user_id)
    paused_notifications.add(OWNER_ID)
    await update.message.reply_text("✅ Paused all OTP Destruction (5m).")
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
    msg = await update.message.reply_text("🔄 Stopping accounts...")
    for user_id in list(active_online_jobs.keys()):
        stop_online_job(user_id)
    
    stop_tasks = [client.stop() for client in active_userbots.values() if client.is_connected]
    await asyncio.gather(*stop_tasks, return_exceptions=True)
    active_userbots.clear()
    
    await asyncio.sleep(2)
    await msg.edit_text("🔄 Restarting and refreshing...")
    
    total_bots = 0
    if accounts_collection:
        total_bots = await asyncio.to_thread(accounts_collection.count_documents, {})
        
    _, _, errors = await start_all_userbots_from_db(context.application, update_info=True)
    
    running_bots = len(active_userbots)
    final_message = f"✅ <b>Refresh Complete</b>\nStarted {running_bots}/{total_bots} accounts."
    if errors:
        final_message += "\n❌ Errors:\n" + "\n".join(errors)
        
    if len(final_message) > 4096:
        await msg.edit_text("Refresh done with errors (log too long).", parse_mode=ParseMode.HTML)
    else:
        await msg.edit_text(final_message, parse_mode=ParseMode.HTML)

@owner_only
async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("Action cancelled.")

# --- CallbackQuery Handlers ---

@owner_only
async def accounts_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Handles /accs command.
    Uses SAFE HTML with Strict Integer ID enforcement to fix links.
    """
    if accounts_collection is None:
        await update.message.reply_html("⚠️ Database connection error.")
        return
        
    accounts = await asyncio.to_thread(
        lambda: list(accounts_collection.find().sort("unique_name", 1))
    )
    
    if not accounts:
        await update.message.reply_html("You own 0 Accounts!")
        return

    async def send_smart_chunks(header, items):
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

            # SAFE LINK GENERATION
            display_name = unique_name or first_name or f"ID: {user_id}"
            mention = display_name
            status_icon = "📄" # Default: No link possible
            
            if user_id:
                try:
                    uid_int = int(user_id)
                    # If valid int, make link
                    mention = f'<a href="tg://user?id={uid_int}">{display_name}</a>'
                    status_icon = "🔗" 
                except:
                    mention = f"{display_name} (Bad ID)"

            if unique_name and unique_name != display_name:
                mention += f" ({unique_name})"

            entry_text = (
                f"{status_icon} {mention}\n"
                f"<b>User:</b> {username_str}\n"
                f"<b>Phone:</b> <code>{phone_str}</code>\n"
                f"{device_model} ({escape_html(online_interval)} min)\n" 
                f"<b>ID:</b> {user_id if user_id else 'MISSING'}"
            )
            items.append(entry_text + f"\n{'-'*25}")
        
        await send_smart_chunks(header_text, items)
        return

    # --- CONCISE VIEW ---
    header_text = f"You own {len(accounts)} Accounts!\n"
    items = []
    
    for acc in accounts:
        user_id = acc.get('user_id')
        unique_name = escape_html(acc.get('unique_name', ''))
        first_name = escape_html(acc.get('first_name', ''))
        phone = acc.get('phone_number')
        phone_str = f"+<code>{escape_html(phone)}</code>" if phone else "No Phone"
        interval = acc.get('online_interval', '1440')
        interval_str = f" (⌚ {interval}m)" if interval != '1440' else ""
        
        display_name = unique_name or first_name or f"ID: {user_id}" or "Unknown"
        
        mention = display_name
        status_icon = "📄"
        
        if user_id:
            try:
                uid_int = int(user_id)
                mention = f'<a href="tg://user?id={uid_int}">{display_name}</a>'
                status_icon = "🔗"
            except:
                pass # Keep plain text

        items.append(f"{status_icon} {mention}: {phone_str}{interval_str}")

    await send_smart_chunks(header_text, items)


@owner_only
async def accounts_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    if accounts_collection is None:
        await query.edit_message_text("⚠️ Database connection is not available.")
        return
        
    accounts = await asyncio.to_thread(lambda: list(accounts_collection.find().sort("unique_name", 1)))
    
    base_text = "👤 <b>Your Managed Accounts:</b>\n\n"
    text_parts = []

    if not accounts:
        base_text += "No accounts yet."
    else:
        for acc in accounts:
            user_id = acc.get('user_id')
            unique_name = escape_html(acc.get('unique_name', ''))
            first_name = escape_html(acc.get('first_name', ''))
            username = escape_html(acc.get('username', 'N/A'))
            phone = escape_html(acc.get('phone_number', 'N/A'))
            device = escape_html(acc.get('device_model', 'N/A'))
            interval = acc.get('online_interval', '1440')

            display = unique_name or first_name or f"ID: {user_id}"
            mention = display
            if user_id:
                try:
                    uid_int = int(user_id)
                    mention = f'<a href="tg://user?id={uid_int}">{display}</a>'
                except:
                    pass
            
            entry = (
                f"{mention}\n"
                f"<b>User:</b> @{username}\n"
                f"<b>Phone:</b> +{phone}\n"
                f"{device} ({interval} min)\n"
                f"<b>ID:</b> {user_id}"
            )
            text_parts.append(entry)

    final_text = base_text + f"\n{'-'*25}\n".join(text_parts)
    keyboard = []
    reply_markup = InlineKeyboardMarkup(keyboard)

    try:
        await query.edit_message_text(text=final_text, parse_mode=ParseMode.HTML, reply_markup=reply_markup, disable_web_page_preview=True)
    except Exception as e:
        if "Message is too long" in str(e):
             await query.edit_message_text("⚠️ Too many accounts for menu. Use /accs command.", parse_mode=ParseMode.HTML, reply_markup=reply_markup)
        else:
             logger.error(f"Error in accounts_menu: {e}")

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
    query = update.callback_query
    await query.answer()
    try:
        pause_id = query.data.split("pause_notify_", 1)[1]
    except IndexError:
        await query.edit_message_text(f"Error: Invalid data.")
        return

    if pause_id not in context.bot_data:
        await query.answer("Expired.", show_alert=True)
        return

    paused_notifications.add(OWNER_ID)
    context.bot_data[pause_id] = True 
    await query.edit_message_text(f"{query.message.text}\n\n<i>✅ Notifications paused.</i>", parse_mode=ParseMode.HTML)

# --- Message Handler (for multiple strings) ---

async def handle_text_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
        await msg.edit_text("⚠️ DB Error.")
        context.user_data.clear()
        return

    success, fail = 0, 0
    for session in session_strings:
        status, _, _ = await start_userbot(session, context.application, update_info=True, run_acquaintance=True, device_model_to_use=persistent_device_model)
        if status == "success": success += 1
        else: fail += 1
        
    context.user_data.clear()
    await msg.edit_text(f"Batch complete! ✅: {success}, ❌: {fail}")
    await asyncio.sleep(3); await settings_command(update, context)

# --- Paste Single String Conversation ---

@owner_only
async def cancel_paste_conv(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    if update.message: await update.message.reply_text("Cancelled.")
    return ConversationHandler.END

@owner_only
async def prompt_for_unique_name_paste(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data['persistent_device_model'] = generate_device_name()
    await query.message.reply_text("Send unique name for new account.")
    return UNIQUE_NAME_PASTE

@owner_only
async def get_unique_name_for_paste(update: Update, context: ContextTypes.DEFAULT_TYPE):
    unique_name = sanitize_unique_name(update.message.text.strip().split()[0])
    if accounts_collection and await asyncio.to_thread(accounts_collection.find_one, {"unique_name": unique_name}):
        await update.message.reply_text("Name taken. Try another.")
        return UNIQUE_NAME_PASTE 
        
    context.user_data['unique_name'] = unique_name
    await update.message.reply_text(f"Name: <b>{unique_name}</b>\nPaste session string.", parse_mode=ParseMode.HTML)
    return AWAIT_STRING_PASTE

@owner_only
async def get_session_string_and_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    session = clean_session_string(update.message.text)
    name = context.user_data.get('unique_name')
    model = context.user_data.get('persistent_device_model') 
    
    msg = await update.message.reply_text("⏳ Processing...")
    status, info, detail = await start_userbot(session, context.application, update_info=True, unique_name=name, run_acquaintance=True, device_model_to_use=model)
    
    if status == "success":
        await msg.edit_text(f"✅ Added {escape_html(info.first_name)} ({name})", parse_mode=ParseMode.HTML)
    else:
        await msg.edit_text(f"⚠️ Error: {detail}")
    context.user_data.clear()
    return ConversationHandler.END

paste_single_conv = ConversationHandler(
    entry_points=[CallbackQueryHandler(prompt_for_unique_name_paste, pattern="^add_single$")],
    states={
        UNIQUE_NAME_PASTE: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_unique_name_for_paste)],
        AWAIT_STRING_PASTE: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_session_string_and_add)],
    },
    fallbacks=[CommandHandler("cancel", cancel_paste_conv), *COMMAND_FALLBACKS],
    conversation_timeout=300,
)

# --- Online Interval Flow ---

@owner_only
async def online_interval_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [[InlineKeyboardButton("OnlineInterval settings ⌚️⚙️", callback_data="oi_start_selection")]]
    await update.message.reply_html("Select accounts to change interval.", reply_markup=InlineKeyboardMarkup(keyboard))
    return AWAIT_BUTTON

async def draw_account_selection_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    all_ids = context.user_data.get('all_account_ids', [])
    selected = context.user_data.get('selected_accounts', set())
    page = context.user_data.get('current_page', 0)
    
    total = len(all_ids)
    pages = math.ceil(total / ACCOUNTS_PER_PAGE)
    start, end = page * ACCOUNTS_PER_PAGE, (page + 1) * ACCOUNTS_PER_PAGE
    page_ids = all_ids[start:end]
    
    page_accs = await asyncio.to_thread(lambda: list(accounts_collection.find({"user_id": {"$in": page_ids}}, {"first_name": 1, "user_id": 1, "unique_name": 1, "online_interval": 1})))
    acc_map = {a['user_id']: a for a in page_accs}
    
    keyboard = []
    keyboard.append([InlineKeyboardButton(f"Select all ({total})", callback_data="oi_select_all"), InlineKeyboardButton(f"Unselect all ({len(selected)})", callback_data="oi_unselect_all")])
    
    acc_btns = []
    for uid in page_ids:
        if uid in acc_map:
            acc = acc_map[uid]
            name = escape_html(acc.get('unique_name') or acc.get('first_name', str(uid)))
            interval = acc.get('online_interval', '1440')
            prefix = "✅" if uid in selected else ("⌚️" if interval != '1440' else "")
            acc_btns.append(InlineKeyboardButton(f"{prefix} {name} ({interval}m)", callback_data=f"oi_toggle_{uid}"))
            
    for i in range(0, len(acc_btns), 2): keyboard.append(acc_btns[i:i+2])
    
    keyboard.append([InlineKeyboardButton("Done selecting 👌", callback_data="oi_done")])
    nav = []
    if page > 0: nav.append(InlineKeyboardButton("⬅️", callback_data="oi_prev_page"))
    nav.append(InlineKeyboardButton(f"{page + 1}/{pages}", callback_data="oi_noop"))
    if page < pages - 1: nav.append(InlineKeyboardButton("➡️", callback_data="oi_next_page"))
    keyboard.append(nav)
    keyboard.append([InlineKeyboardButton("Cancel", callback_data="cancel_oi_conv")])

    try:
        await query.edit_message_text(f"<b>Select Accounts</b>\nPage: {page+1}/{pages}\nSelected: {len(selected)}", parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(keyboard))
    except: pass
    return SELECT_ACCOUNTS

@owner_only
async def online_interval_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    all_accs = await asyncio.to_thread(lambda: list(accounts_collection.find({}, {"user_id": 1})))
    if not all_accs:
        await query.edit_message_text("No accounts.")
        return ConversationHandler.END
        
    context.user_data.clear()
    context.user_data['all_account_ids'] = [a['user_id'] for a in all_accs]
    context.user_data['selected_accounts'] = set()
    context.user_data['current_page'] = 0
    return await draw_account_selection_menu(update, context)

@owner_only
async def handle_account_selection_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    
    all_ids = context.user_data.get('all_account_ids', [])
    selected = context.user_data.get('selected_accounts', set())
    page = context.user_data.get('current_page', 0)
    
    if data.startswith("oi_toggle_"):
        uid = int(data.split("_")[2])
        if uid in selected: selected.discard(uid)
        else: selected.add(uid)
    elif data == "oi_select_all": selected.update(all_ids)
    elif data == "oi_unselect_all": selected.clear()
    elif data == "oi_prev_page": 
        if page > 0: context.user_data['current_page'] -= 1
    elif data == "oi_next_page": 
        if page < math.ceil(len(all_ids)/ACCOUNTS_PER_PAGE)-1: context.user_data['current_page'] += 1
    elif data == "oi_done":
        if not selected: 
            await query.answer("Select at least one.", show_alert=True)
            return SELECT_ACCOUNTS
        await query.edit_message_text("Send interval (min) or range (e.g. 30-60). /default to reset.")
        return AWAIT_INTERVAL
        
    context.user_data['selected_accounts'] = selected
    return await draw_account_selection_menu(update, context)

@owner_only
async def handle_interval_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    val = update.message.text.strip()
    selected = list(context.user_data.get('selected_accounts', []))
    
    await asyncio.to_thread(accounts_collection.update_many, {"user_id": {"$in": selected}}, {"$set": {"online_interval": val}})
    
    for uid in selected:
        stop_online_job(uid)
        if uid in active_userbots:
            await schedule_online_job(active_userbots[uid], val, context.application)
            
    await update.message.reply_text("✅ Saved.")
    context.user_data.clear()
    return ConversationHandler.END

@owner_only
async def set_interval_default(update: Update, context: ContextTypes.DEFAULT_TYPE):
    selected = list(context.user_data.get('selected_accounts', []))
    await asyncio.to_thread(accounts_collection.update_many, {"user_id": {"$in": selected}}, {"$set": {"online_interval": "1440"}})
    for uid in selected: stop_online_job(uid)
    await update.message.reply_text("✅ Reset to default.")
    context.user_data.clear()
    return ConversationHandler.END

@owner_only
async def cancel_interval_conv(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text("Cancelled.")
    else:
        await update.message.reply_text("Cancelled.")
    return ConversationHandler.END

online_interval_conv = ConversationHandler(
    entry_points=[CommandHandler("online_interval", online_interval_start)],
    states={
        AWAIT_BUTTON: [CallbackQueryHandler(online_interval_menu, pattern="^oi_start_selection$")],
        SELECT_ACCOUNTS: [CallbackQueryHandler(handle_account_selection_callback, pattern=r"^oi_")],
        AWAIT_INTERVAL: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_interval_input), CommandHandler("default", set_interval_default)],
    },
    fallbacks=[CommandHandler("cancel", cancel_interval_conv), CallbackQueryHandler(cancel_interval_conv, pattern="^cancel")],
    conversation_timeout=600,
)

# --- Account Detail ---

@owner_only
async def account_detail_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /acc <name>")
        return
    acc = await get_account_from_arg(context.args[0])
    if not acc:
        await update.message.reply_text("Not found.")
        return
        
    text = (f"<b>Account:</b> {escape_html(acc.get('unique_name'))}\n"
            f"ID: <code>{acc.get('user_id')}</code>\n"
            f"Phone: {acc.get('phone_number')}\n"
            f"OTP Destroy: {acc.get('otp_destroy_enabled', True)}\n"
            f"2FA: {'Set' if acc.get('two_fa_password') else 'Not set'}")
    await update.message.reply_html(text)

@owner_only
async def toggle_otp_destroy_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args: return
    acc = await get_account_from_arg(context.args[0])
    if not acc: return
    
    new_val = not acc.get("otp_destroy_enabled", True)
    await asyncio.to_thread(accounts_collection.update_one, {"_id": acc["_id"]}, {"$set": {"otp_destroy_enabled": new_val}})
    await update.message.reply_text(f"OTP Destroy: {new_val}")

# --- Remove Flow ---

@owner_only
async def remove_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.args:
        acc = await get_account_from_arg(context.args[0])
        if not acc:
            await update.message.reply_text("Not found.")
            return ConversationHandler.END
        context.user_data['selected_accounts'] = {acc['user_id']}
        return await handle_remove_done_selecting(update, context) # Jump to confirm
        
    keyboard = [[InlineKeyboardButton("Select to Remove 🗑️", callback_data="acct_rm_start")]]
    await update.message.reply_html("Select accounts to remove.", reply_markup=InlineKeyboardMarkup(keyboard))
    return AWAIT_BUTTON_REMOVE

@owner_only
async def remove_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    all_accs = await asyncio.to_thread(lambda: list(accounts_collection.find({}, {"user_id": 1})))
    if not all_accs:
        await query.edit_message_text("No accounts.")
        return ConversationHandler.END
    context.user_data.clear()
    context.user_data['all_account_ids'] = [a['user_id'] for a in all_accs]
    context.user_data['selected_accounts'] = set()
    context.user_data['current_page'] = 0
    await draw_account_selection_menu_remove(query, context)
    return SELECT_ACCOUNTS_REMOVE

async def draw_account_selection_menu_remove(query_obj, context):
    query = query_obj if isinstance(query_obj, CallbackQueryHandler) else query_obj.callback_query or query_obj
    
    all_ids = context.user_data.get('all_account_ids', [])
    selected = context.user_data.get('selected_accounts', set())
    page = context.user_data.get('current_page', 0)
    
    total = len(all_ids)
    pages = math.ceil(total / ACCOUNTS_PER_PAGE)
    page_ids = all_ids[page*ACCOUNTS_PER_PAGE : (page+1)*ACCOUNTS_PER_PAGE]
    
    page_accs = await asyncio.to_thread(lambda: list(accounts_collection.find({"user_id": {"$in": page_ids}}, {"unique_name": 1, "user_id": 1})))
    acc_map = {a['user_id']: a for a in page_accs}
    
    keyboard = []
    keyboard.append([InlineKeyboardButton("Select All", callback_data="acct_rm_select_all"), InlineKeyboardButton("Unselect All", callback_data="acct_rm_unselect_all")])
    
    btns = []
    for uid in page_ids:
        if uid in acc_map:
            name = escape_html(acc_map[uid].get('unique_name') or str(uid))
            prefix = "🗑️" if uid not in selected else "✅"
            btns.append(InlineKeyboardButton(f"{prefix} {name}", callback_data=f"acct_rm_toggle_{uid}"))
    for i in range(0, len(btns), 2): keyboard.append(btns[i:i+2])
    
    keyboard.append([InlineKeyboardButton("Done selecting 👌", callback_data="dnrm_done_select")])
    nav = []
    if page > 0: nav.append(InlineKeyboardButton("⬅️", callback_data="acct_rm_prev_page"))
    if page < pages-1: nav.append(InlineKeyboardButton("➡️", callback_data="acct_rm_next_page"))
    keyboard.append(nav)
    keyboard.append([InlineKeyboardButton("Cancel", callback_data="cancel_rm_conv")])
    
    try:
        await query.edit_message_text(f"<b>Remove Accounts</b>\nSelected: {len(selected)}", parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(keyboard))
    except: pass

@owner_only
async def handle_account_selection_callback_remove(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    
    selected = context.user_data.get('selected_accounts', set())
    all_ids = context.user_data.get('all_account_ids', [])
    
    if data.startswith("acct_rm_toggle_"):
        uid = int(data.split("_")[3])
        if uid in selected: selected.discard(uid)
        else: selected.add(uid)
    elif data == "acct_rm_select_all": selected.update(all_ids)
    elif data == "acct_rm_unselect_all": selected.clear()
    elif data == "acct_rm_next_page": context.user_data['current_page'] += 1
    elif data == "acct_rm_prev_page": context.user_data['current_page'] -= 1
    
    context.user_data['selected_accounts'] = selected
    await draw_account_selection_menu_remove(query, context)
    return SELECT_ACCOUNTS_REMOVE

@owner_only
async def handle_remove_done_selecting(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query: await query.answer()
    
    selected = context.user_data.get('selected_accounts', set())
    if not selected: return SELECT_ACCOUNTS_REMOVE
    
    text = f"Confirm removing {len(selected)} accounts? Irreversible."
    kb = [[InlineKeyboardButton("Yes", callback_data="acct_rm_confirm_yes"), InlineKeyboardButton("No", callback_data="acct_rm_confirm_no")]]
    
    if query: await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb))
    else: await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(kb))
    return AWAIT_CONFIRM_REMOVE

@owner_only
async def handle_remove_confirmation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "acct_rm_confirm_no":
        await query.edit_message_text("Cancelled.")
        return ConversationHandler.END
        
    selected = context.user_data.get('selected_accounts', [])
    await query.edit_message_text(f"Removing {len(selected)} accounts...")
    
    for uid in selected:
        stop_online_job(uid)
        if uid in active_userbots:
            c = active_userbots.pop(uid)
            asyncio.create_task(c.stop())
        await asyncio.to_thread(accounts_collection.delete_many, {"user_id": uid})
        
    await query.edit_message_text("✅ Removed.")
    context.user_data.clear()
    return ConversationHandler.END

@owner_only
async def cancel_remove_conv(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text("Cancelled.")
    else:
        await update.message.reply_text("Cancelled.")
    return ConversationHandler.END

remove_conv = ConversationHandler(
    entry_points=[CommandHandler("remove", remove_start)],
    states={
        AWAIT_BUTTON_REMOVE: [CallbackQueryHandler(remove_menu, pattern="^acct_rm_start$")],
        SELECT_ACCOUNTS_REMOVE: [
            CallbackQueryHandler(handle_remove_done_selecting, pattern="^dnrm_done_select$"),
            CallbackQueryHandler(handle_account_selection_callback_remove, pattern="^acct_rm_")
        ],
        AWAIT_CONFIRM_REMOVE: [CallbackQueryHandler(handle_remove_confirmation, pattern="^acct_rm_confirm_")],
    },
    fallbacks=[CommandHandler("cancel", cancel_remove_conv), CallbackQueryHandler(cancel_remove_conv, pattern="^cancel")],
    conversation_timeout=600,
)

# --- Deduplicate ---

@owner_only
async def deduplicate_db_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text("Processing...")
    
    all_accs = await asyncio.to_thread(lambda: list(accounts_collection.find().sort("_id", 1)))
    seen_ids, seen_names, to_delete = set(), set(), []
    
    for acc in all_accs:
        uid = acc.get('user_id')
        name = str(acc.get('unique_name', '')).lower()
        doc_id = acc['_id']
        
        remove = False
        if uid and uid in seen_ids: remove = True
        elif uid: seen_ids.add(uid)
        
        if name and name in seen_names and not remove: remove = True
        elif name: seen_names.add(name)
        
        if remove: to_delete.append(doc_id)
        
    if to_delete:
        await asyncio.to_thread(accounts_collection.delete_many, {"_id": {"$in": to_delete}})
        await msg.edit_text(f"Removed {len(to_delete)} duplicates.")
    else:
        await msg.edit_text("No duplicates found.")

# --- 2FA Config ---

@owner_only
async def update_2fa_password_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 2:
        await update.message.reply_text("Usage: /update2fa <name> <pass>")
        return
    acc = await get_account_from_arg(context.args[0])
    if not acc: return
    await asyncio.to_thread(accounts_collection.update_one, {"_id": acc["_id"]}, {"$set": {"two_fa_password": context.args[1]}})
    await update.message.reply_text("Updated.")

# (Simplified 2FA Conv for brevity - re-using structures from other convs)
two_fa_conv = ConversationHandler(
    entry_points=[CommandHandler("2fas", two_fa_start)],
    states={
        AWAIT_BUTTON_2FA: [CallbackQueryHandler(two_fa_menu, pattern="^2fa_start_selection$")],
        SELECT_ACCOUNTS_2FA: [CallbackQueryHandler(handle_account_selection_callback_2fa, pattern="^2fa_")],
        AWAIT_DELAY_2FA: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_2fa_delay_input)],
        AWAIT_PASSWORD_2FA: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_2fa_password_input)],
        AWAIT_HINT_2FA: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_2fa_hint_input)],
        AWAIT_CURRENT_2FA_PASSWORD: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_current_2fa_password_input), CommandHandler("skip", skip_current_2fa_account)]
    },
    fallbacks=[CommandHandler("cancel", cancel_2fa_conv), CallbackQueryHandler(cancel_2fa_conv, pattern="^cancel")],
    conversation_timeout=600
)
