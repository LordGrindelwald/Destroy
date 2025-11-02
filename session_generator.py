import asyncio
import random
# --- MODIFIED: Imports are from pyrogram, as per your documentation ---
from pyrogram import Client
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
from config import (
    accounts_collection, logger, 
    UNIQUE_NAME_GEN, PHONE, CODE, PASSWORD,
    TD_API_ID, TD_API_HASH, TD_SYSTEM_VERSION, 
    TD_APP_VERSION, TD_LANG_CODE, 
    TD_SYSTEM_LANG_CODE, TD_LANG_PACK
)
from utils import owner_only, generate_device_name, escape_html
from userbot_logic import start_userbot # To add the account after generation

@owner_only
async def generate_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Entry point for session generation. Asks for unique name."""
    message = update.message or update.callback_query.message
    
    if update.callback_query:
        await update.callback_query.answer()
        
    await message.reply_text("Please send a unique name (e.g., 'work_acct') for this new account. Send /cancel to stop.")
    return UNIQUE_NAME_GEN

@owner_only
async def get_unique_name_for_generate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Saves unique name and asks for phone number."""
    unique_name = update.message.text.strip().split()[0] # Take first word
    
    if accounts_collection.find_one({"unique_name": unique_name}):
        await update.message.reply_text("That name is already taken. Please choose another one.")
        return UNIQUE_NAME_GEN # Stay in this state

    context.user_data['unique_name'] = unique_name
    await update.message.reply_text("Great. Now please send the phone number in international format (e.g., +1234567890).")
    return PHONE

@owner_only
async def get_phone_number(update: Update, context: ContextTypes.DEFAULT_TYPE):
    phone = update.message.text
    msg = await update.message.reply_text("⏳ Connecting to Telegram...")
    
    client = Client(
        name=f"userbot_{random.randint(1000, 9999)}",
        api_id=TD_API_ID,
        api_hash=TD_API_HASH,
        in_memory=True,
        device_model=generate_device_name(), # Use random device name
        system_version=TD_SYSTEM_VERSION,
        app_version=TD_APP_VERSION,
        lang_code=TD_LANG_CODE,
        system_lang_code=TD_SYSTEM_LANG_CODE,
        lang_pack=TD_LANG_PACK
    )
    try:
        await asyncio.wait_for(client.connect(), timeout=30.0)
    except Exception as e:
        await msg.edit_text(f"❌ <b>Error:</b> <code>{escape_html(str(e))}</code>\nCancelled.", parse_mode=ParseMode.HTML)
        context.user_data.clear()
        return ConversationHandler.END
    try:
        sent_code = await client.send_code(phone)
        context.user_data.update({'phone': phone, 'phone_code_hash': sent_code.phone_code_hash, 'temp_client': client})
        await msg.edit_text("A login code has been sent to your Telegram account. Please send it here.")
        return CODE
    except Exception as e:
        await msg.edit_text(f"❌ <b>Error:</b> <code>{escape_html(str(e))}</code>. Cancelled.", parse_mode=ParseMode.HTML)
        if client.is_connected: await client.disconnect()
        context.user_data.clear()
        return ConversationHandler.END

@owner_only
async def get_login_code(update: Update, context: ContextTypes.DEFAULT_TYPE):
    code, client = update.message.text, context.user_data['temp_client']
    phone, phone_code_hash = context.user_data['phone'], context.user_data['phone_code_hash']
    unique_name = context.user_data.get('unique_name')
    
    msg = await update.message.reply_text("⏳ Signing in...")
    try:
        await client.sign_in(phone, phone_code_hash, code)
        
        await msg.edit_text("✅ Signed in! Generating session and adding account...")
        session_string = await client.export_session_string()
        
        status, user_info, detail = await start_userbot(
            session_string, context.application, update_info=True, unique_name=unique_name
        )
        
        if status == "success":
            await msg.edit_text(f"✅ Account <code>{escape_html(user_info.first_name)}</code> (<code>{escape_html(unique_name)}</code>) added successfully!", parse_mode=ParseMode.HTML)
        else:
            await msg.edit_text(f"⚠️ Error adding account: {detail}\n\nSession string (for manual retry):\n<code>{session_string}</code>", parse_mode=ParseMode.HTML)
        
        await update.message.delete()
        context.user_data.clear()
        return ConversationHandler.END

    except SessionPasswordNeeded:
        await msg.edit_text("2FA is enabled. Please send your password.")
        return PASSWORD
    except Exception as e:
        await msg.edit_text(f"❌ <b>Error:</b> <code>{escape_html(str(e))}</code>. Cancelled.", parse_mode=ParseMode.HTML)
        if client.is_connected: await client.disconnect()
        context.user_data.clear()
        return ConversationHandler.END

@owner_only
async def get_2fa_password(update: Update, context: ContextTypes.DEFAULT_TYPE):
    password, client = update.message.text, context.user_data['temp_client']
    unique_name = context.user_data.get('unique_name')
    
    msg = await update.message.reply_text("⏳ Checking password...")
    try:
        await client.check_password(password)

        await msg.edit_text("✅ Password correct! Generating session and adding account...")
        session_string = await client.export_session_string()
        
        status, user_info, detail = await start_userbot(
            session_string, context.application, update_info=True, unique_name=unique_name
        )
        
        if status == "success":
            await msg.edit_text(f"✅ Account <code>{escape_html(user_info.first_name)}</code> (<code>{escape_html(unique_name)}</code>) added successfully!", parse_mode=ParseMode.HTML)
        else:
            await msg.edit_text(f"⚠️ Error adding account: {detail}\n\nSession string (for manual retry):\n<code>{session_string}</code>", parse_mode=ParseMode.HTML)

        await update.message.delete()
        context.user_data.clear()
        return ConversationHandler.END
        
    except Exception as e:
        await msg.edit_text(f"❌ <b>Error:</b> <code>{escape_html(str(e))}</code>. Cancelled.", parse_mode=ParseMode.HTML)
        if client.is_connected: await client.disconnect()
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

gen_conv = ConversationHandler(
    entry_points=[
        CommandHandler("generate", generate_command), 
        CallbackQueryHandler(generate_command, pattern="^call_generate$")
    ],
    states={
        UNIQUE_NAME_GEN: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_unique_name_for_generate)],
        PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_phone_number)],
        CODE: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_login_code)],
        PASSWORD: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_2fa_password)],
    },
    fallbacks=[CommandHandler("cancel", cancel_command_conv)],
    conversation_timeout=300,
)