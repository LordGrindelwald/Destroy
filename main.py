# ██████╗ ██╗   ██╗███████╗██████╗  ██████╗ ████████╗
# ██╔══██╗██║   ██║██╔════╝██╔══██╗██╔═══██╗╚══██╔══╝
# ██████╔╝██║   ██║█████╗  ██████╔╝██║   ██║   ██║
# ██╔══██╗██║   ██║██╔══╝  ██╔══██╗██║   ██║   ██║
# ██████╔╝╚██████╔╝███████╗██║  ██║╚██████╔╝   ██║
# ╚═════╝  ╚═════╝ ╚══════╝╚═╝  ╚═╝ ╚═════╝    ╚═╝
#
#           Userbot Forwarder Management Bot
#          (Final Version with All Fixes)

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

def escape_markdown_v2(text: str) -> str:
    """Helper function to escape text for Telegram MarkdownV2."""
    if not isinstance(text, str):
        text = str(text)
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
                else:
                    status_text = "⚠️ Not Forwarded (No Target Set)"
            else:
                status_text = "⏸️ Paused (Forwarding Only)"
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

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    keyboard = [
        [InlineKeyboardButton("📚 Set Source", callback_data="set_source")],
        [InlineKeyboardButton("🎯 Set Target", callback_data="set_target")],
        [InlineKeyboardButton("👤 Manage Accounts", callback_data="manage_accounts")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    source_chat_id = await get_source_chat()
    target_chat = await get_target_chat() or "Not Set"
    message_text = (
        f"👋 **Welcome\!**\n\n"
        f"▶️ **Source:** `{source_chat_id}`\n"
        f"🎯 **Target:** `{escape_markdown_v2(target_chat)}`"
    )
    if update.callback_query:
        await update.callback_query.edit_message_text(message_text, parse_mode=ParseMode.MARKDOWN_V2, reply_markup=reply_markup)
    else:
        await update.message.reply_markdown_v2(message_text, reply_markup=reply_markup)

async def accounts_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    accounts = list(accounts_collection.find())
    text = "👤 **Your Managed Accounts:**\n\n" if accounts else "No accounts have been added yet."
    for acc in accounts:
        first_name = escape_markdown_v2(acc.get('first_name', 'N/A'))
        separator = '-'*20
        text += f"**Name:** {first_name}\n**ID:** `{acc.get('user_id', 'N/A')}`\n{separator}\n"
    keyboard = [
        [InlineKeyboardButton("➕ Add Single", callback_data="add_single"), InlineKeyboardButton("➕ Add Multiple", callback_data="add_multiple")],
        [InlineKeyboardButton("« Back", callback_data="main_menu")],
    ]
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
    del context.user_data['next_step']

    if step == 'awaiting_source':
        try:
            chat_id = int(update.message.text)
            config_collection.update_one({"_id": "config"}, {"$set": {"source_chat_id": chat_id}}, upsert=True)
            await update.message.reply_text(f"✅ Source chat updated to: {chat_id}")
        except ValueError:
            await update.message.reply_text("❌ Invalid ID.")
        await start_command(update, context)

    elif step == 'awaiting_target':
        username = update.message.text.strip()
        if username.startswith("@") and len(username) > 4:
            config_collection.update_one({"_id": "config"}, {"$set": {"target_chat_username": username}}, upsert=True)
            await update.message.reply_text(f"✅ Target chat updated to: {username}")
        else:
            await update.message.reply_text("❌ Invalid username.")
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
    if 'next_step' in context.user_data:
        del context.user_data['next_step']
    await update.message.reply_text("Action cancelled.")
    await start_command(update, context)

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
    status_text = (
        f"📊 **Bot Status**\n\n"
        f"**Management Bot:** Online\n"
        f"**Source Chat:** `{source_chat}`\n"
        f"**Target Chat:** `{escape_markdown_v2(target_chat)}`\n"
        f"**Userbots Running:** {running_bots}/{total_bots}\n"
        f"**Paused Forwarding:** {len(paused_forwarding)} bots\n"
        f"**Paused Notifications:** {len(paused_notifications)} bots"
    )
    await update.message.reply_markdown_v2(status_text)

async def refresh_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text("🔄 Stopping all userbots...")
    for user_id, data in list(active_userbots.items()):
        await data["client"].stop()
        del active_userbots[user_id]
    await msg.edit_text("🔄 Restarting and refreshing account details...")
    started, total = await start_all_userbots_from_db(context.application, update_info=True)
    await msg.edit_text(f"✅ Refresh complete. Started {started}/{total} userbots.")

async def unpause_forwarding_job(context: ContextTypes.DEFAULT_TYPE):
    user_id = context.job.data["user_id"]
    if user_id in paused_forwarding:
        paused_forwarding.remove(user_id)
        await context.bot.send_message(OWNER_ID, f"▶️ Forwarding automatically resumed for `{user_id}`.", parse_mode=ParseMode.MARKDOWN_V2)

async def unpause_notifications_job(context: ContextTypes.DEFAULT_TYPE):
    user_id = context.job.data["user_id"]
    if user_id in paused_notifications:
        paused_notifications.remove(user_id)
        await context.bot.send_message(OWNER_ID, f"🔔 Notifications automatically resumed for `{user_id}`.", parse_mode=ParseMode.MARKDOWN_V2)

async def temp_pause_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        user_id_to_pause = int(context.args[0])
        if user_id_to_pause not in active_userbots:
            await update.message.reply_text("❌ User ID not found.")
            return
        paused_forwarding.add(user_id_to_pause)
        context.job_queue.run_once(unpause_forwarding_job, 300, data={"user_id": user_id_to_pause}, name=f"unpause_fwd_{user_id_to_pause}")
        keyboard = [[InlineKeyboardButton("🤫 Also Pause Notifications (5 min)", callback_data=f"pause_notify_{user_id_to_pause}")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text(f"⏸️ Forwarding paused for `{user_id_to_pause}` for 5 minutes.", parse_mode=ParseMode.MARKDOWN_V2, reply_markup=reply_markup)
    except (IndexError, ValueError):
        await update.message.reply_text("Usage: `/temp <userbot_user_id>`")

async def temp_fwd_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        user_id_to_pause = int(context.args[0])
        if user_id_to_pause not in active_userbots:
            await update.message.reply_text("❌ User ID not found.")
            return
        paused_forwarding.add(user_id_to_pause)
        paused_notifications.add(user_id_to_pause)
        context.job_queue.run_once(unpause_forwarding_job, 300, data={"user_id": user_id_to_pause}, name=f"unpause_fwd_{user_id_to_pause}")
        context.job_queue.run_once(unpause_notifications_job, 300, data={"user_id": user_id_to_pause}, name=f"unpause_notify_{user_id_to_pause}")
        await update.message.reply_text(f"🤫 Silent pause enabled for `{user_id_to_pause}` for 5 minutes.", parse_mode=ParseMode.MARKDOWN_V2)
    except (IndexError, ValueError):
        await update.message.reply_text("Usage: `/temp_fwd <userbot_user_id>`")

async def pause_notifications_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id_to_pause = int(query.data.split("_")[2])
    if user_id_to_pause not in active_userbots:
        await query.edit_message_text("This userbot is no longer active.", reply_markup=None)
        return
    paused_notifications.add(user_id_to_pause)
    context.job_queue.run_once(unpause_notifications_job, 300, data={"user_id": user_id_to_pause}, name=f"unpause_notify_{user_id_to_pause}")
    await query.edit_message_text(f"🤫 Notifications now also paused for `{user_id_to_pause}` for 5 minutes.", parse_mode=ParseMode.MARKDOWN_V2, reply_markup=None)

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
        application.add_handler(CommandHandler("start", lambda u, c: owner_only(u, c, start_command)))
        application.add_handler(CommandHandler("cancel", lambda u, c: owner_only(u, c, cancel_command)))
        application.add_handler(CommandHandler("ping", ping_command))
        application.add_handler(CommandHandler("status", lambda u, c: owner_only(u, c, status_command)))
        application.add_handler(CommandHandler("refresh", lambda u, c: owner_only(u, c, refresh_command)))
        application.add_handler(CommandHandler("temp", lambda u, c: owner_only(u, c, temp_pause_command)))
        application.add_handler(CommandHandler("temp_fwd", lambda u, c: owner_only(u, c, temp_fwd_command)))

        # Callback handlers for buttons
        application.add_handler(CallbackQueryHandler(lambda u,c: set_next_step(u, c, 'awaiting_source', "Please send the source chat ID."), pattern="^set_source$"))
        application.add_handler(CallbackQueryHandler(lambda u,c: set_next_step(u, c, 'awaiting_target', "Please send the target bot username."), pattern="^set_target$"))
        application.add_handler(CallbackQueryHandler(accounts_menu, pattern="^manage_accounts$"))
        application.add_handler(CallbackQueryHandler(lambda u,c: set_next_step(u, c, 'awaiting_single_account', "Please send the session string."), pattern="^add_single$"))
        application.add_handler(CallbackQueryHandler(lambda u,c: set_next_step(u, c, 'awaiting_multiple_accounts', "Please send all session strings."), pattern="^add_multiple$"))
        application.add_handler(CallbackQueryHandler(lambda u,c: start_command(u,c), pattern="^main_menu$"))
        application.add_handler(CallbackQueryHandler(pause_notifications_callback, pattern="^pause_notify_"))

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
