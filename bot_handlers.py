import asyncio
from datetime import datetime
from functools import partial
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, User, MessageEntity
from telegram.ext import (
    ContextTypes,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    filters,
)
from telegram.constants import ParseMode

# Import from our own modules
from config import (
    OWNER_ID, accounts_collection, active_userbots, 
    paused_forwarding, paused_notifications, logger
)
from utils import owner_only, escape_html, clean_session_string
from userbot_logic import start_userbot, start_all_userbots_from_db
from jobs import resume_forwarding_job, resume_all_job

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
        "Please choose a method to add a new userbot account for security monitoring."
    )
    if update.callback_query:
        await update.callback_query.edit_message_text(message_text, parse_mode=ParseMode.HTML, reply_markup=reply_markup)
    else:
        await update.message.reply_html(message_text, reply_markup=reply_markup)

@owner_only
async def remove_account_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    accounts = list(accounts_collection.find())
    if not accounts:
        await update.message.reply_html("There are no accounts to remove.")
        return
    keyboard = []
    for acc in accounts:
        user_id = acc.get('user_id')
        name = escape_html(acc.get('first_name', f"ID: {user_id}"))
        button = [InlineKeyboardButton(f"🗑️ {name}", callback_data=f"delete_account_{user_id}")]
        keyboard.append(button)
    keyboard.append([InlineKeyboardButton("« Back to Settings", callback_data="main_settings")])
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_html("Please select an account to remove:", reply_markup=reply_markup)

@owner_only
async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    running_bots, total_bots = len(active_userbots), accounts_collection.count_documents({})
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
    try:
        user_id_to_pause = int(context.args[0])
        if user_id_to_pause not in active_userbots:
            await update.message.reply_text("User ID not found or bot is not active.")
            return

        pause_id = f"{user_id_to_pause}_{int(datetime.now().timestamp())}"
        context.bot_data[pause_id] = False # False means notifications are NOT paused by default

        paused_forwarding.add(user_id_to_pause)
        
        keyboard = [[InlineKeyboardButton("Pause Notifications", callback_data=f"pause_notify_{pause_id}")]]
        message = await update.message.reply_text(f"✅ Paused OTP processing for user ID {user_id_to_pause} for 5 minutes.",
                                                  reply_markup=InlineKeyboardMarkup(keyboard))
        
        context.application.job_queue.run_once(
            callback=resume_forwarding_job,
            when=300, # 5 minutes
            data={'user_id': user_id_to_pause, 'pause_id': pause_id, 'message_id': message.message_id},
            name=f"resume_{pause_id}"
        )

    except (IndexError, ValueError):
        await update.message.reply_text("Usage: /temp <user_id>")

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
    _, _, errors = await start_all_userbots_from_db(context.application, update_info=True)
    
    running_bots, total_bots = len(active_userbots), accounts_collection.count_documents({})
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

@owner_only
async def accounts_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    accounts = list(accounts_collection.find())
    
    base_text = "👤 Your Managed Accounts:\n\n"
    entities = [MessageEntity(type='bold', offset=0, length=28)]
    
    text_parts = []
    current_text_len = len(base_text)

    if not accounts:
        no_acc_text = "No accounts have been added yet.\n\n"
        info_text = "ℹ️ Run /refresh if accounts are missing details."
        text_parts.append(no_acc_text)
        text_parts.append(info_text)
        entities.append(MessageEntity(
            type='italic',
            offset=current_text_len + len(no_acc_text),
            length=len(info_text)
        ))
    else:
        for acc in accounts:
            first_name = acc.get('first_name', 'N/A')
            username_str = f"@{acc.get('username')}" if acc.get('username') else 'N/A'
            phone_str = acc.get('phone_number', 'N/A')
            user_id = acc.get('user_id')

            name_line_prefix = "Name: "
            name_line = f"{name_line_prefix}{first_name}\n"
            text_parts.append(name_line)
            
            if user_id and isinstance(user_id, int):
                user_for_mention = User(id=int(user_id), first_name=first_name if first_name else "User", is_bot=False)
                entities.append(MessageEntity(
                    type='text_mention',
                    offset=current_text_len + len(name_line_prefix),
                    length=len(first_name),
                    user=user_for_mention
                ))
            current_text_len += len(name_line)

            username_line = f"Username: {username_str}\n"
            text_parts.append(username_line)
            current_text_len += len(username_line)

            phone_line_prefix = "Phone: "
            phone_line = f"{phone_line_prefix}{phone_str}\n"
            text_parts.append(phone_line)
            entities.append(MessageEntity(
                type='code',
                offset=current_text_len + len(phone_line_prefix),
                length=len(phone_str)
            ))
            current_text_len += len(phone_line)

            id_line_prefix = "ID: "
            id_str = str(user_id) if user_id else 'N/A'
            id_line = f"{id_line_prefix}{id_str}\n"
            text_parts.append(id_line)
            entities.append(MessageEntity(
                type='code',
                offset=current_text_len + len(id_line_prefix),
                length=len(id_str)
            ))
            current_text_len += len(id_line)

            separator = f"{'-'*25}\n"
            text_parts.append(separator)
            current_text_len += len(separator)

    final_text = base_text + "".join(text_parts)
    keyboard = [[InlineKeyboardButton("« Back to Settings", callback_data="main_settings")]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.edit_message_text(
        text=final_text,
        entities=entities,
        reply_markup=reply_markup
    )

@owner_only
async def execute_remove_account(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
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

    del context.user_data['next_step']
    
    if step == 'awaiting_single_account':
        session_string = clean_session_string(update.message.text)
        msg = await update.message.reply_text("⏳ Processing...")
        status, user_info, detail = await start_userbot(session_string, context.application, update_info=True)
        if status == "success":
            await msg.edit_text(f"✅ Account added: {escape_html(user_info.first_name)}", parse_mode=ParseMode.HTML)
        else:
            await msg.edit_text(f"⚠️ Error adding account: {detail}")
        await asyncio.sleep(3); await settings_command(update, context)
        
    elif step == 'awaiting_multiple_accounts':
        text = update.message.text
        session_strings = [clean_session_string(s) for s in text.replace(",", " ").replace("\n", " ").split() if s.strip()]
        msg = await update.message.reply_text(f"Processing {len(session_strings)} strings...")
        success, fail = 0, 0
        for session in session_strings:
            status, _, _ = await start_userbot(session, context.application, update_info=True)
            if status == "success": success += 1
            else: fail += 1
        await msg.edit_text(f"Batch complete! ✅ Added: {success}, ❌ Failed: {fail}")
        await asyncio.sleep(3); await settings_command(update, context)
