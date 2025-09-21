# ██████╗ ██╗   ██╗███████╗██████╗  ██████╗ ████████╗
# ██╔══██╗██║   ██║██╔════╝██╔══██╗██╔═══██╗╚══██╔══╝
# ██████╔╝██║   ██║█████╗  ██████╔╝██║   ██║   ██║
# ██╔══██╗██║   ██║██╔══╝  ██╔══██╗██║   ██║   ██║
# ██████╔╝╚██████╔╝███████╗██║  ██║╚██████╔╝   ██║
# ╚═════╝  ╚═════╝ ╚══════╝╚═╝  ╚═╝ ╚═════╝    ╚═╝
#
#           Userbot Forwarder Management Bot
#          (Definitive Version with All Fixes)

import os
import asyncio
import logging
import random
import string
from datetime import datetime
from functools import partial
from pymongo import MongoClient
from dotenv import load_dotenv

from pyrogram import Client as PyrogramClient
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
PHONE, CODE, PASSWORD, SET_SOURCE, SET_TARGET, ADD_SINGLE, ADD_MULTIPLE = range(7)

def escape_html(text: str) -> str:
    """Escapes special characters for Telegram HTML parsing."""
    if not isinstance(text, str): text = str(text)
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

def generate_device_name():
    """Generates a realistic device name from a predefined list."""
    device_names = [
        "MSI-B550-GAMING-PLUS", "ASUS-ROG-STRIX-Z690-E", "GIGABYTE-AORUS-MASTER",
        "DELL-XPS-DESKTOP", "HP-PAVILION-GAMING", "LENOVO-LEGION-TOWER", "ALIENWARE-AURORA-R13"
    ]
    suffix = ''.join(random.choices(string.digits, k=4))
    return f"{random.choice(device_names)}-{suffix}"

# --- Userbot Core Logic ---
async def start_userbot(session_string: str, ptb_app: Application, update_info: bool = False):
    client = PyrogramClient(
        name=f"userbot_{random.randint(1000, 9999)}",
        api_id=API_ID, api_hash=API_HASH, session_string=session_string, in_memory=True,
        device_model=generate_device_name(), system_version="Telegram Desktop 4.8.3",
        app_version="4.8.3", lang_code="en"
    )
    try:
        await client.start()
        me = await client.get_me()
        if me.id in active_userbots:
            await client.stop()
            return "already_exists", None
        
        handler_with_context = partial(forwarder_handler, ptb_app=ptb_app)
        client.add_handler(PyrogramMessageHandler(handler_with_context, filters=~filters.service))
        
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
        if client.is_connected: await client.stop()
        return "error", None

async def forwarder_handler(client: PyrogramClient, message: Message, ptb_app: Application):
    if client.me.id in paused_notifications: return
    source_chat_id = await get_source_chat()
    if message.chat.id == source_chat_id:
        try:
            target_chat = await get_target_chat()
            is_forwarding_paused = client.me.id in paused_forwarding
            if not is_forwarding_paused:
                if target_chat:
                    if random.random() < 0.90: await message.forward(chat_id=target_chat)
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
    return config.get("source_chat_id", "Not Set") if config else "Not Set"

async def get_target_chat():
    config = config_collection.find_one({"_id": "config"})
    return config.get("target_chat_username") if config else None

# --- Management Bot Handlers ---
async def owner_only(update: Update, context: ContextTypes.DEFAULT_TYPE, command_handler):
    if update.effective_user.id != OWNER_ID:
        await update.message.reply_text("⛔️ You are not authorized.")
        return
    await command_handler(update, context)

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_html(
        "👋 Welcome! I am your userbot forwarder manager.\n\n"
        "Use /settings to configure, /add to add accounts, and /remove to delete them."
    )

