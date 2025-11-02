import asyncio
from pyrogram import Client as PyrogramClient
from pyrogram.errors import SessionPasswordNeeded
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    CommandHandler,
    CallbackQueryHandler,
    ConversationHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
from telegram.constants import ParseMode

# Import from our own modules
from config import API_ID, API_HASH, OWNER_ID, logger, PHONE, CODE, PASSWORD, ADD_ACCOUNT
from utils import owner_only, generate_device_name, escape_html
from userbot_logic import start_userbot # To add the account after generation

@owner_only
async def generate_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message or update.callback_query.message
    await message.reply_text("Starting session generator...\nPlease send the phone number in international format (e.g., +1234567890).")
    return PHONE

@owner_only
async def get_phone_number(update: Update, context: ContextTypes.DEFAULT_TYPE):
    phone = update.message.text
    msg = await update.message.reply_text("⏳ Connecting to Telegram...")
    client = PyrogramClient(
        name=f"userbot_{random.randint(1000, 9999)}", api_id=API_ID, api_hash=API_HASH, in_memory=True,
        device_model=generate_device_name(), system_version="Telegram Desktop 4.8.3", app_version="4.8.3", lang_code="en"
    )
    try:
        await asyncio.wait_for(client.connect(), timeout=30.0)
    except Exception as e:
        await msg.edit_text(f"❌ <b>Error:</b> <code>{escape_html(str(e))}</code>\nCancelled.", parse_mode=ParseMode.HTML)
        return ConversationHandler.END
    try:
        sent_code = await client.send_code(phone)
        context.user_data.update({'phone': phone, 'phone_code_hash': sent_code.phone_code_hash, 'temp_client': client})
        await msg.edit_text("A login code has been sent to your Telegram account. Please send it here.")
        return CODE
    except Exception as e:
        await msg.edit_text(f"❌ <b>Error:</b> <code>{escape_html(str(e))}</code>. Cancelled.", parse_mode=ParseMode.HTML)
        if client.is_connected: await client.disconnect()
        return ConversationHandler.END

@owner_only
async def get_login_code(update: Update, context: ContextTypes.DEFAULT_TYPE):
    code, client = update.message.text, context.user_data['temp_client']
    phone, phone_code_hash = context.user_data['phone'], context.user_data['phone_code_hash']
    msg = await update.message.reply_text("⏳ Signing in...")
    try:
        await client.sign_in(phone, phone_code_hash, code)
        session_string = await client.export_session_string()
        context.user_data['session_string'] = session_string
        keyboard = [[InlineKeyboardButton("Add Account", callback_data="add_account")]]
        await msg.reply_html(f"✅ Session generated successfully!\n\n<code>{session_string}</code>", reply_markup=InlineKeyboardMarkup(keyboard))
        await update.message.delete()
        return ADD_ACCOUNT
    except SessionPasswordNeeded:
        await msg.edit_text("2FA is enabled. Please send your password.")
        return PASSWORD
    except Exception as e:
        await msg.edit_text(f"❌ <b>Error:</b> <code>{escape_html(str(e))}</code>. Cancelled.", parse_mode=ParseMode.HTML)
        if client.is_connected: await client.disconnect()
        return ConversationHandler.END

@owner_only
async def get_2fa_password(update: Update, context: ContextTypes.DEFAULT_TYPE):
    password, client = update.message.text, context.user_data['temp_client']
    msg = await update.message.reply_text("⏳ Checking password...")
    try:
        await client.check_password(password)
        session_string = await client.export_session_string()
        context.user_data['session_string'] = session_string
        keyboard = [[InlineKeyboardButton("Add Account", callback_data="add_account")]]
        await msg.reply_html(f"✅ Session generated successfully!\n\n<code>{session_string}</code>", reply_markup=InlineKeyboardMarkup(keyboard))
        await update.message.delete()
        return ADD_ACCOUNT
    except Exception as e:
        await msg.edit_text(f"❌ <b>Error:</b> <code>{escape_html(str(e))}</code>. Cancelled.", parse_mode=ParseMode.HTML)
        if client.is_connected: await client.disconnect()
        return ConversationHandler.END

@owner_only
async def add_account_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    session_string = context.user_data.get('session_string')
    if not session_string:
        await query.answer("Session string not found. Please generate again.", show_alert=True)
        return ConversationHandler.END

    status, user_info, detail = await start_userbot(session_string, context.application, update_info=True)
    if status == "success":
        await query.edit_message_text(f"✅ Account added: {escape_html(user_info.first_name)}", parse_mode=ParseMode.HTML)
    else:
        await query.edit_message_text(f"⚠️ Error adding account: {detail}", parse_mode=ParseMode.HTML)
    
    context.user_data.clear()
    return ConversationHandler.END

@owner_only
async def cancel_command_conv(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancels the session generation conversation."""
    if 'temp_client' in context.user_data:
        client = context.user_data.get('temp_client')
        if client and client.is_connected: await client.disconnect()
    context.user_data.clear()
    await update.message.reply_text("Action cancelled.")
    return ConversationHandler.END

# --- Define the ConversationHandler ---
gen_conv = ConversationHandler(
    entry_points=[CommandHandler("generate", generate_command), CallbackQueryHandler(generate_command, pattern="^call_generate$")],
    states={
        PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_phone_number)],
        CODE: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_login_code)],
        PASSWORD: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_2fa_password)],
        ADD_ACCOUNT: [CallbackQueryHandler(add_account_callback, pattern="^add_account$")]
    },
    fallbacks=[CommandHandler("cancel", cancel_command_conv)],
    conversation_timeout=300,
)
