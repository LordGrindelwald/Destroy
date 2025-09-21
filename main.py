# ██████╗ ██╗   ██╗███████╗██████╗  ██████╗ ████████╗
# ██╔══██╗██║   ██║██╔════╝██╔══██╗██╔═══██╗╚══██╔══╝
# ██████╔╝██║   ██║█████╗  ██████╔╝██║   ██║   ██║
# ██╔══██╗██║   ██║██╔══╝  ██╔══██╗██║   ██║   ██║
# ██████╔╝╚██████╔╝███████╗██║  ██║╚██████╔╝   ██║
# ╚═════╝  ╚═════╝ ╚══════╝╚═╝  ╚═╝ ╚═════╝    ╚═╝
#
#           Userbot Forwarder Management Bot
#          (Final Version with Leader Election)

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

# --- ConversationHandler States ---
(
    SELECTING_ACTION,
    SET_SOURCE,
    ADD_SINGLE_ACCOUNT,
    ADD_MULTIPLE_ACCOUNTS,
    SET_TARGET,
) = range(5)


# --- Userbot Core Logic ---
async def get_source_chat():
    config = config_collection.find_one({"_id": "config"})
    return config.get("source_chat_id", 777000)

async def get_target_chat():
    config = config_collection.find_one({"_id": "config"})
    return config.get("target_chat_username")

async def forwarder_handler(client: PyrogramClient, message: Message, ptb_app: Application):
    if client.me.id in paused_notifications:
        return

    source_chat_id = await get_source_chat()
    if message.chat.id == source_chat_id:
        try:
            target_chat = await get_target_chat()
            is_forwarding_paused = client.me.id in paused_forwarding

            if not is_forwarding_paused:
                if target_chat:
                    await client.forward_messages(chat_id=target_chat, from_chat_id=message.chat.id, message_ids=message.id)
                    logger.info(f"Userbot {client.me.id} forwarded message {message.id} to {target_chat}.")
                    status_text = "✅ Forwarded"
                else:
                    status_text = "⚠️ Not Forwarded (No Target Set)"
            else:
                logger.info(f"Forwarding is paused for {client.me.id}. Skipping forward.")
                status_text = "⏸️ Paused (Forwarding Only)"

            content = message.text or message.caption or "(Media without caption)"
            header = f"👤 **{client.me.first_name}**"
            notification_text = (
                f"{header}\n"
                f"**Status:** {status_text}\n\n"
                f"**Content:**\n`{content[:3000]}`"
            )
            await ptb_app.bot.send_message(OWNER_ID, notification_text, parse_mode=ParseMode.MARKDOWN_V2)

        except Exception as e:
            logger.error(f"Failed to process message {message.id} from {client.me.id}: {e}")
            await ptb_app.bot.send_message(OWNER_ID, f"Error processing message: `{e}`", parse_mode=ParseMode.MARKDOWN_V2)

async def start_userbot(session_string: str, ptb_app: Application, update_info: bool = False):
    try:
        userbot = PyrogramClient(
            name=f"userbot_{len(active_userbots)}", api_id=API_ID, api_hash=API_HASH, session_string=session_string, in_memory=True
        )
        await userbot.start()

        if userbot.me.id in active_userbots:
            await userbot.stop()
            return "already_exists", None

        handler_with_context = partial(forwarder_handler, ptb_app=ptb_app)
        userbot.add_handler(MessageHandler(handler_with_context))
        
        active_userbots[userbot.me.id] = {"client": userbot, "task": asyncio.current_task()}
        
        if update_info:
            account_info = {
                "user_id": userbot.me.id, "first_name": userbot.me.first_name, "username": userbot.me.username,
                "phone_number": userbot.me.phone_number, "session_string": session_string,
            }
            accounts_collection.update_one({"user_id": userbot.me.id}, {"$set": account_info}, upsert=True)
            logger.info(f"Refreshed and started userbot: {userbot.me.first_name} (@{userbot.me.username})")
        else:
            logger.info(f"Started userbot from session: {userbot.me.first_name} (@{userbot.me.username})")

        return "success", userbot.me
    
    except (AuthKeyUnregistered, UserDeactivated, AuthKeyDuplicated):
        return "invalid_session", None
    except Exception as e:
        logger.error(f"An unexpected error occurred while starting userbot: {e}")
        return "error", None

