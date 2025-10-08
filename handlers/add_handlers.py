import logging
from telegram import Update
from telegram.ext import ContextTypes, ConversationHandler
from pyrogram import Client as PyrogramClient
from pyrogram.errors import (
    SessionPasswordNeeded, PhoneCodeInvalid, PasswordHashInvalid,
    PhoneCodeExpired, FloodWait
)

from utils import owner_only
from config import (
    GET_CUSTOM_NAME, GET_PHONE_NUMBER, GET_PHONE_CODE, GET_2FA_PASSWORD,
    GET_STRING_NAME, GET_SESSION_STRING, SPOOFED_PARAMS, API_ID, API_HASH
)
from database import accounts_collection
from userbot_manager import start_userbot

logger = logging.getLogger(__name__)

# --- Interactive Add Conversation (/add) ---
@owner_only
async def add_interactive_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Let's add a new account.\n\nPlease provide a unique name for this account (e.g., 'work_acc', 'john_doe').")
    return GET_CUSTOM_NAME

async def get_custom_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    name = update.message.text.strip()
    if accounts_collection.find_one({"custom_name": name}):
        await update.message.reply_text("This name is already taken. Please choose another one.")
        return GET_CUSTOM_NAME

    context.user_data['add_custom_name'] = name
    context.user_data['temp_client'] = PyrogramClient(":memory:", api_id=API_ID, api_hash=API_HASH, **SPOOFED_PARAMS)
    await update.message.reply_text("Great. Now, please send the phone number in international format (e.g., +12223334444).")
    return GET_PHONE_NUMBER

async def get_phone_number(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    phone_number = update.message.text.strip().replace(" ", "").replace("-", "")
    context.user_data['phone_number'] = phone_number
    temp_client: PyrogramClient = context.user_data['temp_client']
    msg = await update.message.reply_text("⏳ Connecting and sending login code...")

    try:
        await temp_client.connect()
        sent_code = await temp_client.send_code(phone_number)
        context.user_data['phone_code_hash'] = sent_code.phone_code_hash
        await msg.edit_text("A login code has been sent to the number. Please send me the code.")
        return GET_PHONE_CODE
    except FloodWait as e:
        await msg.edit_text(f"Telegram is rate-limiting this request. Please wait {e.value} seconds and try again later with /add.")
        return ConversationHandler.END
    except Exception as e:
        logger.error(f"Error sending code: {e}")
        await msg.edit_text(f"An error occurred: {e}. Please ensure the phone number is correct and try again.")
        return ConversationHandler.END

async def get_phone_code(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    phone_code = update.message.text.strip()
    temp_client: PyrogramClient = context.user_data['temp_client']
    phone_number = context.user_data['phone_number']
    phone_code_hash = context.user_data['phone_code_hash']
    msg = await update.message.reply_text("⏳ Verifying code...")

    try:
        await temp_client.sign_in(phone_number, phone_code_hash, phone_code)
        return await complete_login(update, context, msg)
    except SessionPasswordNeeded:
        await msg.edit_text("This account has Two-Factor Authentication enabled. Please send your 2FA password.")
        return GET_2FA_PASSWORD
    except (PhoneCodeInvalid, PhoneCodeExpired):
        await msg.edit_text("❌ The code is invalid or has expired. Please start over with /add.")
        if temp_client.is_connected: await temp_client.disconnect()
        return ConversationHandler.END
    except Exception as e:
        logger.error(f"Error signing in: {e}")
        await msg.edit_text(f"An error occurred: {e}. Please start over.")
        if temp_client.is_connected: await temp_client.disconnect()
        return ConversationHandler.END

async def get_2fa_password(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    password = update.message.text.strip()
    temp_client: PyrogramClient = context.user_data['temp_client']
    msg = await update.message.reply_text("⏳ Verifying password and logging in...")

    try:
        await temp_client.check_password(password)
        return await complete_login(update, context, msg)
    except PasswordHashInvalid:
        await msg.edit_text("❌ Incorrect password. Please start over with /add.")
        if temp_client.is_connected: await temp_client.disconnect()
        return ConversationHandler.END
    except Exception as e:
        logger.error(f"Error checking password: {e}")
        await msg.edit_text(f"An error occurred: {e}. Please start over.")
        if temp_client.is_connected: await temp_client.disconnect()
        return ConversationHandler.END

async def complete_login(update: Update, context: ContextTypes.DEFAULT_TYPE, msg) -> int:
    temp_client: PyrogramClient = context.user_data['temp_client']
    custom_name = context.user_data['add_custom_name']

    session_string = await temp_client.export_session_string()
    me = await temp_client.get_me()

    new_account = {
        "custom_name": custom_name,
        "user_id": me.id,
        "first_name": me.first_name,
        "username": me.username,
        "phone_number": me.phone_number,
        "session_string": session_string,
        "online_interval_min": 1440,
        "otp_forwarding_enabled": True,
        "otp_destroy_enabled": False
    }
    accounts_collection.insert_one(new_account)
    await temp_client.disconnect()

    status = await start_userbot(new_account, context.application)
    if status == "success":
        await msg.edit_text(f"✅ Successfully added and started **{custom_name}**!")
    else:
        await msg.edit_text(f"❌ Account saved, but failed to start (Error: {status}). Try `/refresh`.")

    context.user_data.clear()
    return ConversationHandler.END

# --- Add Account via String Conversation (/add_string) ---
@owner_only
async def add_string_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Please provide a unique name for this account.")
    return GET_STRING_NAME

async def get_string_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    name = update.message.text.strip()
    if accounts_collection.find_one({"custom_name": name}):
        await update.message.reply_text("This name is already taken. Please choose another one.")
        return GET_STRING_NAME

    context.user_data['add_custom_name'] = name
    await update.message.reply_text("Great. Now, please send the Pyrogram session string.")
    return GET_SESSION_STRING

async def get_session_string(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    session_string = update.message.text.strip()
    custom_name = context.user_data['add_custom_name']
    msg = await update.message.reply_text(f"⏳ Verifying session string for **{custom_name}**...")

    try:
        temp_client = PyrogramClient("temp_session", api_id=API_ID, api_hash=API_HASH, session_string=session_string, in_memory=True)
        await temp_client.start()
        me = await temp_client.get_me()

        new_account = {
            "custom_name": custom_name,
            "user_id": me.id,
            "first_name": me.first_name,
            "username": me.username,
            "phone_number": me.phone_number,
            "session_string": session_string,
            # Set default values for new accounts
            "online_interval_min": 1440,
            "otp_forwarding_enabled": True,
            "otp_destroy_enabled": False
        }
        accounts_collection.insert_one(new_account)
        await temp_client.stop()

        status = await start_userbot(new_account, context.application)
        if status == "success":
            await msg.edit_text(f"✅ Successfully added and started **{custom_name}**!")
        else:
            await msg.edit_text(f"❌ Account saved, but failed to start (Error: {status}). Try `/refresh`.")

    except Exception as e:
        logger.error(f"Error adding with string: {e}")
        await msg.edit_text(f"❌ An error occurred. The session string might be invalid. Error: {e}")

    context.user_data.clear()
    return ConversationHandler.END

async def cancel_add(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if 'temp_client' in context.user_data:
        client = context.user_data['temp_client']
        if client.is_connected:
            await client.disconnect()
    context.user_data.clear()
    await update.message.reply_text("Operation cancelled.")
    return ConversationHandler.END
