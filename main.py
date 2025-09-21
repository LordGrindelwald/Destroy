# ██████╗ ██╗   ██╗███████╗██████╗  ██████╗ ████████╗
# ██╔══██╗██║   ██║██╔════╝██╔══██╗██╔═══██╗╚══██╔══╝
# ██████╔╝██║   ██║█████╗  ██████╔╝██║   ██║   ██║
# ██╔══██╗██║   ██║██╔══╝  ██╔══██╗██║   ██║   ██║
# ██████╔╝╚██████╔╝███████╗██║  ██║╚██████╔╝   ██║
# ╚═════╝  ╚═════╝ ╚══════╝╚═╝  ╚═╝ ╚═════╝    ╚═╝
#
#           Userbot Forwarder Management Bot
#          (Final Version with Refactored Conversation Flow)

import os
import asyncio
import logging
from datetime import datetime
from functools import partial
from pymongo import MongoClient
from dotenv import load_dotenv

from pyrogram import Client as PyrogramClient
from pyrogram.errors import AuthKeyUnregistered, UserDeactivated, AuthKeyDuplicated
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
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
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

# --- REFACTORED: ConversationHandler States ---
(
    # These states are now entered from the main menu buttons
    MANAGING_ACCOUNTS,
    SET_SOURCE,
    SET_TARGET,
    # Sub-states for adding accounts
    ADD_SINGLE_ACCOUNT,
    ADD_MULTIPLE_ACCOUNTS,
) = range(5)


# --- Userbot Core Logic (Unchanged) ---
async def get_source_chat():
    config = config_collection.find_one({"_id": "config"})
    return config.get("source_chat_id", 777000)

async def get_target_chat():
    config = config_collection.find_one({"_id": "config"})
    return config.get("target_chat_username")

async def forwarder_handler(client: PyrogramClient, message: Message, ptb_app: Application):
    if client.me.id in paused_notifications: return
    source_chat_id = await get_source_chat()
    if message.chat.id == source_chat_id:
        try:
            target_chat = await get_target_chat()
            is_forwarding_paused = client.me.id in paused_forwarding
            if not is_forwarding_paused:
                if target_chat:
                    await client.forward_messages(chat_id=target_chat, from_chat_id=message.chat.id, message_ids=message.id)
                    status_text = "✅ Forwarded"
                else:
                    status_text = "⚠️ Not Forwarded (No Target Set)"
            else:
                status_text = "⏸️ Paused (Forwarding Only)"
            content = message.text or message.caption or "(Media without caption)"
            header = f"👤 **{client.me.first_name}**"
            notification_text = (f"{header}\n**Status:** {status_text}\n\n**Content:**\n`{content[:3000]}`")
            await ptb_app.bot.send_message(OWNER_ID, notification_text, parse_mode=ParseMode.MARKDOWN_V2)
        except Exception as e:
            logger.error(f"Failed to process message {message.id} from {client.me.id}: {e}")
            await ptb_app.bot.send_message(OWNER_ID, f"Error processing message: `{e}`", parse_mode=ParseMode.MARKDOWN_V2)

async def start_userbot(session_string: str, ptb_app: Application, update_info: bool = False):
    try:
        userbot = PyrogramClient(name=f"userbot_{len(active_userbots)}", api_id=API_ID, api_hash=API_HASH, session_string=session_string, in_memory=True)
        await userbot.start()
        if userbot.me.id in active_userbots:
            await userbot.stop()
            return "already_exists", None
        handler_with_context = partial(forwarder_handler, ptb_app=ptb_app)
        userbot.add_handler(MessageHandler(handler_with_context))
        active_userbots[userbot.me.id] = {"client": userbot, "task": asyncio.current_task()}
        if update_info:
            account_info = {"user_id": userbot.me.id, "first_name": userbot.me.first_name, "username": userbot.me.username, "phone_number": userbot.me.phone_number, "session_string": session_string}
            accounts_collection.update_one({"user_id": userbot.me.id}, {"$set": account_info}, upsert=True)
        return "success", userbot.me
    except (AuthKeyUnregistered, UserDeactivated, AuthKeyDuplicated):
        return "invalid_session", None
    except Exception as e:
        logger.error(f"An unexpected error occurred: {e}")
        return "error", None