async def start_all_userbots_from_db(application: Application, update_info: bool = False):
    all_accounts = list(accounts_collection.find())
    count = 0
    for account in all_accounts:
        logger.info(f"Initializing userbot for user ID {account['user_id']} from database...")
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
    if update.effective_user.id != OWNER_ID:
        await update.message.reply_text("⛔️ You are not authorized to use this bot.")
        return ConversationHandler.END

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
        f"🎯 Target Chat: <code>{target_chat}</code>",
        reply_markup=reply_markup,
    )
    return SELECTING_ACTION

async def accounts_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    accounts = list(accounts_collection.find())
    text = "<b>Your currently managed accounts:</b>\n\n" if accounts else "No accounts have been added yet."
    for acc in accounts:
        text += (
            f"👤 <b>Name:</b> {acc.get('first_name', 'N/A')}\n"
            f"   - <b>Username:</b> @{acc.get('username', 'N/A')}\n"
            f"   - <b>Phone:</b> +{acc.get('phone_number', 'N/A')}\n"
            f"   - <b>ID:</b> <code>{acc.get('user_id', 'N/A')}</code>\n"
            f"--------------------\n"
        )
    keyboard = [
        [InlineKeyboardButton("➕ Add Single Account", callback_data="add_single")],
        [InlineKeyboardButton("➕ Add Multiple Accounts", callback_data="add_multiple")],
        [InlineKeyboardButton("« Back to Main Menu", callback_data="main_menu")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=reply_markup)
    return SELECTING_ACTION

async def ask_for_source(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("Please send the new universal source chat ID.\nIt must be a numerical ID (e.g., -10012345678).")
    return SET_SOURCE

async def set_source(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        chat_id = int(update.message.text)
        config_collection.update_one({"_id": "config"}, {"$set": {"source_chat_id": chat_id}}, upsert=True)
        await update.message.reply_text(f"✅ Universal source chat ID has been updated to: {chat_id}")
    except ValueError:
        await update.message.reply_text("❌ Invalid ID. Please send a numerical chat ID.")
    
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
        await update.message.reply_text(f"✅ Universal target chat has been updated to: {username}")
    else:
        await update.message.reply_text("❌ Invalid username. It must start with '@'.")
    
    await start_command(update.message, context)
    return ConversationHandler.END

async def ask_for_single_account(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("Please send the session string for the account you want to add.")
    return ADD_SINGLE_ACCOUNT

async def add_single_account(update: Update, context: ContextTypes.DEFAULT_TYPE):
    session_string = update.message.text
    msg = await update.message.reply_text("⏳ Attempting to log in and start the userbot...")
    status, user_info = await start_userbot(session_string, context.application, update_info=True)
    if status == "success":
        await msg.edit_text(f"✅ Successfully added and started: {user_info.first_name} (@{user_info.username})")
    elif status == "already_exists":
        await msg.edit_text("⚠️ This account is already being managed.")
    elif status == "invalid_session":
        await msg.edit_text("❌ Authentication failed. The session string is invalid or has expired.")
    else:
        await msg.edit_text("❌ An unexpected error occurred. Please check the logs.")
    await start_command(update.message, context)
    return ConversationHandler.END

async def ask_for_multiple_accounts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("Please send all the session strings you want to add.\nSeparate each string with a space, comma, or a new line.")
    return ADD_MULTIPLE_ACCOUNTS

async def add_multiple_accounts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    session_strings = [s.strip() for s in text.replace(",", " ").replace("\n", " ").split() if s.strip()]
    
    if not session_strings:
        await update.message.reply_text("No session strings found in your message.")
        await start_command(update.message, context)
        return ConversationHandler.END

    msg = await update.message.reply_text(f"Found {len(session_strings)} session strings. Processing them now...")
    success_count = 0
    failure_count = 0
    for i, session in enumerate(session_strings):
        await msg.edit_text(f"⏳ Processing... ({i+1}/{len(session_strings)})")
        status, _ = await start_userbot(session, context.application, update_info=True)
        if status == "success":
            success_count += 1
        else:
            failure_count += 1
    await msg.edit_text(f"Batch process complete!\n\n✅ Successfully added: {success_count}\n❌ Failed or already exists: {failure_count}")
    await start_command(update.message, context)
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Operation cancelled.")
    await start_command(update.message, context)
    return ConversationHandler.END

async def back_to_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await start_command(query.message, context)
    return SELECTING_ACTION

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
        f"**Target Chat:** `{target_chat}`\n"
        f"**Userbots Running:** {running_bots}/{total_bots}\n"
        f"**Paused Forwarding:** {len(paused_forwarding)} bots\n"
        f"**Paused Notifications:** {len(paused_notifications)} bots"
    )
    await update.message.reply_text(status_text, parse_mode=ParseMode.MARKDOWN_V2)

async def refresh_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text("🔄 Stopping all userbots...")
    for user_id, data in list(active_userbots.items()):
        await data["client"].stop()
        del active_userbots[user_id]
    
    await msg.edit_text("🔄 Restarting and refreshing account details from the database...")
    started, total = await start_all_userbots_from_db(context.application, update_info=True)
    await msg.edit_text(f"✅ Refresh complete. Started {started}/{total} userbots.")

async def unpause_forwarding_job(context: ContextTypes.DEFAULT_TYPE):
    user_id = context.job.data["user_id"]
    if user_id in paused_forwarding:
        paused_forwarding.remove(user_id)
        logger.info(f"Auto-resumed forwarding for userbot {user_id}.")
        await context.bot.send_message(OWNER_ID, f"▶️ Forwarding automatically resumed for userbot `{user_id}`.", parse_mode=ParseMode.MARKDOWN_V2)

async def unpause_notifications_job(context: ContextTypes.DEFAULT_TYPE):
    user_id = context.job.data["user_id"]
    if user_id in paused_notifications:
        paused_notifications.remove(user_id)
        logger.info(f"Auto-resumed notifications for userbot {user_id}.")
        await context.bot.send_message(OWNER_ID, f"🔔 Notifications automatically resumed for userbot `{user_id}`.", parse_mode=ParseMode.MARKDOWN_V2)

async def temp_pause_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        user_id_to_pause = int(context.args[0])
        if user_id_to_pause not in active_userbots:
            await update.message.reply_text("❌ User ID not found among active userbots.")
            return

        paused_forwarding.add(user_id_to_pause)
        context.job_queue.run_once(unpause_forwarding_job, 300, data={"user_id": user_id_to_pause}, name=f"unpause_fwd_{user_id_to_pause}")
        
        keyboard = [[InlineKeyboardButton("🤫 Also Pause Notifications (5 min)", callback_data=f"pause_notify_{user_id_to_pause}")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await update.message.reply_text(
            f"⏸️ Forwarding has been paused for userbot `{user_id_to_pause}` for 5 minutes.\n\nYou will still receive notifications.",
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=reply_markup
        )
    except (IndexError, ValueError):
        await update.message.reply_text("Usage: `/temp <userbot_user_id>`")

async def temp_fwd_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        user_id_to_pause = int(context.args[0])
        if user_id_to_pause not in active_userbots:
            await update.message.reply_text("❌ User ID not found among active userbots.")
            return

        paused_forwarding.add(user_id_to_pause)
        paused_notifications.add(user_id_to_pause)
        context.job_queue.run_once(unpause_forwarding_job, 300, data={"user_id": user_id_to_pause}, name=f"unpause_fwd_{user_id_to_pause}")
        context.job_queue.run_once(unpause_notifications_job, 300, data={"user_id": user_id_to_pause}, name=f"unpause_notify_{user_id_to_pause}")
        
        await update.message.reply_text(
            f"🤫 Silent pause enabled. All activity for userbot `{user_id_to_pause}` is paused for 5 minutes.",
            parse_mode=ParseMode.MARKDOWN_V2
        )
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
    
    await query.edit_message_text(
        f"⏸️ Forwarding is paused.\n🤫 Notifications are now also paused for userbot `{user_id_to_pause}` for 5 minutes.",
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=None
    )


# --- Main Application Runner with Leader Election ---
async def run_bot_as_leader(application: Application):
    """The main logic for the leader process."""
    logger.info("👑 This process is the leader. Starting all services.")
    
    # Start the PTB application for polling
    await application.initialize()
    await application.start()
    await application.updater.start_polling()

    # Start the userbots from the database
    await start_all_userbots_from_db(application)
    
    # We need a never-ending task to keep the process alive
    while True:
        await asyncio.sleep(3600)

async def main():
    """Initializes and runs the bot using a leader election model."""
    host = "0.0.0.0"
    port = int(os.getenv("PORT", 8080))
    server = None
    
    try:
        # Try to bind to the health check port.
        server = await asyncio.start_server(lambda r, w: w.close(), host, port)
        logger.info(f"Health check port {port} acquired. This process will be the leader.")

        # If successful, this process is the leader.
        application = Application.builder().token(BOT_TOKEN).build()

        # Add all handlers
        conv_handler = ConversationHandler(
            entry_points=[CommandHandler("start", start_command)],
            states={
                SELECTING_ACTION: [
                    CallbackQueryHandler(accounts_menu, pattern="^manage_accounts$"),
                    CallbackQueryHandler(ask_for_source, pattern="^set_source$"),
                    CallbackQueryHandler(ask_for_target, pattern="^set_target$"),
                    CallbackQueryHandler(ask_for_single_account, pattern="^add_single$"),
                    CallbackQueryHandler(ask_for_multiple_accounts, pattern="^add_multiple$"),
                    CallbackQueryHandler(back_to_main_menu, pattern="^main_menu$"),
                ],
                SET_SOURCE: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_source)],
                SET_TARGET: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_target)],
                ADD_SINGLE_ACCOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_single_account)],
                ADD_MULTIPLE_ACCOUNTS: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_multiple_accounts)],
            },
            fallbacks=[CommandHandler("cancel", cancel)],
            persistent=False,
            name="main_conversation"
        )
        application.add_handler(conv_handler)
        application.add_handler(CommandHandler("ping", lambda u, c: owner_only(u, c, ping_command)))
        application.add_handler(CommandHandler("status", lambda u, c: owner_only(u, c, status_command)))
        application.add_handler(CommandHandler("refresh", lambda u, c: owner_only(u, c, refresh_command)))
        application.add_handler(CommandHandler("temp", lambda u, c: owner_only(u, c, temp_pause_command)))
        application.add_handler(CommandHandler("temp_fwd", lambda u, c: owner_only(u, c, temp_fwd_command)))
        application.add_handler(CallbackQueryHandler(pause_notifications_callback, pattern="^pause_notify_"))
        
        # Run the leader's tasks
        await run_bot_as_leader(application)
        
    except OSError:
        # If the port is already in use, this is a follower process.
        logger.info(f"Health check port {port} is already in use. This process will be a follower and stay idle.")
        # Keep the follower alive indefinitely without doing any work.
        while True:
            await asyncio.sleep(3600)
    finally:
        if server:
            server.close()
            await server.wait_closed()


if __name__ == "__main__":
    asyncio.run(main())
