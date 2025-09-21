# ██████╗ ██╗   ██╗███████╗██████╗  ██████╗ ████████╗
# ██╔══██╗██║   ██║██╔════╝██╔══██╗██╔═══██╗╚══██╔══╝
# ██████╔╝██║   ██║█████╗  ██████╔╝██║   ██║   ██║
# ██╔══██╗██║   ██║██╔══╝  ██╔══██╗██║   ██║   ██║
# ██████╔╝╚██████╔╝███████╗██║  ██║╚██████╔╝   ██║
# ╚═════╝  ╚═════╝ ╚══════╝╚═╝  ╚═╝ ╚═════╝    ╚═╝
#
#           Userbot Forwarder Management Bot
#          (Version 2.0 - Reworked with HTML)

import os
import asyncio
import logging
from datetime import datetime
from functools import partial
from pymongo import MongoClient
from dotenv import load_dotenv

from pyrogram import Client as PyrogramClient
from pyrogram.errors import (
    AuthKeyUnregistered, UserDeactivated, AuthKeyDuplicated,
    SessionPasswordNeeded, PhoneNumberInvalid, PhoneCodeInvalid, PhoneCodeExpired, PasswordHashInvalid
)
from pyrogram.handlers import MessageHandler as PyrogramMessageHandler
from pyrogram.types import Message

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
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

# NEW: Helper function to escape text for HTML
def escape_html(text: str) -> str:
    """Escapes special characters for Telegram HTML parsing."""
    if not isinstance(text, str): text = str(text)
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

# --- Userbot Core Logic ---
async def get_source_chat():
    config = config_collection.find_one({"_id": "config"})
    return config.get("source_chat_id", 777000) if config else 777000

async def get_target_chat():
    config = config_collection.find_one({"_id": "config"})
    return config.get("target_chat_username") if config else None

async def forwarder_handler(client: PyrogramClient, message: Message, ptb_app: Application):
    if client.me.id in paused_notifications: return
    source_chat_id = await get_source_chat()
    if message.chat.id == source_chat_id:
        try:
            target_chat = await get_target_chat()
            is_forwarding_paused = client.me.id in paused_forwarding
            if not is_forwarding_paused:
                if target_chat:
                    if os.urandom(1)[0] % 10 < 9: await message.forward(chat_id=target_chat)
                    else: await message.copy(chat_id=target_chat)
                    status_text = "✅ Forwarded"
                else: status_text = "⚠️ Not Forwarded (No Target Set)"
            else: status_text = "⏸️ Paused (Forwarding Only)"
            content = message.text or message.caption or "(Media without caption)"
            header = f"👤 <b>{escape_html(client.me.first_name)}</b>"
            notification_text = (f"{header}\n<b>Status:</b> {status_text}\n\n<b>Content:</b>\n<code>{escape_html(content[:3000])}</code>")
            await ptb_app.bot.send_message(OWNER_ID, notification_text, parse_mode=ParseMode.HTML)
        except Exception as e:
            logger.error(f"Failed to process message {message.id} from {client.me.id}: {e}")
            await ptb_app.bot.send_message(OWNER_ID, f"Error processing message: <code>{escape_html(str(e))}</code>", parse_mode=ParseMode.HTML)

async def start_userbot(session_string: str, ptb_app: Application, update_info: bool = False):
    try:
        userbot = PyrogramClient(name=f"userbot_{len(active_userbots)}", api_id=API_ID, api_hash=API_HASH, session_string=session_string, in_memory=True)
        await userbot.start()
        if userbot.me.id in active_userbots:
            await userbot.stop()
            return "already_exists", None
        handler_with_context = partial(forwarder_handler, ptb_app=ptb_app)
        userbot.add_handler(PyrogramMessageHandler(handler_with_context))
        active_userbots[userbot.me.id] = {"client": userbot, "task": asyncio.current_task()}
        if update_info:
            account_info = {"user_id": userbot.me.id, "first_name": userbot.me.first_name, "username": userbot.me.username, "phone_number": userbot.me.phone_number, "session_string": session_string}
            accounts_collection.update_one({"user_id": userbot.me.id}, {"$set": account_info}, upsert=True)
        return "success", userbot.me
    except (AuthKeyUnregistered, UserDeactivated, AuthKeyDuplicated): return "invalid_session", None
    except Exception as e:
        logger.error(f"An unexpected error occurred: {e}")
        return "error", None

async def start_all_userbots_from_db(application: Application, update_info: bool = False):
    all_accounts = list(accounts_collection.find())
    count = 0
    for account in all_accounts:
        status, _ = await start_userbot(account["session_string"], application, update_info=update_info)
        if status == "success": count += 1
    logger.info(f"Started {count}/{len(all_accounts)} userbots.")
    return count, len(all_accounts)

# --- Management Bot Handlers ---
async def owner_only(update: Update, context: ContextTypes.DEFAULT_TYPE, command_handler):
    if update.effective_user.id != OWNER_ID:
        if update.message: await update.message.reply_text("⛔️ You are not authorized.")
        elif update.callback_query: await update.callback_query.answer("⛔️ You are not authorized.", show_alert=True)
        return
    await command_handler(update, context)

