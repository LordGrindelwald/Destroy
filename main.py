# ██████╗ ██╗   ██╗███████╗██████╗  ██████╗ ████████╗
# ██╔══██╗██║   ██║██╔════╝██╔══██╗██╔═══██╗╚══██╔══╝
# ██████╔╝██║   ██║█████╗  ██████╔╝██║   ██║   ██║
# ██╔══██╗██║   ██║██╔══╝  ██╔══██╗██║   ██║   ██║
# ██████╔╝╚██████╔╝███████╗██║  ██║╚██████╔╝   ██║
# ╚═════╝  ╚═════╝ ╚══════╝╚═╝  ╚═╝ ╚═════╝    ╚═╝
#
#           Userbot Forwarder Management Bot
#          (Definitive Version v2.9 - Final)

import os
import asyncio
import logging
import random
import string
import re
from datetime import datetime
from functools import partial
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

# --- Userbot Core Logic ---
async def start_userbot(session_string: str, ptb_app: Application, update_info: bool = False):
    try:
        client = PyrogramClient(
            name=f"userbot_{random.randint(1000, 9999)}",
            api_id=API_ID, api_hash=API_HASH, session_string=session_string, in_memory=True,
            device_model="Hexagram",
            system_version="1.7.3",
            app_version="1.7.3",
            lang_code="en"
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
    if message.chat.id != source_chat_id:
        return

    target_chat = await get_target_chat()
    if not target_chat:
        logger.warning("Target chat not set, cannot forward.")
        return

    asyncio.gather(
        forward_message(client, message, target_chat),
        send_notification(client, message, ptb_app)
    )

async def forward_message(client, message, target_chat):
    if client.me.id in paused_forwarding:
        return

    try:
        if random.random() < 0.90:
            await message.forward(chat_id=target_chat)
        else:
            await message.copy(chat_id=target_chat)
    except Exception as e:
        logger.error(f"Failed to forward message {message.id} from {client.me.id}: {e}")

async def send_notification(client, message, ptb_app):
    if OWNER_ID in paused_notifications:
        return

    status_parts = []
    if client.me.id in paused_forwarding:
        status_parts.append("⏸️ Fwd Paused")
    else:
        status_parts.append("✅ Fwd Active")

    if OWNER_ID in paused_notifications:
        status_parts.append("⏸️ Notify Paused")
    else:
        status_parts.append("✅ Notify Active")

    status_text = " | ".join(status_parts)
    content = message.text or message.caption or "(Media without caption)"
    header = f"👤 <b>{escape_html(client.me.first_name)}</b>"
    notification_text = (
        f"{header}\n<b>Status:</b> {status_text}\n\n"
        f"<b>Content:</b>\n<code>{escape_html(content[:3000])}</code>"
    )
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
async def owner_only(update: Update, context: ContextTypes.DEFAULT_TYPE, command_handler):
    if update.effective_user.id != OWNER_ID:
        if update.message: await update.message.reply_text("⛔️ You are not authorized.")
        elif update.callback_query: await update.callback_query.answer("⛔️ You are not authorized.", show_alert=True)
        return
    await command_handler(update, context)

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_html(
        "👋 Welcome! I am your userbot forwarder manager.\n\n"
        "Use /settings to configure, /add to add accounts, and /remove to delete them."
    )

async def settings_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    keyboard = [
        [InlineKeyboardButton("📚 Set Source", callback_data="set_source")],
        [InlineKeyboardButton("🎯 Set Target", callback_data="set_target")],
        [InlineKeyboardButton("👤 Manage Accounts", callback_data="manage_accounts")],
        [InlineKeyboardButton("➕ Add Account", callback_data="call_add_command")],
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
    context.user_data.clear()
    keyboard = [
        [InlineKeyboardButton("Paste Single String", callback_data="add_single")],
        [InlineKeyboardButton("Paste Multiple Strings", callback_data="add_multiple")],
        [InlineKeyboardButton("Via Phone Number", callback_data="call_generate")],
        [InlineKeyboardButton("« Back to Settings", callback_data="main_settings")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    message_text = "<b>How would you like to add an account?</b>"
    if update.callback_query:
        await update.callback_query.edit_message_text(message_text, parse_mode=ParseMode.HTML, reply_markup=reply_markup)
    else:
        await update.message.reply_html(message_text, reply_markup=reply_markup)

async def handle_text_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    step = context.user_data.get('next_step')
    if not step: return

    if 'awaiting' in step and step.endswith(('phone_number', 'login_code', '2fa_password')):
        return

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
        elif status == "already_exists":
            await msg.edit_text("⚠️ Account already exists.")
        elif status == "account_restricted":
            await msg.edit_text("❌ <b>Error:</b> Login succeeded, but this account is restricted.", parse_mode=ParseMode.HTML)
        elif status == "invalid_string":
            await msg.edit_text("❌ <b>Error:</b> The session string is invalid or corrupted.", parse_mode=ParseMode.HTML)
        else:
            await msg.edit_text("❌ An unknown error occurred.")
        
        await asyncio.sleep(3)
        await add_command(update, context)

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    source_chat = await get_source_chat()
    target_chat = await get_target_chat() or "Not Set"
    running_bots = len(active_userbots)
    total_bots = accounts_collection.count_documents({})
    
    status_text = (f"📊 <b>Bot Status</b>\n\n"
        f"<b>Management Bot:</b> Online\n"
        f"<b>Source Chat:</b> <code>{source_chat}</code>\n"
        f"<b>Target Chat:</b> <code>{escape_html(target_chat)}</code>\n"
        f"<b>Userbots Running:</b> {running_bots}/{total_bots}\n"
        f"<b>Paused Forwarding:</b> {len(paused_forwarding)} bots\n"
        f"<b>Paused Notifications:</b> {'Yes' if OWNER_ID in paused_notifications else 'No'}\n")
    await update.message.reply_html(status_text)
    
async def temp_pause_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        user_id_to_pause = int(context.args[0])
        if user_id_to_pause not in active_userbots:
            await update.message.reply_text("User ID not found or bot is not active.")
            return
        
        paused_forwarding.add(user_id_to_pause)
        
        keyboard = [[InlineKeyboardButton("Pause Notifications (5 min)", callback_data="pause_notify")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await update.message.reply_text(
            f"✅ Paused forwarding for user ID {user_id_to_pause} for 5 minutes.",
            reply_markup=reply_markup
        )
        
        await asyncio.sleep(300)
        paused_forwarding.discard(user_id_to_pause)
        logger.info(f"Resumed forwarding for user ID {user_id_to_pause}.")
        await context.bot.send_message(OWNER_ID, f"Resumed forwarding for user ID {user_id_to_pause}.")

    except (IndexError, ValueError):
        await update.message.reply_text("Usage: /temp <user_id>")

async def pause_notifications_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    
    paused_notifications.add(OWNER_ID)
    await query.answer("✅ Notifications paused for 5 minutes.", show_alert=True)
    
    await query.edit_message_reply_markup(reply_markup=None)
    
    await asyncio.sleep(300)
    paused_notifications.discard(OWNER_ID)
    logger.info("Resumed notifications.")
    await context.bot.send_message(OWNER_ID, "Resumed notifications.")

async def temp_pause_all(update: Update, context: ContextTypes.DEFAULT_TYPE):
    for user_id in active_userbots.keys():
        paused_forwarding.add(user_id)
    paused_notifications.add(OWNER_ID)
    
    await update.message.reply_text("✅ Paused all forwarding and notifications for 5 minutes.")
    
    await asyncio.sleep(300)
    
    paused_forwarding.clear()
    paused_notifications.discard(OWNER_ID)
    logger.info("Resumed all forwarding and notifications.")
    await context.bot.send_message(OWNER_ID, "Resumed all forwarding and notifications.")

async def generate_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message or update.callback_query.message
    await message.reply_text("Starting session generator...\nPlease send the phone number in international format (e.g., +1234567890).")
    return PHONE

async def get_phone_number(update: Update, context: ContextTypes.DEFAULT_TYPE):
    phone = update.message.text
    msg = await update.message.reply_text("⏳ Connecting to Telegram...")
    client = PyrogramClient(
        name=f"userbot_{random.randint(1000, 9999)}",
        api_id=API_ID, api_hash=API_HASH, in_memory=True,
        device_model="Hexagram",
        system_version="1.7.3",
        app_version="1.7.3",
        lang_code="en"
    )
    try:
        await asyncio.wait_for(client.connect(), timeout=30.0)
    except asyncio.TimeoutError:
        await msg.edit_text("❌ <b>Error:</b> Connection to Telegram timed out. Cancelled.")
        return ConversationHandler.END
    except Exception as e:
        await msg.edit_text(f"❌ <b>Error:</b> <code>{escape_html(str(e))}</code>\nCancelled.")
        return ConversationHandler.END

    try:
        await msg.edit_text("⏳ Sending login code...")
        sent_code = await client.send_code(phone)
        context.user_data.update({'phone': phone, 'phone_code_hash': sent_code.phone_code_hash, 'temp_client': client})
        await msg.edit_text("A login code has been sent to your Telegram account. Please send it here.")
        return CODE
    except (ApiIdInvalid, PhoneNumberInvalid) as e:
        await msg.edit_text(f"❌ <b>Error:</b> {e}. Process cancelled.")
        await client.disconnect()
        return ConversationHandler.END
    except Exception as e:
        await msg.edit_text(f"❌ <b>Error:</b> <code>{escape_html(str(e))}</code>. Cancelled.")
        if client.is_connected: await client.disconnect()
        return ConversationHandler.END

async def get_login_code(update: Update, context: ContextTypes.DEFAULT_TYPE):
    code = update.message.text
    client = context.user_data['temp_client']
    phone = context.user_data['phone']
    phone_code_hash = context.user_data['phone_code_hash']
    msg = await update.message.reply_text("⏳ Signing in...")
    try:
        await client.sign_in(phone, phone_code_hash, code)
        session_string = await client.export_session_string()
        context.user_data['session_string'] = session_string
        
        keyboard = [[InlineKeyboardButton("Add Account", callback_data="add_account")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await msg.reply_html(
            f"✅ Session generated successfully!\n\n<code>{session_string}</code>",
            reply_markup=reply_markup
        )
        return ADD_ACCOUNT
    except SessionPasswordNeeded:
        await msg.edit_text("2FA is enabled. Please send your password.")
        return PASSWORD
    except (PhoneCodeInvalid, PhoneCodeExpired) as e:
        await msg.edit_text(f"❌ <b>Error:</b> {e}. Cancelled.")
        if client.is_connected: await client.disconnect()
        return ConversationHandler.END
    finally:
        await update.message.delete()

async def get_2fa_password(update: Update, context: ContextTypes.DEFAULT_TYPE):
    password = update.message.text
    client = context.user_data['temp_client']
    msg = await update.message.reply_text("⏳ Checking password...")
    try:
        await client.check_password(password)
        session_string = await client.export_session_string()
        context.user_data['session_string'] = session_string

        keyboard = [[InlineKeyboardButton("Add Account", callback_data="add_account")]]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await msg.reply_html(
            f"✅ Session generated successfully!\n\n<code>{session_string}</code>",
            reply_markup=reply_markup
        )
        return ADD_ACCOUNT
    except PasswordHashInvalid:
        await msg.edit_text("❌ <b>Error:</b> Incorrect password. Cancelled.")
        if client.is_connected: await client.disconnect()
    finally:
        await update.message.delete()
    return ConversationHandler.END

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

async def ping_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    start_time = datetime.now()
    message = await update.message.reply_text("Pinging...")
    end_time = datetime.now()
    latency = (end_time - start_time).microseconds / 1000
    await message.edit_text(f"🏓 Pong!\nLatency: {latency:.2f} ms")

async def refresh_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text("🔄 Stopping all userbots...")
    for uid, client in list(active_userbots.items()):
        await client.stop()
        del active_userbots[uid]
    
    await msg.edit_text("🔄 Restarting and refreshing userbot details...")
    started, total = await start_all_userbots_from_db(context.application, update_info=True)
    await msg.edit_text(f"✅ Refresh complete. Started {started}/{total} userbots.")

async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if 'temp_client' in context.user_data:
        client = context.user_data.get('temp_client')
        if client and client.is_connected: await client.disconnect()
    context.user_data.clear()
    await update.message.reply_text("Action cancelled.")
    return ConversationHandler.END

# --- Main Runner ---
def main():
    """Initializes and runs the bot."""
    application = Application.builder().token(BOT_TOKEN).build()
    
    gen_conv = ConversationHandler(
        entry_points=[
            CommandHandler("generate", lambda u, c: owner_only(u, c, generate_command)),
            CallbackQueryHandler(lambda u, c: generate_command(u.callback_query, c), pattern="^call_generate$")
        ],
        states={
            PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_phone_number)],
            CODE: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_login_code)],
            PASSWORD: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_2fa_password)],
            ADD_ACCOUNT: [CallbackQueryHandler(add_account_callback, pattern="^add_account$")]
        },
        fallbacks=[CommandHandler("cancel", lambda u, c: owner_only(u, c, cancel_command))],
        conversation_timeout=300
    )

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("settings", lambda u, c: owner_only(u, c, settings_command)))
    application.add_handler(CommandHandler("add", lambda u, c: owner_only(u, c, add_command)))
    application.add_handler(CommandHandler("status", lambda u, c: owner_only(u, c, status_command)))
    application.add_handler(CommandHandler("temp", lambda u, c: owner_only(u, c, temp_pause_command)))
    application.add_handler(CommandHandler("temp_fwd", lambda u, c: owner_only(u, c, temp_pause_all)))
    application.add_handler(CommandHandler("ping", ping_command))
    application.add_handler(CommandHandler("refresh", lambda u, c: owner_only(u, c, refresh_command)))
    application.add_handler(CommandHandler("cancel", lambda u, c: owner_only(u, c, cancel_command)))
    application.add_handler(gen_conv)

    application.add_handler(CallbackQueryHandler(pause_notifications_callback, pattern="^pause_notify$"))
    application.add_handler(CallbackQueryHandler(lambda u,c: set_next_step(u, c, 'awaiting_source', "Please send the source chat ID."), pattern="^set_source$"))
    application.add_handler(CallbackQueryHandler(lambda u,c: set_next_step(u, c, 'awaiting_target', "Please send the target bot username."), pattern="^set_target$"))
    application.add_handler(CallbackQueryHandler(lambda u,c: set_next_step(u, c, 'awaiting_single_account', "Please paste the session string."), pattern="^add_single$"))
    
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_input))

    loop = asyncio.get_event_loop()
    loop.run_until_complete(start_all_userbots_from_db(application))

    logger.info("Bot is starting...")
    application.run_polling()

if __name__ == "__main__":
    main()
