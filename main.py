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

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_html(
        "👋 Welcome! I am your userbot forwarder manager.\n\n"
        "Use /settings to configure me, or /add to add accounts."
    )

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
        [InlineKeyboardButton("Via Phone Number", callback_data="generate_session_from_add")],
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

async def set_next_step(update: Update, context: ContextTypes.DEFAULT_TYPE, step: str, text: str):
    query = update.callback_query
    await query.answer()
    context.user_data['next_step'] = step
    await query.edit_message_text(text)

async def handle_text_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    step = context.user_data.get('next_step')
    if not step: return

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
            await update.message.reply_html("❌ <b>Error:</b> The phone number is invalid. Process cancelled.")
            context.user_data.clear()
        except Exception as e:
            await update.message.reply_html(f"❌ <b>An unexpected error occurred:</b> {e}. Process cancelled.")
            context.user_data.clear()
        return

    if step == 'awaiting_login_code':
        code = update.message.text
        client = context.user_data['temp_client']
        phone = context.user_data['phone']
        phone_code_hash = context.user_data['phone_code_hash']
        try:
            await client.sign_in(phone, phone_code_hash, code)
            session_string = await client.export_session_string()
            await client.disconnect()
            keyboard = [[InlineKeyboardButton("➕ Add this account now", callback_data=f"add_generated_session:{session_string}")]]
            await update.message.reply_html(f"✅ <b>Success!</b> Here is your session string:\n\n<code>{session_string}</code>", reply_markup=InlineKeyboardMarkup(keyboard))
            context.user_data.clear()
        except SessionPasswordNeeded:
            context.user_data['next_step'] = 'awaiting_2fa_password'
            await update.message.reply_text("Your account has 2FA enabled. Please send your password.")
        except (PhoneCodeInvalid, PhoneCodeExpired):
            await update.message.reply_html("❌ <b>Error:</b> The login code is invalid or expired. Cancelled.")
            if client.is_connected: await client.disconnect()
            context.user_data.clear()
        finally:
             await update.message.delete()
        return

    if step == 'awaiting_2fa_password':
        password = update.message.text
        client = context.user_data['temp_client']
        try:
            await client.check_password(password)
            session_string = await client.export_session_string()
            await client.disconnect()
            keyboard = [[InlineKeyboardButton("➕ Add this account now", callback_data=f"add_generated_session:{session_string}")]]
            await update.message.reply_html(f"✅ <b>Success!</b> Here is your session string:\n\n<code>{session_string}</code>", reply_markup=InlineKeyboardMarkup(keyboard))
        except PasswordHashInvalid:
            await update.message.reply_html("❌ <b>Error:</b> The password is incorrect. Cancelled.")
            if client.is_connected: await client.disconnect()
            context.user_data.clear()
        finally:
            await update.message.delete()
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
        session_string = update.message.text
        msg = await update.message.reply_text("⏳ Processing...")
        status, user_info = await start_userbot(session_string, context.application, update_info=True)
        if status == "success": await msg.edit_text(f"✅ Account added: {escape_html(user_info.first_name)}", parse_mode=ParseMode.HTML)
        elif status == "already_exists": await msg.edit_text("⚠️ Account already exists.")
        else: await msg.edit_text("❌ Invalid session string.")
        await asyncio.sleep(2)
        await add_command(update, context)

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
        await asyncio.sleep(2)
        await add_command(update, context)

async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if 'temp_client' in context.user_data:
        client = context.user_data['temp_client']
        if client.is_connected: await client.disconnect()
    context.user_data.clear()
    await update.message.reply_text("Action cancelled.")

# --- Session Generator Handlers ---
async def ask_to_generate_session(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    context.user_data['next_step'] = 'awaiting_phone_number'
    prompt_text = "Starting session generator...\n\nPlease send the phone number in international format (e.g., +1234567890)."
    if query:
        await query.answer()
        await query.edit_message_text(prompt_text)
    else:
        await update.message.reply_text(prompt_text)

async def add_generated_session_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    session_string = query.data.split(":", 1)[1]
    
    await query.edit_message_text("⏳ Adding the new account...", reply_markup=None)
    status, user_info = await start_userbot(session_string, context.application, update_info=True)
    
    if status == "success": await query.edit_message_text(f"✅ Account added: {escape_html(user_info.first_name)}", parse_mode=ParseMode.HTML)
    elif status == "already_exists": await query.edit_message_text("⚠️ This account already exists.")
    else: await query.edit_message_text("❌ An error occurred.")
    
    await asyncio.sleep(3)
    await settings_command(update, context)

# --- Independent Commands ---
async def ping_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    start_time = datetime.now()
    message = await update.message.reply_text("Pinging...")
    end_time = datetime.now()
    latency = (end_time - start_time).microseconds / 1000
    await message.edit_text(f"🏓 Pong!\nLatency: {latency:.2f} ms")

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
        f"<b>Paused Notifications:</b> {len(paused_notifications)} bots")
    await update.message.reply_html(status_text)

async def refresh_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text("🔄 Stopping all userbots...")
    for uid, data in list(active_userbots.items()): await data["client"].stop(); del active_userbots[uid]
    await msg.edit_text("🔄 Restarting and refreshing details...")
    started, total = await start_all_userbots_from_db(context.application, update_info=True)
    await msg.edit_text(f"✅ Refresh complete. Started {started}/{total} userbots.")

