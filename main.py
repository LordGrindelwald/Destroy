# ██████╗ ██╗   ██╗███████╗██████╗  ██████╗ ████████╗
# ██╔══██╗██║   ██║██╔════╝██╔══██╗██╔═══██╗╚══██╔══╝
# ██████╔╝██║   ██║█████╗  ██████╔╝██║   ██║   ██║
# ██╔══██╗██║   ██║██╔══╝  ██╔══██╗██║   ██║   ██║
# ██████╔╝╚██████╔╝███████╗██║  ██║╚██████╔╝   ██║
# ╚═════╝  ╚═════╝ ╚══════╝╚═╝  ╚═╝ ╚═════╝    ╚═╝
#
#           Userbot Forwarder Management Bot
#          (Final Version with Session Generator)

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

def escape_markdown_v2(text: str) -> str:
    """Helper function to escape text for Telegram MarkdownV2."""
    if not isinstance(text, str): text = str(text)
    escape_chars = r'_*[]()~`>#+-=|{}.!'
    return ''.join(f'\\{char}' if char in escape_chars else char for char in text)

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
                    await client.forward_messages(chat_id=target_chat, from_chat_id=message.chat.id, message_ids=message.id)
                    status_text = "✅ Forwarded"
                else: status_text = "⚠️ Not Forwarded (No Target Set)"
            else: status_text = "⏸️ Paused (Forwarding Only)"
            content = message.text or message.caption or "(Media without caption)"
            header = f"👤 **{escape_markdown_v2(client.me.first_name)}**"
            notification_text = (f"{header}\n**Status:** {status_text}\n\n**Content:**\n`{content[:3000]}`")
            await ptb_app.bot.send_message(OWNER_ID, notification_text, parse_mode=ParseMode.MARKDOWN_V2)
        except Exception as e:
            logger.error(f"Failed to process message {message.id} from {client.me.id}: {e}")
            await ptb_app.bot.send_message(OWNER_ID, f"Error processing message: `{escape_markdown_v2(str(e))}`", parse_mode=ParseMode.MARKDOWN_V2)

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
        await update.message.reply_text("⛔️ You are not authorized to use this command.")
        return
    await command_handler(update, context)

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    keyboard = [
        [InlineKeyboardButton("📚 Set Source", callback_data="set_source")],
        [InlineKeyboardButton("🎯 Set Target", callback_data="set_target")],
        [InlineKeyboardButton("👤 Manage Accounts", callback_data="manage_accounts")],
        [InlineKeyboardButton("ģ Generate Session", callback_data="generate_session")], # NEW
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    source_chat_id = await get_source_chat()
    target_chat = await get_target_chat() or "Not Set"
    message_text = (f"👋 **Welcome\!**\n\n▶️ **Source:** `{source_chat_id}`\n🎯 **Target:** `{escape_markdown_v2(target_chat)}`")
    if update.callback_query:
        await update.callback_query.edit_message_text(message_text, parse_mode=ParseMode.MARKDOWN_V2, reply_markup=reply_markup)
    else:
        await update.message.reply_markdown_v2(message_text, reply_markup=reply_markup)

async def accounts_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    accounts = list(accounts_collection.find())
    text = "👤 **Your Managed Accounts:**\n\n" if accounts else "No accounts have been added yet\\."
    for acc in accounts:
        first_name = escape_markdown_v2(acc.get('first_name', 'N/A'))
        separator = '-'*20
        text += f"**Name:** {first_name}\n**ID:** `{acc.get('user_id', 'N/A')}`\n{separator}\n"
    keyboard = [[InlineKeyboardButton("➕ Add Single", callback_data="add_single"), InlineKeyboardButton("➕ Add Multiple", callback_data="add_multiple")], [InlineKeyboardButton("« Back", callback_data="main_menu")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN_V2, reply_markup=reply_markup)

async def set_next_step(update: Update, context: ContextTypes.DEFAULT_TYPE, step: str, text: str):
    query = update.callback_query
    await query.answer()
    context.user_data['next_step'] = step
    await query.edit_message_text(text)

async def handle_text_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    step = context.user_data.get('next_step')
    if not step: return

    # --- Session Generation Logic ---
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
            await update.message.reply_text("A login code has been sent to your Telegram account. Please send it here.")
        except PhoneNumberInvalid:
            await update.message.reply_text("❌ **Error:** The phone number is invalid. Process cancelled.")
            context.user_data.clear()
        except Exception as e:
            await update.message.reply_text(f"❌ **An unexpected error occurred:** {e}. Process cancelled.")
            context.user_data.clear()

    elif step == 'awaiting_login_code':
        code = update.message.text
        client = context.user_data['temp_client']
        phone = context.user_data['phone']
        phone_code_hash = context.user_data['phone_code_hash']
        try:
            await client.sign_in(phone, phone_code_hash, code)
            session_string = await client.export_session_string()
            await client.disconnect()
            context.user_data['last_generated_session'] = session_string
            keyboard = [[InlineKeyboardButton("➕ Add this account now", callback_data="add_generated_session")]]
            await update.message.reply_text(f"✅ **Success!** Here is your session string:\n\n`{session_string}`", reply_markup=InlineKeyboardMarkup(keyboard))
            context.user_data.clear()
        except SessionPasswordNeeded:
            context.user_data['next_step'] = 'awaiting_2fa_password'
            await update.message.reply_text("Your account has Two-Factor Authentication enabled. Please send your password.")
        except (PhoneCodeInvalid, PhoneCodeExpired):
            await update.message.reply_text("❌ **Error:** The login code is invalid or has expired. Process cancelled.")
            await client.disconnect()
            context.user_data.clear()
        finally:
             await update.message.delete() # Delete the message with the code for security

    elif step == 'awaiting_2fa_password':
        password = update.message.text
        client = context.user_data['temp_client']
        try:
            await client.check_password(password)
            session_string = await client.export_session_string()
            await client.disconnect()
            context.user_data['last_generated_session'] = session_string
            keyboard = [[InlineKeyboardButton("➕ Add this account now", callback_data="add_generated_session")]]
            await update.message.reply_text(f"✅ **Success!** Here is your session string:\n\n`{session_string}`", reply_markup=InlineKeyboardMarkup(keyboard))
            context.user_data.clear()
        except PasswordHashInvalid:
            await update.message.reply_text("❌ **Error:** The password is incorrect. Process cancelled.")
            await client.disconnect()
            context.user_data.clear()
        finally:
            await update.message.delete() # Delete the message with the password

    # --- Other Input Logic ---
    elif step == 'awaiting_source':
        try:
            chat_id = int(update.message.text)
            config_collection.update_one({"_id": "config"}, {"$set": {"source_chat_id": chat_id}}, upsert=True)
            await update.message.reply_text(f"✅ Source chat updated to: {chat_id}")
        except ValueError: await update.message.reply_text("❌ Invalid ID.")
        await start_command(update, context)

    elif step == 'awaiting_target':
        username = update.message.text.strip()
        if username.startswith("@") and len(username) > 4:
            config_collection.update_one({"_id": "config"}, {"$set": {"target_chat_username": username}}, upsert=True)
            await update.message.reply_text(f"✅ Target chat updated to: {username}")
        else: await update.message.reply_text("❌ Invalid username.")
        await start_command(update, context)

    elif step == 'awaiting_single_account':
        session_string = update.message.text
        msg = await update.message.reply_text("⏳ Processing...")
        status, user_info = await start_userbot(session_string, context.application, update_info=True)
        if status == "success": await msg.edit_text(f"✅ Account added: {escape_markdown_v2(user_info.first_name)}", parse_mode=ParseMode.MARKDOWN_V2)
        elif status == "already_exists": await msg.edit_text("⚠️ Account already exists.")
        else: await msg.edit_text("❌ Invalid session string.")
        await start_command(update, context)

    elif step == 'awaiting_multiple_accounts':
        text = update.message.text
        session_strings = [s.strip() for s in text.replace(",", " ").replace("\n", " ").split() if s.strip()]
        msg = await update.message.reply_text(f"Processing {len(session_strings)} strings...")
        success, fail = 0, 0
        for session in session_strings:
            status, _ = await start_userbot(session, context.application, update_info=True)
            if status == "success": success += 1
            else: fail += 1
        await msg.edit_text(f"Batch complete! ✅ Added: {success}, ❌ Failed: {fail}")
        await start_command(update, context)

async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if 'next_step' in context.user_data: del context.user_data['next_step']
    if 'temp_client' in context.user_data:
        client = context.user_data['temp_client']
        if client.is_connected: await client.disconnect()
    context.user_data.clear()
    await update.message.reply_text("Action cancelled.")
    await start_command(update, context)

# --- Independent Commands ---
async def generate_session_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Starts the session generation process."""
    # This is a callback query handler, so we get the query object
    query = update.callback_query
    await query.answer()
    context.user_data['next_step'] = 'awaiting_phone_number'
    await query.edit_message_text(
        "Starting session generator...\n\nPlease send me the phone number of the account you want to add (e.g., +1234567890)."
    )

async def add_generated_session_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Callback to add the last generated session string."""
    query = update.callback_query
    await query.answer()
    session_string = context.user_data.get('last_generated_session')
    if not session_string:
        await query.edit_message_text("No session string was found to add.", reply_markup=None)
        return

    await query.edit_message_text("⏳ Adding the new account...", reply_markup=None)
    status, user_info = await start_userbot(session_string, context.application, update_info=True)
    if status == "success":
        await query.edit_message_text(f"✅ Account added: {escape_markdown_v2(user_info.first_name)}", parse_mode=ParseMode.MARKDOWN_V2)
    elif status == "already_exists":
        await query.edit_message_text("⚠️ This account already exists.")
    else:
        await query.edit_message_text("❌ An error occurred while adding the account.")
    await asyncio.sleep(2)
    await start_command(query, context)


async def ping_command(update: Update, context: ContextTypes.DEFAULT_TYPE): pass # Full function omitted for brevity
async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE): pass # Full function omitted for brevity
async def refresh_command(update: Update, context: ContextTypes.DEFAULT_TYPE): pass # Full function omitted for brevity
async def temp_pause_command(update: Update, context: ContextTypes.DEFAULT_TYPE): pass # Full function omitted for brevity
async def temp_fwd_command(update: Update, context: ContextTypes.DEFAULT_TYPE): pass # Full function omitted for brevity

# --- Main Application Runner with Leader Election ---
async def main():
    application = Application.builder().token(BOT_TOKEN).build()

    # Add all command handlers
    application.add_handler(CommandHandler("start", lambda u, c: owner_only(u, c, start_command)))
    application.add_handler(CommandHandler("cancel", lambda u, c: owner_only(u, c, cancel_command)))
    application.add_handler(CommandHandler("ping", ping_command))
    application.add_handler(CommandHandler("status", lambda u, c: owner_only(u, c, status_command)))
    #... and so on for all other commands

    # Add all callback handlers
    application.add_handler(CallbackQueryHandler(generate_session_command, pattern="^generate_session$"))
    application.add_handler(CallbackQueryHandler(add_generated_session_callback, pattern="^add_generated_session$"))
    #... and so on for all other buttons

    # The main text handler for state-based inputs
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_input))

    # The rest of the main function...
    
if __name__ == "__main__":
    pass
