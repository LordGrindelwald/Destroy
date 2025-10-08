import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, ConversationHandler
from telegram.constants import ParseMode
from pyrogram.raw.functions.account import GetAuthorizations, ResetAuthorization

from utils import owner_only
from config import (
    SELECT_ACCOUNT_SINGLE, TERMINATE_SESSION_CONFIRM, REMOVE_ACCOUNT_CONFIRM, active_userbots
)
from database import accounts_collection
from ui import build_account_selection_keyboard

logger = logging.getLogger(__name__)

# --- Menu Entry Point ---
@owner_only
async def menu_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Entry point for commands that need account selection."""
    command = update.message.text.split()[0][1:]
    context.user_data['action'] = command
    mode = 'single'  # All current menu commands are single select

    keyboard, text = await build_account_selection_keyboard(page=0, mode=mode, action=command)
    if not keyboard:
        await update.message.reply_text(text)
        return ConversationHandler.END

    await update.message.reply_text(text, reply_markup=keyboard)
    return SELECT_ACCOUNT_SINGLE

# --- Callback Handlers ---
async def change_page(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handles pagination in the selection menu."""
    query = update.callback_query
    await query.answer()

    _, mode, action, page_str = query.data.split('_', 3)
    page = int(page_str)

    keyboard, text = await build_account_selection_keyboard(page=page, mode=mode, action=action)
    await query.edit_message_text(text, reply_markup=keyboard)
    return SELECT_ACCOUNT_SINGLE

async def handle_single_selection(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Routes the user to the correct function after they select an account."""
    query = update.callback_query
    await query.answer()

    _, _, action, custom_name = query.data.split('_', 3)
    context.user_data['selected_account_name'] = custom_name
    account = accounts_collection.find_one({"custom_name": custom_name})

    if not account:
        await query.edit_message_text("❌ Error: Account not found. It might have been removed.")
        return ConversationHandler.END

    if action == "sessions":
        return await show_sessions(query, context, account)
    elif action == "remove":
        return await confirm_remove_account(query, context, account)

async def show_sessions(query, context, account) -> int:
    """Displays the active sessions for the selected account."""
    user_id = account.get("user_id")
    if not user_id or user_id not in active_userbots:
        await query.edit_message_text("This account is not currently online. Use `/refresh` to bring it online.")
        return ConversationHandler.END

    await query.edit_message_text("⏳ Fetching session data...")
    client_instance = active_userbots[user_id]["client"]

    try:
        sessions_result = await client_instance.invoke(GetAuthorizations())
        text = f"**Active Sessions for {account['custom_name']}**:\n\n"
        keyboard_buttons = []
        current_hash = next((s.hash for s in sessions_result.authorizations if s.current), 0)

        for session in sessions_result.authorizations:
            is_current = " (Current Session)" if session.hash == current_hash else ""
            text += (
                f"**Device:** {session.device_model}{is_current}\n"
                f"**Location:** {session.ip} ({session.country})\n"
                f"**App:** {session.app_name} v{session.app_version}\n"
                f"**Last Active:** {session.date_active.strftime('%Y-%m-%d %H:%M UTC')}\n"
                f"--------------------\n"
            )
            if not is_current:
                keyboard_buttons.append([InlineKeyboardButton(
                    f"Terminate {session.device_model} ({session.ip})",
                    callback_data=f"term_{user_id}_{session.hash}"
                )])

        keyboard_buttons.append([InlineKeyboardButton("Done", callback_data="cancel_selection")])
        await query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(keyboard_buttons))
        return TERMINATE_SESSION_CONFIRM
    except Exception as e:
        logger.error(f"Error fetching sessions for {account['custom_name']}: {e}")
        await query.edit_message_text(f"❌ Error fetching sessions: {e}")
        return ConversationHandler.END

async def terminate_session(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Terminates a specific session."""
    query = update.callback_query
    await query.answer("Terminating...", show_alert=True)

    _, user_id_str, hash_str = query.data.split('_')
    user_id, session_hash = int(user_id_str), int(hash_str)

    if user_id not in active_userbots:
        await query.edit_message_text("This account is no longer online. Refresh and try again.")
        return ConversationHandler.END

    client_instance = active_userbots[user_id]["client"]
    try:
        await client_instance.invoke(ResetAuthorization(hash=session_hash))
        await query.edit_message_text("✅ Session terminated successfully. You can now close this message.")
    except Exception as e:
        logger.error(f"Error terminating session: {e}")
        await query.edit_message_text(f"❌ Failed to terminate session: {e}")

    context.user_data.clear()
    return ConversationHandler.END

async def confirm_remove_account(query, context, account) -> int:
    """Asks for confirmation before removing an account."""
    text = (f"Are you absolutely sure you want to remove the account **{account['custom_name']}**?\n\n"
            "This will stop the userbot and permanently delete its session from the database. This action cannot be undone.")
    keyboard = [
        [InlineKeyboardButton("Yes, Remove It", callback_data=f"remove_confirm_yes")],
        [InlineKeyboardButton("No, Cancel", callback_data="cancel_selection")]
    ]
    await query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(keyboard))
    return REMOVE_ACCOUNT_CONFIRM

async def do_remove_account(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Performs the actual removal of the account."""
    query = update.callback_query
    await query.answer("Removing account...")
    
    custom_name = context.user_data.get('selected_account_name')
    if not custom_name:
        await query.edit_message_text("Error: Could not identify account to remove. Please start over.")
        return ConversationHandler.END
        
    account = accounts_collection.find_one({"custom_name": custom_name})
    if not account:
        await query.edit_message_text("Account already removed.")
        return ConversationHandler.END

    user_id = account.get("user_id")
    if user_id and user_id in active_userbots:
        try:
            await active_userbots[user_id]["client"].stop()
            del active_userbots[user_id]
            logger.info(f"Stopped userbot {custom_name} for removal.")
        except Exception as e:
            logger.error(f"Error stopping userbot {custom_name} during removal: {e}")
            
    accounts_collection.delete_one({"custom_name": custom_name})
    await query.edit_message_text(f"✅ Account **{custom_name}** has been successfully removed.")
    context.user_data.clear()
    return ConversationHandler.END

async def cancel_selection(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Cancels the current menu operation."""
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("Operation cancelled.")
    context.user_data.clear()
    return ConversationHandler.END
