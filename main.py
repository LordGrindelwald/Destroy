# ██████╗ ██╗   ██╗███████╗██████╗  ██████╗ ████████╗
# ██╔══██╗██║   ██║██╔════╝██╔══██╗██╔═══██╗╚══██╔══╝
# ██████╔╝██║   ██║█████╗  ██████╔╝██║   ██║   ██║
# ██╔══██╗██║   ██║██╔══╝  ██╔══██╗██║   ██║   ██║
# ██████╔╝╚██████╔╝███████╗██║  ██║╚██████╔╝   ██║
# ╚═════╝  ╚═════╝ ╚══════╝╚═╝  ╚═╝ ╚═════╝    ╚═╝
#
#           Userbot Forwarder Management Bot
#          (Worker Version - No Web Server)

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
    """Handles forwarding and notifies the bot owner, with priority on forwarding."""
    if client.me.id in paused_notifications:
        return

    source_chat_id = await get_source_chat()
    if message.chat.id == source_chat_id:
        try:
            target_chat = await get_target_chat()
            is_forwarding_paused = client.me.id in paused_forwarding

            # Step 1 (Priority): Forward the message, unless paused.
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

            # Step 2: Notify the owner.
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
    """Initializes and starts a single Pyrogram userbot client."""
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
            logger.info(f"Started userbot from session: {user_e.me.first_name} (@{userbot.me.username})")

        return "success", userbot.me
    
    except (AuthKeyUnregistered, UserDeactivated, AuthKeyDuplicated):
        return "invalid_session", None
    except Exception as e:
        logger.error(f"An unexpected error occurred while starting userbot: {e}")
        return "error", None

async def start_all_userbots_from_db(context: ContextTypes.DEFAULT_TYPE, update_info: bool = False):
    """On bot startup or refresh, load all accounts from DB and start them."""
    all_accounts = list(accounts_collection.find())
    count = 0
    for account in all_accounts:
        logger.info(f"Initializing userbot for user ID {account['user_id']} from database...")
        status, _ = await start_userbot(account["session_string"], context.application, update_info=update_info)
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

# ... All other handlers (accounts_menu, ask_for_source, set_target, add_single_account, ping_command, etc.) remain exactly the same ...
# ... I've omitted them for brevity but they should be here in your final file ...
# ... (The full code from the previous response can be used, just with the new main() function below) ...

# --- Placeholder for the omitted handlers ---
async def accounts_menu(update: Update, context: ContextTypes.DEFAULT_TYPE): return SELECTING_ACTION
async def ask_for_source(update: Update, context: ContextTypes.DEFAULT_TYPE): return SET_SOURCE
async def set_source(update: Update, context: ContextTypes.DEFAULT_TYPE): return ConversationHandler.END
async def ask_for_target(update: Update, context: ContextTypes.DEFAULT_TYPE): return SET_TARGET
async def set_target(update: Update, context: ContextTypes.DEFAULT_TYPE): return ConversationHandler.END
async def ask_for_single_account(update: Update, context: ContextTypes.DEFAULT_TYPE): return ADD_SINGLE_ACCOUNT
async def add_single_account(update: Update, context: ContextTypes.DEFAULT_TYPE): return ConversationHandler.END
async def ask_for_multiple_accounts(update: Update, context: ContextTypes.DEFAULT_TYPE): return ADD_MULTIPLE_ACCOUNTS
async def add_multiple_accounts(update: Update, context: ContextTypes.DEFAULT_TYPE): return ConversationHandler.END
async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE): return ConversationHandler.END
async def back_to_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE): return SELECTING_ACTION
async def ping_command(update: Update, context: ContextTypes.DEFAULT_TYPE): pass
async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE): pass
async def refresh_command(update: Update, context: ContextTypes.DEFAULT_TYPE): pass
async def unpause_forwarding_job(context: ContextTypes.DEFAULT_TYPE): pass
async def unpause_notifications_job(context: ContextTypes.DEFAULT_TYPE): pass
async def temp_pause_command(update: Update, context: ContextTypes.DEFAULT_TYPE): pass
async def temp_fwd_command(update: Update, context: ContextTypes.DEFAULT_TYPE): pass
async def pause_notifications_callback(update: Update, context: ContextTypes.DEFAULT_TYPE): pass
# --- End of placeholder section. Ensure you have the full functions from the previous answer. ---

# SIMPLIFIED: The main function no longer needs to run a web server.
def main():
    """Start the bot."""
    application = Application.builder().token(BOT_TOKEN).build()
    
    # This job runs on startup to initialize userbots from the database.
    application.job_queue.run_once(lambda ctx: asyncio.create_task(start_all_userbots_from_db(ctx)), 5)

    # The ConversationHandler for the UI remains the same.
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

    # Add all handlers to the application.
    application.add_handler(conv_handler)
    application.add_handler(CommandHandler("ping", lambda u, c: owner_only(u, c, ping_command)))
    application.add_handler(CommandHandler("status", lambda u, c: owner_only(u, c, status_command)))
    application.add_handler(CommandHandler("refresh", lambda u, c: owner_only(u, c, refresh_command)))
    application.add_handler(CommandHandler("temp", lambda u, c: owner_only(u, c, temp_pause_command)))
    application.add_handler(CommandHandler("temp_fwd", lambda u, c: owner_only(u, c, temp_fwd_command)))
    application.add_handler(CallbackQueryHandler(pause_notifications_callback, pattern="^pause_notify_"))

    logger.info("Management bot is starting as a background worker...")
    
    # This starts the bot and blocks until you stop it.
    application.run_polling()

if __name__ == "__main__":
    main()