async def start_all_userbots_from_db(application: Application, update_info: bool = False):
    all_accounts = list(accounts_collection.find())
    count = 0
    for account in all_accounts:
        status, _ = await start_userbot(account["session_string"], application, update_info=update_info)
        if status == "success":
            count += 1
    logger.info(f"Started {count}/{len(all_accounts)} userbots.")
    return count, len(all_accounts)


# --- Management Bot Handlers ---
async def owner_only(update: Update, context: ContextTypes.DEFAULT_TYPE, command_handler):
    if update.effective_user.id != OWNER_ID:
        await update.message.reply_text("⛔️ You are not authorized to use this command.")
        return
    await command_handler(update, context)

# --- REFACTORED: `/start` is now a simple, standalone command ---
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Displays the main menu keyboard."""
    keyboard = [
        [InlineKeyboardButton("📚 Source Chat", callback_data="set_source")],
        [InlineKeyboardButton("🎯 Target Chat", callback_data="set_target")],
        [InlineKeyboardButton("👤 Accounts", callback_data="manage_accounts")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    source_chat_id = await get_source_chat()
    target_chat = await get_target_chat() or "Not Set"
    await update.message.reply_html(
        f"👋 Welcome, <b>{update.effective_user.first_name}</b>!\n\n"
        f"I am your userbot forwarder manager.\n\n"
        f"▶️ Source Chat: <code>{source_chat_id}</code>\n"
        f"🎯 Target Chat: <code>{target_chat}</code>\n\n"
        f"Please choose an option from the menu below.",
        reply_markup=reply_markup,
    )

async def accounts_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Displays account list and enters the account management state."""
    query = update.callback_query
    await query.answer()
    accounts = list(accounts_collection.find())
    text = "<b>Your currently managed accounts:</b>\n\n" if accounts else "No accounts have been added yet."
    for acc in accounts:
        text += (f"👤 <b>Name:</b> {acc.get('first_name', 'N/A')}\n   - <b>ID:</b> <code>{acc.get('user_id', 'N/A')}</code>\n--------------------\n")
    keyboard = [
        [InlineKeyboardButton("➕ Add Single", callback_data="add_single"), InlineKeyboardButton("➕ Add Multiple", callback_data="add_multiple")],
        [InlineKeyboardButton("« Back to Main Menu", callback_data="main_menu")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=reply_markup)
    return MANAGING_ACCOUNTS

async def ask_for_source(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("Please send the new universal source chat ID.")
    return SET_SOURCE

async def set_source(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        chat_id = int(update.message.text)
        config_collection.update_one({"_id": "config"}, {"$set": {"source_chat_id": chat_id}}, upsert=True)
        await update.message.reply_text(f"✅ Source chat updated to: {chat_id}")
    except ValueError:
        await update.message.reply_text("❌ Invalid ID.")
    await start_command(update.message, context)
    return ConversationHandler.END

async def ask_for_target(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("Please send the username of the target bot (e.g., @my_bot).")
    return SET_TARGET

async def set_target(update: Update, context: ContextTypes.DEFAULT_TYPE):
    username = update.message.text.strip()
    if username.startswith("@") and len(username) > 4:
        config_collection.update_one({"_id": "config"}, {"$set": {"target_chat_username": username}}, upsert=True)
        await update.message.reply_text(f"✅ Target chat updated to: {username}")
    else:
        await update.message.reply_text("❌ Invalid username.")
    await start_command(update.message, context)
    return ConversationHandler.END

async def ask_for_single_account(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("Please send the session string.")
    return ADD_SINGLE_ACCOUNT

async def add_single_account(update: Update, context: ContextTypes.DEFAULT_TYPE):
    session_string = update.message.text
    msg = await update.message.reply_text("⏳ Processing...")
    status, user_info = await start_userbot(session_string, context.application, update_info=True)
    if status == "success":
        await msg.edit_text(f"✅ Account added: {user_info.first_name}")
    elif status == "already_exists":
        await msg.edit_text("⚠️ Account already exists.")
    else:
        await msg.edit_text("❌ Invalid session string.")
    await start_command(update.message, context)
    return ConversationHandler.END

async def ask_for_multiple_accounts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("Please send all session strings, separated by a space or new line.")
    return ADD_MULTIPLE_ACCOUNTS

async def add_multiple_accounts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    session_strings = [s.strip() for s in text.replace(",", " ").replace("\n", " ").split() if s.strip()]
    msg = await update.message.reply_text(f"Processing {len(session_strings)} strings...")
    success, fail = 0, 0
    for session in session_strings:
        status, _ = await start_userbot(session, context.application, update_info=True)
        if status == "success": success += 1
        else: fail += 1
    await msg.edit_text(f"Batch complete! ✅ Added: {success}, ❌ Failed: {fail}")
    await start_command(update.message, context)
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Operation cancelled.")
    await start_command(update.message, context)
    return ConversationHandler.END

async def main_menu_from_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Ends conversation and shows the main menu."""
    query = update.callback_query
    await query.answer()
    await start_command(query.message, context)
    return ConversationHandler.END

# --- Independent Commands ---
async def ping_command(update: Update, context: ContextTypes.DEFAULT_TYPE): pass
async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE): pass
async def refresh_command(update: Update, context: ContextTypes.DEFAULT_TYPE): pass
async def temp_pause_command(update: Update, context: ContextTypes.DEFAULT_TYPE): pass
async def temp_fwd_command(update: Update, context: ContextTypes.DEFAULT_TYPE): pass
# (All independent commands from the full previous version should be pasted here)

# --- Health Check Server (Unchanged) ---
async def health_check_server():
    host, port = "0.0.0.0", int(os.getenv("PORT", 8080))
    server = await asyncio.start_server(lambda r, w: w.close(), host, port)
    logger.info(f"Health check server started on port {port}")
    async with server: await server.serve_forever()

# --- Main Application Runner with Leader Election (Unchanged) ---
async def main():
    application = Application.builder().token(BOT_TOKEN).build()
    
    # --- REFACTORED: ConversationHandler starts from button presses ---
    conv_handler = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(ask_for_source, pattern="^set_source$"),
            CallbackQueryHandler(ask_for_target, pattern="^set_target$"),
            CallbackQueryHandler(accounts_menu, pattern="^manage_accounts$"),
        ],
        states={
            SET_SOURCE: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_source)],
            SET_TARGET: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_target)],
            MANAGING_ACCOUNTS: [
                CallbackQueryHandler(ask_for_single_account, pattern="^add_single$"),
                CallbackQueryHandler(ask_for_multiple_accounts, pattern="^add_multiple$"),
                CallbackQueryHandler(main_menu_from_button, pattern="^main_menu$"),
            ],
            ADD_SINGLE_ACCOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_single_account)],
            ADD_MULTIPLE_ACCOUNTS: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_multiple_accounts)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        persistent=False,
        name="main_conversation"
    )

    # Add the standalone start command handler
    application.add_handler(CommandHandler("start", lambda u, c: owner_only(u, c, start_command)))
    
    # Add the conversation handler and other independent commands
    application.add_handler(conv_handler)
    # (Add handlers for ping, status, refresh, temp, etc. here)
    
    # The rest of the leader election logic is complex and best left as is.
    # It will correctly run the application and health check server.
    # ... (The full main() and `run_bot_as_leader` from the previous answer should be here) ...

if __name__ == "__main__":
    # Ensure the full functions are included before running
    pass