async def settings_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("📚 Set Source", callback_data="set_source")],
        [InlineKeyboardButton("🎯 Set Target", callback_data="set_target")],
        [InlineKeyboardButton("👤 Manage Accounts", callback_data="manage_accounts")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    source_chat_id = await get_source_chat()
    target_chat = await get_target_chat() or "Not Set"
    message_text = (
        f"⚙️ <b>Settings Dashboard</b>\n\n"
        f"▶️ <b>Source:</b> <code>{source_chat_id}</code>\n"
        f"🎯 <b>Target:</b> <code>{escape_html(target_chat)}</code>"
    )
    if update.callback_query:
        await update.callback_query.edit_message_text(message_text, parse_mode=ParseMode.HTML, reply_markup=reply_markup)
    else:
        await update.message.reply_html(message_text, reply_markup=reply_markup)

async def add_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("Paste Single String", callback_data="add_single")],
        [InlineKeyboardButton("Paste Multiple Strings", callback_data="add_multiple")],
        [InlineKeyboardButton("Via Phone Number (/generate)", callback_data="call_generate")],
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
    keyboard = [[InlineKeyboardButton("« Back to Settings", callback_data="main_settings")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=reply_markup)

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
    keyboard.append([InlineKeyboardButton("« Cancel", callback_data="main_settings")])
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_html("Please select an account to remove:", reply_markup=reply_markup)

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

# --- Conversation Handlers ---
async def ask_for_input(update: Update, context: ContextTypes.DEFAULT_TYPE, state, prompt: str):
    """Generic function to start a conversation step."""
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text(prompt)
    else:
        await update.message.reply_text(prompt)
    return state

async def set_source_from_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        chat_id = int(update.message.text)
        config_collection.update_one({"_id": "config"}, {"$set": {"source_chat_id": chat_id}}, upsert=True)
        await update.message.reply_text(f"✅ Source chat updated to: {chat_id}")
    except ValueError: await update.message.reply_text("❌ Invalid ID.")
    await settings_command(update, context)
    return ConversationHandler.END

async def set_target_from_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    username = update.message.text.strip()
    if username.startswith("@") and len(username) > 4:
        config_collection.update_one({"_id": "config"}, {"$set": {"target_chat_username": username}}, upsert=True)
        await update.message.reply_text(f"✅ Target chat updated to: {username}")
    else: await update.message.reply_text("❌ Invalid username.")
    await settings_command(update, context)
    return ConversationHandler.END
    
async def add_single_from_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    session_string = update.message.text
    msg = await update.message.reply_text("⏳ Processing...")
    status, user_info = await start_userbot(session_string, context.application, update_info=True)
    if status == "success": await msg.edit_text(f"✅ Account added: {escape_html(user_info.first_name)}", parse_mode=ParseMode.HTML)
    elif status == "already_exists": await msg.edit_text("⚠️ Account already exists.")
    elif status == "account_restricted": await msg.edit_text("❌ <b>Error:</b> Login succeeded, but this account is restricted.", parse_mode=ParseMode.HTML)
    else: await msg.edit_text("❌ Invalid session string.")
    await asyncio.sleep(2)
    await add_command(update, context)
    return ConversationHandler.END
    
async def add_multiple_from_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    session_strings = [s.strip() for s in text.replace(",", " ").replace("\n", " ").split() if s.strip()]
    msg = await update.message.reply_text(f"Processing {len(session_strings)} strings...")
    success, fail = 0, 0
    for session in session_strings:
        status, _ = await start_userbot(session, context.application, update_info=True)
        if status == "success": success += 1
        else: fail += 1
    await msg.edit_text(f"Batch complete! ✅ Added: {success}, ❌ Failed: {fail}")
    await asyncio.sleep(2)
    await add_command(update, context)
    return ConversationHandler.END

# --- Session Generator Rework ---
async def generate_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Starting session generator...\nPlease send the phone number in international format (e.g., +1234567890).")
    return PHONE

async def get_phone_number(update: Update, context: ContextTypes.DEFAULT_TYPE):
    phone = update.message.text
    msg = await update.message.reply_text("⏳ Connecting to Telegram...")
    client = PyrogramClient(
        name=f"generator_{update.effective_user.id}",
        api_id=API_ID, api_hash=API_HASH, in_memory=True,
        device_model=generate_device_name(),
        system_version="Telegram Desktop 4.8.3",
        app_version="4.8.3",
        lang_code="en"
    )
    try:
        await asyncio.wait_for(client.connect(), timeout=30.0)
    except asyncio.TimeoutError:
        await msg.edit_text("❌ <b>Error:</b> Connection to Telegram timed out. The server might be busy or your API keys might be wrong. Cancelled.")
        return ConversationHandler.END
    except Exception as e:
        logger.error(f"Error during client.connect(): {e}")
        await msg.edit_text(f"❌ <b>Error:</b> <code>{escape_html(str(e))}</code>\nCancelled.")
        return ConversationHandler.END

    try:
        await msg.edit_text("⏳ Sending login code...")
        sent_code = await client.send_code(phone)
        context.user_data['phone'] = phone
        context.user_data['phone_code_hash'] = sent_code.phone_code_hash
        context.user_data['temp_client'] = client
        await msg.edit_text("A login code has been sent to your Telegram account. Please send it here.")
        return CODE
    except ApiIdInvalid:
        await msg.edit_text("❌ <b>CRITICAL ERROR:</b> The `API_ID` and `API_HASH` are invalid. Please check your environment variables. Process cancelled.")
        await client.disconnect()
        return ConversationHandler.END
    except PhoneNumberInvalid:
        await msg.edit_text("❌ <b>Error:</b> The phone number is invalid. Process cancelled.")
        await client.disconnect()
        return ConversationHandler.END
    except Exception as e:
        logger.error(f"Error during client.send_code(): {e}")
        await msg.edit_text(f"❌ <b>Error:</b> <code>{escape_html(str(e))}</code>\nThis can happen if the API keys are incorrect. Process cancelled.")
        if client.is_connected: await client.disconnect()
        return ConversationHandler.END

async def get_login_code(update: Update, context: ContextTypes.DEFAULT_TYPE):
    code = update.message.text
    client = context.user_data['temp_client']
    phone = context.user_data['phone']
    phone_code_hash = context.user_data['phone_code_hash']
    await update.message.reply_text("⏳ Signing in...")
    try:
        await client.sign_in(phone, phone_code_hash, code)
        session_string = await client.export_session_string()
        await client.disconnect()
        await update.message.reply_html(f"✅ <b>Success!</b> Session string:\n\n<code>{session_string}</code>")
        return ConversationHandler.END
    except SessionPasswordNeeded:
        await update.message.reply_text("2FA is enabled. Please send your password.")
        return PASSWORD
    except (PhoneCodeInvalid, PhoneCodeExpired):
        await update.message.reply_html("❌ <b>Error:</b> Invalid or expired code. Cancelled.")
        if client.is_connected: await client.disconnect()
        return ConversationHandler.END
    except Exception as e:
        logger.error(f"Error during login code stage: {e}")
        await update.message.reply_html(f"❌ <b>Error:</b> <code>{escape_html(str(e))}</code>. Cancelled.")
        if client.is_connected: await client.disconnect()
        return ConversationHandler.END
    finally:
        await update.message.delete()

async def get_2fa_password(update: Update, context: ContextTypes.DEFAULT_TYPE):
    password = update.message.text
    client = context.user_data['temp_client']
    await update.message.reply_text("⏳ Checking password...")
    try:
        await client.check_password(password)
        session_string = await client.export_session_string()
        await client.disconnect()
        await update.message.reply_html(f"✅ <b>Success!</b> Session string:\n\n<code>{session_string}</code>")
    except PasswordHashInvalid:
        await update.message.reply_html("❌ <b>Error:</b> Incorrect password. Cancelled.")
    except Exception as e:
        logger.error(f"Error during 2FA stage: {e}")
        await update.message.reply_html(f"❌ <b>Error:</b> <code>{escape_html(str(e))}</code>. Cancelled.")
    finally:
        if client.is_connected: await client.disconnect()
        await update.message.delete()
    return ConversationHandler.END

async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if 'temp_client' in context.user_data:
        client = context.user_data.get('temp_client')
        if client and client.is_connected: await client.disconnect()
    context.user_data.clear()
    await update.message.reply_text("Action cancelled.")
    return ConversationHandler.END

# --- Health Check Server & Main Runner ---
async def main():
    application = Application.builder().token(BOT_TOKEN).build()
    
    # Conversation Handlers
    gen_conv = ConversationHandler(
        entry_points=[
            CommandHandler("generate", lambda u, c: owner_only(u, c, generate_command)),
            CallbackQueryHandler(lambda u,c: generate_command(u.callback_query, c), pattern="^call_generate$")
        ],
        states={
            PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_phone_number)],
            CODE: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_login_code)],
            PASSWORD: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_2fa_password)],
        },
        fallbacks=[CommandHandler("cancel", cancel_command)], conversation_timeout=300
    )
    
    source_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(lambda u,c: ask_for_input(u,c, SET_SOURCE, "Please send the source chat ID."), pattern="^set_source$")],
        states={ SET_SOURCE: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_source_from_input)] },
        fallbacks=[CommandHandler("cancel", cancel_command)]
    )
    target_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(lambda u,c: ask_for_input(u,c, SET_TARGET, "Please send the target bot username."), pattern="^set_target$")],
        states={ SET_TARGET: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_target_from_input)] },
        fallbacks=[CommandHandler("cancel", cancel_command)]
    )
    add_single_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(lambda u,c: ask_for_input(u,c, ADD_SINGLE, "Please send the session string."), pattern="^add_single$")],
        states={ ADD_SINGLE: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_single_from_input)] },
        fallbacks=[CommandHandler("cancel", cancel_command)]
    )
    add_multiple_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(lambda u,c: ask_for_input(u,c, ADD_MULTIPLE, "Please send all session strings."), pattern="^add_multiple$")],
        states={ ADD_MULTIPLE: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_multiple_from_input)] },
        fallbacks=[CommandHandler("cancel", cancel_command)]
    )
    
    # Command Handlers
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("settings", lambda u, c: owner_only(u, c, settings_command)))
    application.add_handler(CommandHandler("add", lambda u, c: owner_only(u, c, add_command)))
    application.add_handler(CommandHandler("remove", lambda u, c: owner_only(u, c, remove_account_menu)))
    application.add_handler(CommandHandler("ping", ping_command))
    application.add_handler(CommandHandler("status", lambda u, c: owner_only(u, c, status_command)))
    application.add_handler(CommandHandler("refresh", lambda u, c: owner_only(u, c, refresh_command)))
    
    # Add Conversations to Application
    application.add_handler(gen_conv)
    application.add_handler(source_conv)
    application.add_handler(target_conv)
    application.add_handler(add_single_conv)
    application.add_handler(add_multiple_conv)

    # Callback Handlers
    application.add_handler(CallbackQueryHandler(accounts_menu, pattern="^manage_accounts$"))
    application.add_handler(CallbackQueryHandler(lambda u,c: settings_command(u,c), pattern="^main_settings$"))
    application.add_handler(CallbackQueryHandler(execute_remove_account, pattern="^delete_account_"))

    logger.info("Bot is starting...")
    await application.run_polling()

if __name__ == "__main__":
    asyncio.run(main())
