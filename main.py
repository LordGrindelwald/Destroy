# ██████╗ ██╗   ██╗███████╗██████╗  ██████╗ ████████╗
# ██╔══██╗██║   ██║██╔════╝██╔══██╗██╔═══██╗╚══██╔══╝
# ██████╔╝██║   ██║█████╗  ██████╔╝██║   ██║   ██║
# ██╔══██╗██║   ██║██╔══╝  ██╔══██╗██║   ██║   ██║
# ██████╔╝╚██████╔╝███████╗██║  ██║╚██████╔╝   ██║
# ╚═════╝  ╚═════╝ ╚══════╝╚═╝  ╚═╝ ╚═════╝    ╚═╝
#
#           Userbot Forwarder Management Bot
#          (Version 2.0 - Complete Rework)

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
    SessionPasswordNeeded, PhoneNumberInvalid, PhoneCodeInvalid, PhoneCodeExpired, PasswordHashInvalid
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

# --- State definitions for ConversationHandler ---
PHONE, CODE, PASSWORD = range(3)

def escape_html(text: str) -> str:
    if not isinstance(text, str): text = str(text)
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

def generate_device_name():
    device_names = ["Desktop", "Laptop", "Workstation", "PC", "Computer", "System"]
    suffix = ''.join(random.choices(string.ascii_uppercase + string.digits, k=7))
    return f"{random.choice(device_names)}-{suffix}"

# --- Userbot Core Logic ---
async def start_userbot(session_string: str, ptb_app: Application, update_info: bool = False):
    client = PyrogramClient(
        name=f"userbot_{random.randint(1000, 9999)}",
        api_id=API_ID, api_hash=API_HASH, session_string=session_string, in_memory=True,
        device_model=generate_device_name(),
        system_version="Telegram Desktop 4.8.3",
        app_version="4.8.3",
        lang_code="en"
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
        if "AUTH_KEY_PERM_EMPTY" in str(e):
             return "account_restricted", None
        if client.is_connected: await client.stop()
        return "error", None

# --- Main Bot Commands & UI ---
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

# ... (Other commands like settings, remove, status, etc. will be added here)

# --- Session Generator Rework ---
async def generate_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Starting session generator...\nPlease send the phone number in international format (e.g., +1234567890).")
    return PHONE

async def get_phone_number(update: Update, context: ContextTypes.DEFAULT_TYPE):
    phone = update.message.text
    await update.message.reply_text("⏳ Connecting to Telegram...")
    
    client = PyrogramClient(
        name=f"generator_{update.effective_user.id}",
        api_id=API_ID, api_hash=API_HASH, in_memory=True,
        device_model=generate_device_name(),
        system_version="Telegram Desktop 4.8.3",
        app_version="4.8.3",
        lang_code="en"
    )
    
    try:
        await client.connect()
        sent_code = await client.send_code(phone)
        
        context.user_data['phone'] = phone
        context.user_data['phone_code_hash'] = sent_code.phone_code_hash
        context.user_data['temp_client'] = client
        
        await update.message.reply_text("A login code has been sent. Please send it here.")
        return CODE
        
    except Exception as e:
        logger.error(f"Error during phone number stage: {e}")
        await update.message.reply_html(f"❌ <b>Error:</b> <code>{escape_html(str(e))}</code>\nPlease double-check your API_ID/HASH. Process cancelled.")
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
        context.user_data.clear()
        return ConversationHandler.END
        
    except SessionPasswordNeeded:
        await update.message.reply_text("2FA is enabled. Please send your password.")
        return PASSWORD
    except (PhoneCodeInvalid, PhoneCodeExpired):
        await update.message.reply_html("❌ <b>Error:</b> Invalid or expired code. Cancelled.")
        if client.is_connected: await client.disconnect()
        context.user_data.clear()
        return ConversationHandler.END
    except Exception as e:
        logger.error(f"Error during login code stage: {e}")
        await update.message.reply_html(f"❌ <b>Error:</b> <code>{escape_html(str(e))}</code>. Cancelled.")
        if client.is_connected: await client.disconnect()
        context.user_data.clear()
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
        context.user_data.clear()
        await update.message.delete()
        
    return ConversationHandler.END

async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if 'temp_client' in context.user_data:
        client = context.user_data.get('temp_client')
        if client and client.is_connected: await client.disconnect()
    context.user_data.clear()
    await update.message.reply_text("Action cancelled.")
    return ConversationHandler.END

# --- Health Check Server for Koyeb ---
async def health_check_server():
    host, port = "0.0.0.0", int(os.getenv("PORT", 8080))
    server = await asyncio.start_server(lambda r, w: w.close(), host, port)
    logger.info(f"Health check server started on port {port}")
    async with server: await server.serve_forever()

# --- Main Application Runner with Leader Election ---
async def run_bot_as_leader(application: Application):
    logger.info("👑 This process is the leader. Starting all services.")
    await application.initialize()
    await application.start()
    await application.updater.start_polling()
    # await start_all_userbots_from_db(application) # We can re-enable this later
    while True:
        await asyncio.sleep(3600)

async def main():
    host, port = "0.0.0.0", int(os.getenv("PORT", 8080))
    server = None
    try:
        server = await asyncio.start_server(lambda r, w: w.close(), host, port)
        application = Application.builder().token(BOT_TOKEN).build()
        
        # Reworked ConversationHandler for the generator
        gen_conv_handler = ConversationHandler(
            entry_points=[CommandHandler("generate", lambda u, c: owner_only(u, c, generate_command))],
            states={
                PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_phone_number)],
                CODE: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_login_code)],
                PASSWORD: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_2fa_password)],
            },
            fallbacks=[CommandHandler("cancel", cancel_command)],
            conversation_timeout=300 # 5 minute timeout
        )
        
        application.add_handler(CommandHandler("start", start_command))
        application.add_handler(gen_conv_handler)
        # We will add back /settings, /add, /remove, etc. after this core function is confirmed to work.
        
        await run_bot_as_leader(application)
    except OSError:
        logger.info(f"Port {port} in use. This is a follower process, staying idle.")
        while True:
            await asyncio.sleep(3600)
    finally:
        if server:
            server.close()
            await server.wait_closed()

if __name__ == "__main__":
    asyncio.run(main())
