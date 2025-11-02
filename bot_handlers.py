import asyncio
from datetime import datetime
from functools import partial
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
    UNIQUE_NAME_PASTE, AWAIT_STRING_PASTE
)
from utils import owner_only, escape_html, clean_session_string, get_account_from_arg
from userbot_logic import start_userbot, start_all_userbots_from_db
from jobs import resume_forwarding_job, resume_all_job
from session_generator import cancel_command_conv # Re-use cancel logic

# --- Command Handlers ---

@owner_only
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_html(
        "👋 Welcome! I am your userbot security manager.\n\n"
        "Use /settings to configure, /add to add accounts, and /remove to delete them."
    )

@owner_only
async def settings_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    keyboard = [
        [InlineKeyboardButton("👤 Manage Accounts", callback_data="manage_accounts")],
        [InlineKeyboardButton("➕ Add New Account", callback_data="call_add_command")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    bot_username = context.application.bot.username
    
    message_text = (
        "⚙️  <b>Settings Dashboard</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "Here you can manage your userbot accounts.\n\n"
        "▶️  <b>OTP Source:</b> <code>777000</code> (Telegram)\n"
        "      <i>Messages from this chat will be processed.</i>\n\n"
        f"🎯  <b>OTP Target:</b> <code>@{bot_username}</code> (Bot PM)\n"
        "      <i>Messages will be copied here.</i>"
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
        
        # --- Check DB connection ---
        if not accounts_collection:
            await update.message.reply_text("⚠️ Database connection is not available. Please check logs.")
            return
            
        account = await get_account_from_arg(identifier)
        
        if not account:
            await update.message.reply_text(f"⚠️ Account '<code>{escape_html(identifier)}</code>' not found.", parse_mode=ParseMode.HTML)
            return

        user_id_to_delete = account['user_id']
        
        # Stop client if running
        if user_id_to_delete in active_userbots:
            logger.info(f"Stopping userbot client for user ID {user_id_to_delete}")
            await active_userbots[user_id_to_delete].stop()
            del active_userbots[user_id_to_delete]
            
        # Delete from DB
        result = accounts_collection.delete_one({"user_id": user_id_to_delete})
        
        if result.deleted_count > 0:
            await update.message.reply_text(f"✅ Account <code>{user_id_to_delete}</code> (<code>{escape_html(account.get('unique_name', 'N/A'))}</code>) has been successfully removed.", parse_mode=ParseMode.HTML)
        else:
            await update.message.reply_text(f"⚠️ Could not find account <code>{user_id_to_delete}</code> in the database.", parse_mode=ParseMode.HTML)
        
    else:
        # No args, show menu
        if not accounts_collection:
            await update.message.reply_text("⚠️ Database connection is not available. Please check logs.")
            return
            
        accounts = list(accounts_collection.find())
        if not accounts:
            await update.message.reply_html("There are no accounts to remove.")
            return
        keyboard = []
        for acc in accounts:
            user_id = acc.get('user_id')
            name = escape_html(acc.get('first_name', f"ID: {user_id}"))
            unique_name = acc.get('unique_name')
            display_name = f"🗑️ {name}"
            if unique_name:
                display_name += f" ({escape_html(unique_name)})"
            
            button = [InlineKeyboardButton(display_name, callback_data=f"delete_account_{user_id}")]
            keyboard.append(button)
        keyboard.append([InlineKeyboardButton("« Back to Settings", callback_data="main_settings")])
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_html("Please select an account to remove:", reply_markup=reply_markup)

# --- MODIFIED: Fixed silent crash bug ---
@owner_only
async def set_unique_name_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles /xadd <identifier> <new_name>"""
    
    # --- NEW: Check for DB connection first ---
    if not accounts_collection:
        await update.message.reply_text("⚠️ Database connection is not available. Please check logs.")
        return

    if len(context.args) != 2:
        await update.message.reply_text("Usage: /xadd <user_id_or_name> <new_unique_name>")
        return
        
    identifier, new_name = context.args
    
    # Find the account to update *first*
    account = await get_account_from_arg(identifier)
    if not account:
        await update.message.reply_text(f"⚠️ Account '<code>{escape_html(identifier)}</code>' not found.", parse_mode=ParseMode.HTML)
        return

    # Check if new_name is already taken (by a *different* account)
    existing_with_name = accounts_collection.find_one({"unique_name": new_name})
    if existing_with_name and existing_with_name["user_id"] != account["user_id"]:
        await update.message.reply_text(f"⚠️ The name <code>{escape_html(new_name)}</code> is already taken by account <code>{existing_with_name.get('user_id')}</code>.", parse_mode=ParseMode.HTML)
        return
        
    # Update the account
    accounts_collection.update_one(
        {"user_id": account["user_id"]},
        {"$set": {"unique_name": new_name}}
    )
    
    await update.message.reply_text(
        f"✅ Unique name for <b>{escape_html(account.get('first_name'))}</b> "
        f"(<code>{account['user_id']}</code>) has been set to <code>{escape_html(new_name)}</code>.",
        parse_mode=ParseMode.HTML
    )

@owner_only
async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    total_bots = 0
    if accounts_collection:
        total_bots = accounts_collection.count_documents({})
        
    running_bots = len(active_userbots)
    bot_username = context.application.bot.username
    
    status_text = (f"📊 <b>Bot Status</b>\n"
                   f"━━━━━━━━━━━━━━━━━━━━\n\n"
                   f"<b>Management Bot:</b> Online\n"
                   f"<b>OTP Source:</b> <code>777000</code>\n"
                   f"<b>OTP Target:</b> <code>@{bot_username}</code> (Bot PM)\n\n"
                   f"<b>Userbots Running:</b> {running_bots}/{total_bots}\n"
                   f"<b>Paused OTP Processing:</b> {len(paused_forwarding)} bots\n"
                   f"<b>Paused Notifications:</b> {'Yes' if OWNER_ID in paused_notifications else 'No'}\n")
    await update.message.reply_html(status_text)

@owner_only
async def temp_pause_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Pauses a single userbot's OTP processing using the job queue."""
    if not context.args:
        await update.message.reply_text("Usage: /temp <user_id_or_name>")
        return
        
    # --- Check DB connection ---
    if not accounts_collection:
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
        context.bot_data[pause_id] = False # False means notifications are NOT paused by default

        paused_forwarding.add(user_id_to_pause)
        
        keyboard = [[InlineKeyboardButton("Pause Notifications", callback_data=f"pause_notify_{pause_id}")]]
        message = await update.message.reply_text(f"✅ Paused OTP processing for <code>{escape_html(account.get('first_name'))}</code> (<code>{user_id_to_pause}</code>) for 5 minutes.",
                                                  reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML)
        
        context.application.job_queue.run_once(
            callback=resume_forwarding_job,
            when=300, # 5 minutes
            data={'user_id': user_id_to_pause, 'pause_id': pause_id, 'message_id': message.message_id},
            name=f"resume_{pause_id}"
        )

    except (IndexError, ValueError):
        await update.message.reply_text("Usage: /temp <user_id_or_name>")

@owner_only
async def temp_pause_all(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Pauses all userbots' OTP processing and notifications using the job queue."""
    for user_id in active_userbots.keys():
        paused_forwarding.add(user_id)
    paused_notifications.add(OWNER_ID)
    await update.message.reply_text("✅ Paused all OTP processing and notifications for 5 minutes.")
    
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
    msg = await update.message.reply_text("🔄 Stopping all userbots...")
    for uid, client in list(active_userbots.items()):
        if client.is_connected:
            await client.stop()
    active_userbots.clear()
    
    await asyncio.sleep(2)

    await msg.edit_text("🔄 Restarting and refreshing userbot details...")
    
    total_bots = 0
    if accounts_collection:
        total_bots = accounts_collection.count_documents({})
    else:
        await msg.edit_text("⚠️ Database connection is not available. Cannot refresh.")
        return
        
    _, _, errors = await start_all_userbots_from_db(context.application, update_info=True)
    
    running_bots = len(active_userbots)
    final_message = f"✅ <b>Refresh Complete</b>\nStarted {running_bots}/{total_bots} userbots."

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

# --- MODIFIED: Rewritten for clarity, reliability, and single-message format ---
@owner_only
async def accounts_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    if not accounts_collection:
        await query.edit_message_text("⚠️ Database connection is not available. Please check logs.")
        return
        
    accounts = list(accounts_collection.find())
    
    base_text = "👤 <b>Your Managed Accounts:</b>\n\n"
    text_parts = []

    if not accounts:
        base_text += "No accounts have been added yet.\n\n"
        base_text += "ℹ️ <i>Run /refresh if accounts are missing details.</i>"
    else:
        for acc in accounts:
            first_name = escape_html(acc.get('first_name', 'N/A'))
            username_str = f"@{escape_html(acc.get('username'))}" if acc.get('username') else 'N/A'
            phone_str = escape_html(acc.get('phone_number', 'N/A'))
            user_id = acc.get('user_id')
            unique_name = escape_html(acc.get('unique_name'))

            # 1. Build Name Display
            name_display = ""
            if user_id:
                # Use tg://user?id= link which is the most reliable mention
                name_display = f"<a href=\"tg://user?id={user_id}\">{first_name}</a>"
            else:
                name_display = first_name
            
            if unique_name:
                name_display += f" (<code>{unique_name}</code>)"

            # 2. Build Text Entry for this account
            entry_text = (
                f"<b>Name:</b> {name_display}\n"
                f"<b>Username:</b> {username_str}\n"
                f"<b>Phone:</b> <code>{phone_str}</code>\n"
                f"<b>ID:</b> <code>{user_id if user_id else 'N/A'}</code>"
            )
            text_parts.append(entry_text)

    # 3. Join all parts
    final_text = base_text + f"\n{'-'*25}\n".join(text_parts)

    keyboard = [[InlineKeyboardButton("« Back to Settings", callback_data="main_settings")]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    # 4. Edit the message
    await query.edit_message_text(
        text=final_text,
        parse_mode=ParseMode.HTML,
        reply_markup=reply_markup,
        disable_web_page_preview=True # Prevents issues with tg:// links
    )

@owner_only
async def execute_remove_account(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    if not accounts_collection:
        await query.edit_message_text("⚠️ Database connection is not available. Please check logs.")
        return
        
    user_id_to_delete = int(query.data.split("_")[2])
    
    if user_id_to_delete in active_userbots:
        logger.info(f"Stopping userbot client for user ID {user_id_to_delete}")
        await active_userbots[user_id_to_delete].stop()
        del active_userbots[user_id_to_delete]
        
    result = accounts_collection.delete_one({"user_id": user_id_to_delete})
    
    if result.deleted_count > 0:
        await query.edit_message_text(f"✅ Account <code>{user_id_to_delete}</code> has been successfully removed.", parse_mode=ParseMode.HTML)
    else:
        await query.edit_message_text(f"⚠️ Could not find account <code>{user_id_to_delete}</code> in the database.", parse_mode=ParseMode.HTML)
        
    await asyncio.sleep(3)
    await settings_command(update, context) # Show settings menu again

@owner_only
async def set_next_step(update: Update, context: ContextTypes.DEFAULT_TYPE, step: str, text: str):
    query = update.callback_query
    await query.answer()
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
    context.bot_data[pause_id] = True # True means notifications ARE now paused
    
    await query.edit_message_text(
        f"{query.message.text}\n\n<i>✅ Notifications also paused for the remainder of the 5-minute window.</i>",
        parse_mode=ParseMode.HTML,
        reply_markup=None
    )

# --- Message Handler ---

async def handle_text_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID: return
    step = context.user_data.get('next_step')
    if not step: return

    # This check is to avoid collision with conversation handler
    if 'awaiting' in step and step.endswith(('phone_number', 'login_code', '2fa_password')): return
    if step in ['awaiting_single_account', 'awaiting_multiple_accounts']:
        pass # Allow these to proceed
    else:
        # If it's not a recognized 'next_step', ignore it
        return 

    del context.user_data['next_step']
    
    if step == 'awaiting_multiple_accounts':
        text = update.message.text
        session_strings = [clean_session_string(s) for s in text.replace(",", " ").replace("\n", " ").split() if s.strip()]
        msg = await update.message.reply_text(f"Processing {len(session_strings)} strings...")
        
        if not accounts_collection:
            await msg.edit_text("⚠️ Database connection is not available. Cannot add accounts.")
            return

        success, fail = 0, 0
        for session in session_strings:
            # Note: This flow does not add a unique_name
            status, _, detail = await start_userbot(session, context.application, update_info=True)
            if status == "success": success += 1
            else: fail += 1
        await msg.edit_text(f"Batch complete! ✅ Added: {success}, ❌ Failed: {fail}")
        await asyncio.sleep(3); await settings_command(update, context)

# --- NEW Paste Single String Conversation Handler ---

@owner_only
async def prompt_for_unique_name_paste(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Entry point for pasting a single string. Asks for unique name."""
    query = update.callback_query
    await query.answer()
    await query.message.reply_text("Please send a unique name (e.g., 'main_acct') for this new account. Send /cancel to stop.")
    return UNIQUE_NAME_PASTE

@owner_only
async def get_unique_name_for_paste(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Saves unique name and asks for session string."""
    unique_name = update.message.text.strip().split()[0] # Take first word
    
    if not accounts_collection:
        await update.message.reply_text("⚠️ Database connection is not available. Please /cancel and try again.")
        return ConversationHandler.END

    if accounts_collection.find_one({"unique_name": unique_name}):
        await update.message.reply_text("That name is already taken. Please choose another one.")
        return UNIQUE_NAME_PASTE # Stay in this state
        
    context.user_data['unique_name'] = unique_name
    await update.message.reply_text("Great. Now please paste the session string.")
    return AWAIT_STRING_PASTE

@owner_only
async def get_session_string_and_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Gets session string, adds account, and ends conversation."""
    session_string = clean_session_string(update.message.text)
    unique_name = context.user_data.get('unique_name')
    
    msg = await update.message.reply_text("⏳ Processing session string...")
    
    if not accounts_collection:
        await msg.edit_text("⚠️ Database connection is not available. Cannot add account.")
        context.user_data.clear()
        return ConversationHandler.END
        
    status, user_info, detail = await start_userbot(
        session_string, context.application, update_info=True, unique_name=unique_name
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