async def unpause_forwarding_job(context: ContextTypes.DEFAULT_TYPE):
    uid = context.job.data["user_id"]
    if uid in paused_forwarding:
        paused_forwarding.remove(uid)
        await context.bot.send_message(OWNER_ID, f"▶️ Forwarding automatically resumed for <code>{uid}</code>.", parse_mode=ParseMode.HTML)

async def unpause_notifications_job(context: ContextTypes.DEFAULT_TYPE):
    uid = context.job.data["user_id"]
    if uid in paused_notifications:
        paused_notifications.remove(uid)
        await context.bot.send_message(OWNER_ID, f"🔔 Notifications automatically resumed for <code>{uid}</code>.", parse_mode=ParseMode.HTML)

async def temp_pause_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        uid = int(context.args[0])
        if uid not in active_userbots:
            await update.message.reply_text("❌ User ID not found.")
            return
        paused_forwarding.add(uid)
        context.job_queue.run_once(unpause_forwarding_job, 300, data={"user_id": uid}, name=f"unpause_fwd_{uid}")
        keyboard = [[InlineKeyboardButton("🤫 Also Pause Notifications (5 min)", callback_data=f"pause_notify_{uid}")]]
        await update.message.reply_html(f"⏸️ Forwarding paused for <code>{uid}</code> for 5 minutes.", reply_markup=InlineKeyboardMarkup(keyboard))
    except (IndexError, ValueError):
        await update.message.reply_text("Usage: /temp <userbot_user_id>")

async def temp_fwd_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        uid = int(context.args[0])
        if uid not in active_userbots:
            await update.message.reply_text("❌ User ID not found.")
            return
        paused_forwarding.add(uid)
        paused_notifications.add(uid)
        context.job_queue.run_once(unpause_forwarding_job, 300, data={"user_id": uid}, name=f"unpause_fwd_{uid}")
        context.job_queue.run_once(unpause_notifications_job, 300, data={"user_id": uid}, name=f"unpause_notify_{uid}")
        await update.message.reply_html(f"🤫 Silent pause enabled for <code>{uid}</code> for 5 minutes.")
    except (IndexError, ValueError):
        await update.message.reply_text("Usage: /temp_fwd <userbot_user_id>")

async def pause_notifications_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = int(query.data.split("_")[2])
    if uid not in active_userbots:
        await query.edit_message_text("This userbot is no longer active.", reply_markup=None)
        return
    paused_notifications.add(uid)
    context.job_queue.run_once(unpause_notifications_job, 300, data={"user_id": uid}, name=f"unpause_notify_{uid}")
    await query.edit_message_html(f"🤫 Notifications now also paused for <code>{uid}</code> for 5 minutes.", reply_markup=None)

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
    await start_all_userbots_from_db(application)
    while True:
        await asyncio.sleep(3600)

async def main():
    host, port = "0.0.0.0", int(os.getenv("PORT", 8080))
    server = None
    try:
        server = await asyncio.start_server(lambda r, w: w.close(), host, port)
        application = Application.builder().token(BOT_TOKEN).build()

        # Command handlers
        application.add_handler(CommandHandler("start", start_command))
        application.add_handler(CommandHandler("settings", lambda u, c: owner_only(u, c, settings_command)))
        application.add_handler(CommandHandler("add", lambda u, c: owner_only(u, c, add_command)))
        application.add_handler(CommandHandler("cancel", lambda u, c: owner_only(u, c, cancel_command)))
        application.add_handler(CommandHandler("ping", ping_command))
        application.add_handler(CommandHandler("status", lambda u, c: owner_only(u, c, status_command)))
        application.add_handler(CommandHandler("refresh", lambda u, c: owner_only(u, c, refresh_command)))
        application.add_handler(CommandHandler("temp", lambda u, c: owner_only(u, c, temp_pause_command)))
        application.add_handler(CommandHandler("temp_fwd", lambda u, c: owner_only(u, c, temp_fwd_command)))

        # Callback handlers for buttons
        application.add_handler(CallbackQueryHandler(ask_for_source_chat, pattern="^set_source$"))
        application.add_handler(CallbackQueryHandler(ask_for_target_chat, pattern="^set_target$"))
        application.add_handler(CallbackQueryHandler(accounts_menu, pattern="^manage_accounts$"))
        application.add_handler(CallbackQueryHandler(ask_to_generate_session, pattern="^generate_session$"))
        application.add_handler(CallbackQueryHandler(ask_to_generate_session, pattern="^generate_session_from_add$"))
        application.add_handler(CallbackQueryHandler(lambda u,c: set_next_step(u, c, 'awaiting_single_account', "Please send the session string."), pattern="^add_single$"))
        application.add_handler(CallbackQueryHandler(lambda u,c: set_next_step(u, c, 'awaiting_multiple_accounts', "Please send all session strings."), pattern="^add_multiple$"))
        application.add_handler(CallbackQueryHandler(lambda u,c: settings_command(u,c), pattern="^main_settings$"))
        application.add_handler(CallbackQueryHandler(pause_notifications_callback, pattern="^pause_notify_"))
        application.add_handler(CallbackQueryHandler(add_generated_session_callback, pattern="^add_generated_session:"))

        # The text handler that processes replies based on the state
        application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_input))

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
