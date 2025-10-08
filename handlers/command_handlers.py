import logging

from telegram import Update
from telegram.ext import ContextTypes
from telegram.constants import ParseMode

from utils import owner_only
from database import accounts_collection
from config import active_userbots
from userbot_manager import start_all_userbots

logger = logging.getLogger(__name__)

@owner_only
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Shows the main welcome message and command list."""
    text = (
        "👋 **Welcome to your Advanced Account Manager Bot!**\n\n"
        "This bot helps you manage multiple Telegram user accounts efficiently.\n\n"
        "**Account Management**\n"
        "`/add` — Interactively add a new account.\n"
        "`/add_string` — Add an account with a session string.\n"
        "`/accs` — List all your managed accounts.\n"
        "`/remove` — Remove an account.\n\n"
        "**Account Actions**\n"
        "`/sessions` — View and manage active sessions.\n\n"
        "**Bot Control**\n"
        "`/refresh` — Stop and restart all userbots from the database.\n"
        "`/ping` — Check bot latency."
    )
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN_V2)

@owner_only
async def accs_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Lists all managed accounts with their names and phone numbers."""
    accounts = list(accounts_collection.find().sort("custom_name", 1))
    if not accounts:
        await update.message.reply_text("You haven't added any accounts yet. Use `/add` to start.")
        return

    text = "You own the following accounts:\n\n"
    for acc in accounts:
        phone = acc.get('phone_number', 'N/A')
        # Escape markdown characters in custom name for safety
        safe_name = acc['custom_name'].replace('_', '\\_').replace('*', '\\*').replace('[', '\\[').replace(']', '\\]')
        text += f"▪️ **{safe_name}**: `+{phone}`\n"

    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN_V2)

@owner_only
async def ping_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Checks bot latency."""
    start_time = context.application.create_task(asyncio.sleep(0)).created_at
    message = await update.message.reply_text("Pinging...")
    end_time = context.application.create_task(asyncio.sleep(0)).created_at
    latency = (end_time - start_time).total_seconds() * 1000
    await message.edit_text(f"🏓 Pong!\nLatency: {latency:.2f} ms")

@owner_only
async def refresh_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Stops all running userbots and restarts them from the database."""
    msg = await update.message.reply_text("🔄 Stopping all active userbots...")
    
    # Create a copy of the items to avoid runtime errors during iteration
    active_bots_copy = list(active_userbots.items())
    
    for user_id, data in active_bots_copy:
        try:
            await data["client"].stop()
            logger.info(f"Stopped userbot {data['custom_name']} ({user_id}).")
        except Exception as e:
            logger.error(f"Error stopping userbot {data['custom_name']}: {e}")
        
    active_userbots.clear() # Clear the dictionary
    
    await msg.edit_text("🔄 All userbots stopped. Restarting from database...")
    await start_all_userbots(context.application)
    await msg.edit_text(f"✅ Refresh complete. {len(active_userbots)} userbots are now running.")