# NEW: `/start` is now just a simple greeting
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_html(
        "👋 Welcome! I am your userbot forwarder manager.\n\n"
        "Use /settings to see the main configuration menu."
    )

# NEW: `/settings` is the new main menu
async def settings_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    keyboard = [
        [InlineKeyboardButton("📚 Set Source", callback_data="set_source")],
        [InlineKeyboardButton("🎯 Set Target", callback_data="set_target")],
        [InlineKeyboardButton("👤 Manage Accounts", callback_data="manage_accounts")],
        [InlineKeyboardButton("⚙️ Generate Session", callback_data="generate_session")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    source_chat_id = await get_source_chat()
    target_chat = await get_target_chat() or "Not Set"
    message_text = (
        "⚙️ <b>Settings Dashboard</b>\n\n"
        f"▶️ <b>Source:</b> <code>{source_chat_id}</code>\n"
        f"🎯 <b>Target:</b> <code>{escape_html(target_chat)}</code>"
    )
    if update.callback_query:
        await update.callback_query.edit_message_text(message_text, parse_mode=ParseMode.HTML, reply_markup=reply_markup)
    else:
        await update.message.reply_html(message_text, reply_markup=reply_markup)

# NEW: `/add` is the new menu for adding accounts
async def add_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    keyboard = [
        [InlineKeyboardButton("Paste Single String", callback_data="add_single")],
        [InlineKeyboardButton("Paste Multiple Strings", callback_data="add_multiple")],
        [InlineKeyboardButton("Via Phone Number", callback_data="generate_session")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_html("<b>How would you like to add an account?</b>", reply_markup=reply_markup)

async def accounts_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    accounts = list(accounts_collection.find())
    text = "👤 <b>Your Managed Accounts:</b>\n\n" if accounts else "No accounts have been added yet."
    for acc in accounts:
        first_name = escape_html(acc.get('first_name', 'N/A'))
        text += f"<b>Name:</b> {first_name}\n<b>ID:</b> <code>{acc.get('user_id', 'N/A')}</code>\n{'-'*25}\n"
    keyboard = [[InlineKeyboardButton("« Back to Settings", callback_data="main_menu")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=reply_markup)

async def set_next_step(update: Update, context: ContextTypes.DEFAULT_TYPE, step: str, text: str):
    query = update.callback_query
    await query.answer()
    context.user_data['next_step'] = step
    await query.edit_message_text(text)

async def handle_text_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    step = context.user_data.get('next_step')
    if not step: return

    # Session generation logic...
    if step == 'awaiting_phone_number':
        phone = update.message.text
        context.user_data['phone'] = phone
        client = PyrogramClient(name=f"generator_{update.effective_user.id}", api_id=API_ID, api_hash=API_HASH, in_memory=True)
        context.user_data['temp_client'] = client
        try:
            await client.connect()
            sent_code = await client.send_code(phone)
            context.user_data['phone_code_hash'] = sent_code.phone_code_hash
            context.user_data['next_step'] = 'awaiting_login_code'
            await update.message.reply_text("A login code has been sent. Please send it here.")
        except PhoneNumberInvalid:
            await update.message.reply_text("❌ <b>Error:</b> The phone number is invalid. Process cancelled.", parse_mode=ParseMode.HTML)
            context.user_data.clear()
        except Exception as e:
            await update.message.reply_html(f"❌ <b>An unexpected error occurred:</b> {e}. Process cancelled.")
            context.user_data.clear()
        return

    if step == 'awaiting_login_code':
        # ... (rest of function is long and unchanged, so it's omitted for this view)
        pass # Placeholder

async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if 'temp_client' in context.user_data:
        client = context.user_data['temp_client']
        if client.is_connected: await client.disconnect()
    context.user_data.clear()
    await update.message.reply_text("Action cancelled.")
    await settings_command(update, context)

# --- Independent Commands ---
async def ping_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    start_time = datetime.now()
    message = await update.message.reply_text("Pinging...")
    end_time = datetime.now()
    latency = (end_time - start_time).microseconds / 1000
    await message.edit_text(f"🏓 Pong!\nLatency: {latency:.2f} ms")

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # ... (status command is long and unchanged, so it's omitted for this view)
    pass # Placeholder

# --- Health Check Server & Main Runner ---
# ... (These are unchanged, but they are crucial for deployment)

def main():
    application = Application.builder().token(BOT_TOKEN).build()

    # Add new command handlers
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("settings", lambda u, c: owner_only(u, c, settings_command)))
    application.add_handler(CommandHandler("add", lambda u, c: owner_only(u, c, add_command)))
    application.add_handler(CommandHandler("cancel", lambda u, c: owner_only(u, c, cancel_command)))
    application.add_handler(CommandHandler("ping", ping_command))
    # ... (add handlers for status, refresh, temp, temp_fwd)

    # Callback handlers for buttons
    application.add_handler(CallbackQueryHandler(lambda u,c: set_next_step(u, c, 'awaiting_source', "Please send the source chat ID."), pattern="^set_source$"))
    # ... (add all other callback handlers)

    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_input))
    
    # ... (Full leader election logic from the previous answer must be here)

if __name__ == "__main__":
    pass # Full main function logic from previous answer must be here
