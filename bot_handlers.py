import asyncio
import sys # Import sys for restart
import math 
from datetime import datetime
from functools import partial
from bson.objectid import ObjectId # <-- FIX: Import ObjectId
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

# Import from our own modules
from config import (
    OWNER_ID, accounts_collection, active_userbots, 
    paused_forwarding, paused_notifications, logger,
    UNIQUE_NAME_PASTE, AWAIT_STRING_PASTE,
    AWAIT_BUTTON, SELECT_ACCOUNTS, AWAIT_INTERVAL # <-- NEW STATES
)
from utils import owner_only, escape_html, clean_session_string, get_account_from_arg, generate_device_name
from userbot_logic import (
    start_userbot, start_all_userbots_from_db,
    stop_online_job, schedule_online_job, active_online_jobs # <-- NEW IMPORTS
)
from jobs import resume_forwarding_job, resume_all_job
from session_generator import cancel_command_conv # Re-use cancel logic

# --- Constants ---
ACCOUNTS_PER_PAGE = 16 # 8 rows * 2 columns

# --- Command Handlers ---

@owner_only
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_html(
        "👋 Welcome! I am your personal account manager.\n\n"
        "Use /settings to configure, /add to add, and /remove to delete accounts."
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
        [InlineKeyboardButton("➕ Add New Account", callback_data="call_add_command")]
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

@owner_only
async def add_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    keyboard = [
        [InlineKeyboardButton("📝 Paste Single String", callback_data="add_single")],
        [InlineKeyboardButton("📋 Paste Multiple Strings", callback_data="add_multiple")],
        [InlineKeyboardButton("📱 Generate via Phone Number", callback_data="call_generate")],
        [InlineKeyboardButton("« Back to Settings", callback_data="main_settings")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    message_text = (
        "➕  <b>Add a New Account</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "Please choose a method to add a new userbot account."
    )
    if update.callback_query:
        await update.callback_query.edit_message_text(message_text, parse_mode=ParseMode.HTML, reply_markup=reply_markup)
    else:
        await update.message.reply_html(message_text, reply_markup=reply_markup)

@owner_only
async def remove_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Handles /remove command.
    If args provided, attempts to remove directly.
    If no args, shows the button menu.
    """
    if context.args:
        identifier = context.args[0]
        
        if accounts_collection is None:
            await update.message.reply_text("⚠️ Database connection is not available. Please check logs.")
            return
            
        account = await get_account_from_arg(identifier)
        
        if not account:
            await update.message.reply_text(f"⚠️ Account '<code>{escape_html(identifier)}</code>' not found.", parse_mode=ParseMode.HTML)
            return

        user_id_to_delete = account['user_id']
        _id_to_delete = account['_id'] # <-- FIX: Get specific document ID
        
        # --- NEW: Stop online job ---
        stop_online_job(user_id_to_delete)
        # --- End ---
        
        if user_id_to_delete in active_userbots:
            logger.info(f"Stopping userbot client for user ID {user_id_to_delete}")
            await active_userbots[user_id_to_delete].stop()
            del active_userbots[user_id_to_delete]
            
        result = accounts_collection.delete_one({"_id": _id_to_delete}) # <-- FIX: Delete by _id
        
        if result.deleted_count > 0:
            await update.message.reply_text(f"✅ Account <code>{user_id_to_delete}</code> (<code>{escape_html(account.get('unique_name', 'N/A'))}</code>) has been successfully removed.", parse_mode=ParseMode.HTML)
        else:
            await update.message.reply_text(f"⚠️ Could not find account <code>{user_id_to_delete}</code> in the database.", parse_mode=ParseMode.HTML)
        
    else:
        # No args, show menu
        if accounts_collection is None:
            await update.message.reply_text("⚠️ Database connection is not available. Please check logs.")
            return
            
        accounts = list(accounts_collection.find())
        if not accounts:
            await update.message.reply_html("There are no accounts to remove.")
            return
        keyboard = []
        for acc in accounts:
            _id_str = str(acc.get('_id')) # <-- FIX: Get string of _id
            user_id = acc.get('user_id')
            name = escape_html(acc.get('first_name', f"ID: {user_id}"))
            unique_name = acc.get('unique_name')
            display_name = f"🗑️ {name}"
            if unique_name:
                display_name += f" ({escape_html(unique_name)})"
            
            button = [InlineKeyboardButton(display_name, callback_data=f"delete_account_{_id_str}")] # <-- FIX: Use _id string
            keyboard.append(button)
        keyboard.append([InlineKeyboardButton("« Back to Settings", callback_data="main_settings")])
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_html("Please select an account to remove:", reply_markup=reply_markup)

@owner_only
async def rename_command(update: Update, context: ContextTypes.DEFAULT_TYPE): # <-- FIX: Renamed function
    """Handles /rename <identifier> <new_name>""" # <-- FIX: Updated docstring
    
    if accounts_collection is None:
        await update.message.reply_text("⚠️ Database connection is not available. Please check logs.")
        return

    if len(context.args) != 2:
        await update.message.reply_text("Usage: /rename <user_id_or_name> <new_unique_name>") # <-- FIX: Updated usage text
        return
        
    identifier = context.args[0]
    new_name = context.args[1].lower()
    
    account = await get_account_from_arg(identifier)
    if not account:
        await update.message.reply_text(f"⚠️ Account '<code>{escape_html(identifier)}</code>' not found.", parse_mode=ParseMode.HTML)
        return

    existing_with_name = accounts_collection.find_one({"unique_name": new_name})
    if existing_with_name and existing_with_name["user_id"] != account["user_id"]:
        await update.message.reply_text(f"⚠️ The name C{escape_html(new_name)}</code> is already taken by account <code>{existing_with_name.get('user_id')}</code>.", parse_mode=ParseMode.HTML)
        return
        
    accounts_collection.update_one(
        {"user_id": account["user_id"]},
        {"$set": {"unique_name": new_name}}
    )
    
    await update.message.reply_text(
        f"✔️ name id for <b>{escape_html(account.get('first_name'))}</b> "
        f"(<code>{account['user_id']}</code>) has been set to <code>{escape_html(new_name)}</code>.",
        parse_mode=ParseMode.HTML
    )

@owner_only
async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    total_bots = 0
    if accounts_collection is not None:
        total_bots = accounts_collection.count_documents({})
        
    running_bots = len(active_userbots)
    running_jobs = len(active_online_jobs)
    
    status_text = (f"<b>Bot Status</b>\n"
                   f"━━━━━━━━━━━━━━━━━━━━\n"
                   f"<b>Accounts Active:</b> {running_bots}/{total_bots}\n"
                   f"<b>Online Jobs Active:</b> {running_jobs}\n" # <-- NEW
                   f"<b>Paused OTP Destruction:</b> {len(paused_forwarding)} bots\n"
                   f"<b>Paused OTP Forwarding:</b> {'Yes' if OWNER_ID in paused_notifications else 'No'}\n")
    await update.message.reply_html(status_text)

@owner_only
async def temp_pause_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Pauses a single account's OTP Destruction."""
    if not context.args:
        await update.message.reply_text("Usage: /temp <user_id_or_name>")
        return
        
    if accounts_collection is None:
        await update.message.reply_text("⚠️ Database connection is not available. Please check logs.")
        return

    try:
        identifier = context.args[0]
        account = await get_account_from_arg(identifier)
        
        if not account:
            await update.message.reply_text(f"⚠️ Account '<code>{escape_html(identifier)}</code>' not found.", parse_mode=ParseMode.HTML)
            return

        user_id_to_pause = account['user_id']
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
        total_bots = accounts_collection.count_documents({})
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

# --- CallbackQuery Handlers ---

@owner_only
async def accounts_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles /accs command. Shows list of accounts with no buttons."""
    if accounts_collection is None:
        await update.message.reply_html("⚠️ Database connection is not available. Please check logs.")
        return
        
    accounts = list(accounts_collection.find())
    
    base_text = "👤 <b>Your Managed Accounts:</b>\n\n"
    text_parts = []

    if not accounts:
        base_text += "No accounts have been added yet."
        await update.message.reply_html(base_text)
        return
    
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
        
        link_text_content = first_name or (f"ID: {user_id}" if user_id else "Unknown (Refresh required)")
        mention_link = f"<a href=\"tg://user?id={user_id}\">{link_text_content}</a>" if user_id else link_text_content
        name_display = mention_link
        if unique_name:
            name_display += f" ({unique_name})"

        entry_text = (
            f"{name_display}\n"
            f"<b>User:</b> {username_str}\n"
            f"<b>Phone:</b> <code>{phone_str}</code>\n"
            f"{escape_html(device_model)}"
            f" ({escape_html(online_interval)} min)\n" 
            f"<b>ID:</b> {user_id if user_id else 'N/A'}"
        )
        text_parts.append(entry_text)

    final_text = base_text + f"\n{'-'*25}\n".join(text_parts)
    
    await update.message.reply_html(final_text, disable_web_page_preview=True)

@owner_only
async def accounts_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    if accounts_collection is None:
        await query.edit_message_text("⚠️ Database connection is not available. Please check logs.")
        return
        
    accounts = list(accounts_collection.find())
    
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
            
            link_text_content = first_name or (f"ID: {user_id}" if user_id else "Unknown (Refresh required)")
            mention_link = f"<a href=\"tg://user?id={user_id}\">{link_text_content}</a>" if user_id else link_text_content
            name_display = mention_link
            if unique_name:
                name_display += f" ({unique_name})"

            entry_text = (
                f"{name_display}\n"
                f"<b>User:</b> {username_str}\n"
                f"<b>Phone:</b> {phone_str}\n"
                f"{escape_html(device_model)}"
                f"  ({escape_html(online_interval)} min)\n"
                f"<b>ID:</b> {user_id if user_id else 'N/A'}"
            )
            text_parts.append(entry_text)

    final_text = base_text + f"\n{'-'*25}\n".join(text_parts)

    keyboard = [[InlineKeyboardButton("« Back to Settings", callback_data="main_settings")]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.edit_message_text(
        text=final_text,
        parse_mode=ParseMode.HTML,
        reply_markup=reply_markup,
        disable_web_page_preview=True 
    )

@owner_only
async def execute_remove_account(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    if accounts_collection is None:
        await query.edit_message_text("⚠️ Database connection is not available. Please check logs.")
        return
        
    # --- FIX: Delete by _id ---
    _id_str_to_delete = query.data.split("_")[2]
    
    try:
        _id_to_delete = ObjectId(_id_str_to_delete)
    except Exception:
        await query.edit_message_text("Error: Invalid account ID format.")
        return

    account = accounts_collection.find_one({"_id": _id_to_delete})
    if not account:
        await query.edit_message_text(f"Error: Account not found in database (it may have been already removed).")
        # Go back to settings menu
        await asyncio.sleep(3)
        await settings_command(update, context)
        return
        
    user_id_to_delete = account.get('user_id')
    # --- END FIX ---
    
    # --- NEW: Stop online job ---
    if user_id_to_delete: # Only stop if we have a valid user_id
        stop_online_job(user_id_to_delete)
    # --- End ---
    
    if user_id_to_delete and user_id_to_delete in active_userbots:
        logger.info(f"Stopping userbot client for user ID {user_id_to_delete}")
        await active_userbots[user_id_to_delete].stop()
        del active_userbots[user_id_to_delete]
        
    result = accounts_collection.delete_one({"_id": _id_to_delete}) # <-- FIX: Delete by _id
    
    if result.deleted_count > 0:
        await query.edit_message_text(f"✅ Account <code>{user_id_to_delete or 'N/A'}</code> has been successfully removed.", parse_mode=ParseMode.HTML)
    else:
        await query.edit_message_text(f"⚠️ Account <code>{user_id_to_delete or 'N/A'}</code> was not found in the database (it may have been already removed).", parse_mode=ParseMode.HTML)
        
    await asyncio.sleep(3)
    await settings_command(update, context) 

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
    unique_name = update.message.text.strip().split()[0].lower()
    
    if accounts_collection is None:
        await update.message.reply_text("⚠️ Database connection is not available. Please /cancel and try again.")
        return ConversationHandler.END

    if accounts_collection.find_one({"unique_name": unique_name}):
        await update.message.reply_text("That name is already taken. Please choose another one.")
        return UNIQUE_NAME_PASTE 
        
    context.user_data['unique_name'] = unique_name
    await update.message.reply_text("Great. Now please paste the session string.")
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
    fallbacks=[CommandHandler("cancel", cancel_command_conv)],
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
        page_accounts = list(accounts_collection.find(
            {"user_id": {"$in": page_account_ids}},
            {"first_name": 1, "user_id": 1, "unique_name": 1, "online_interval": 1}
        ))
    
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

    all_accounts = list(accounts_collection.find({}, {"user_id": 1}))
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

    accounts_collection.update_many(
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
    accounts_collection.update_many(
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
    
    if update.callback_query:
        await update.callback_query.answer()
        try:
            await update.callback_query.edit_message_text("Action cancelled.")
        except Exception:
            await update.callback_query.message.reply_text("Action cancelled.")
    else:
        await update.message.reply_text("Action cancelled.")
            
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
        CommandHandler("online_interval", online_interval_start) # Restart command as fallback
    ],
    conversation_timeout=600,
)

# --- NEW: DATABASE CLEANUP COMMAND ---

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
        stop_tasks = [client.stop() for client in active_userbots.values() if client.is_connected]
        await asyncio.gather(*stop_tasks, return_exceptions=True)
        active_userbots.clear()
    
    await msg.edit_text("Bots stopped. 🤖 Now searching for duplicates...")
    
    total_deleted = 0
    
    try:
        # --- 3. Fix user_id duplicates ---
        pipeline_uid = [
            {
                '$group': {
                    '_id': '$user_id', 
                    'count': {'$sum': 1}, 
                    'ids': {'$push': '$_id'}
                }
            }, 
            {
                '$match': {
                    'count': {'$gt': 1}
                }
            }
        ]
        duplicates_uid = list(accounts_collection.aggregate(pipeline_uid))
        
        uid_deleted_count = 0
        if duplicates_uid:
            await msg.edit_text(f"Found {len(duplicates_uid)} user_id duplicate groups. Removing extras...")
            for group in duplicates_uid:
                # Keep the first ID in the list, delete the rest
                ids_to_delete = group['ids'][1:]
                result = accounts_collection.delete_many({"_id": {"$in": ids_to_delete}})
                uid_deleted_count += result.deleted_count
            total_deleted += uid_deleted_count
            
        # --- 4. Fix unique_name duplicates ---
        # Find documents where unique_name is not null
        pipeline_name = [
            {
                '$match': {
                    'unique_name': {'$ne': None}
                }
            },
            {
                '$group': {
                    '_id': '$unique_name', 
                    'count': {'$sum': 1}, 
                    'ids': {'$push': '$_id'}
                }
            }, 
            {
                '$match': {
                    'count': {'$gt': 1}
                }
            }
        ]
        duplicates_name = list(accounts_collection.aggregate(pipeline_name))
        
        name_deleted_count = 0
        if duplicates_name:
            await msg.edit_text(f"Removed {uid_deleted_count} user_id duplicates.\nFound {len(duplicates_name)} unique_name duplicate groups. Removing extras...")
            for group in duplicates_name:
                # Keep the first ID, delete the rest
                ids_to_delete = group['ids'][1:]
                result = accounts_collection.delete_many({"_id": {"$in": ids_to_delete}})
                name_deleted_count += result.deleted_count
            total_deleted += name_deleted_count

        # --- 5. Report ---
        final_message = (
            f"✅ **Deduplication Complete**\n"
            f"Removed {uid_deleted_count} duplicates by user_id.\n"
            f"Removed {name_deleted_count} duplicates by unique_name.\n"
            f"**Total documents deleted: {total_deleted}**"
        )
        await msg.edit_text(final_message, parse_mode=ParseMode.HTML)
        
        await update.message.reply_html(
            "Database is now clean.\n\n"
            "Please run /restart now to reload the bot and apply the new database indexes."
        )

    except Exception as e:
        logger.error(f"Error during deduplication: {e}")
        await msg.edit_text(f"An error occurred: {e}")
