import asyncio
import io
import time
import base64
import qrcode
from pyrogram import Client, types
from pyrogram.errors import (
    SessionPasswordNeeded, PasswordHashInvalid, FloodWait, 
    PhoneNumberInvalid, PhoneNumberBanned, AuthTokenExpired,
    RPCError
)
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto
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
    UNIQUE_NAME_GEN, PHONE, CODE, PASSWORD, QR_LOGIN,
    TD_API_ID, TD_API_HASH, TD_SYSTEM_VERSION, 
    TD_APP_VERSION, TD_LANG_CODE, 
    TD_SYSTEM_LANG_CODE, TD_LANG_PACK
)
from utils import (
    owner_only, generate_device_name, escape_html, COMMAND_FALLBACKS,
    sanitize_unique_name
)
from userbot_logic import start_userbot 

@owner_only
async def generate_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Entry point for session generation."""
    message = update.message or update.callback_query.message
    context.user_data.clear()

    if update.callback_query:
        await update.callback_query.answer()

    if not update.callback_query and context.args:
        if context.args[0] == '-sess':
            keyboard = [
                [InlineKeyboardButton("Single String", callback_data="add_single")],
                [InlineKeyboardButton("Multiple Strings", callback_data="add_multiple")],
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await message.reply_html("➕  <b>Add Account via Session</b>", reply_markup=reply_markup)
            return ConversationHandler.END
        
        if context.args[0] == '-qr':
            context.user_data['is_qr_flow'] = True
            await message.reply_text("📱 <b>QR Code Login</b>\n\nPlease send a unique name for this account first.", parse_mode=ParseMode.HTML)
            return UNIQUE_NAME_GEN
        
    await message.reply_text("Please send a unique name (e.g., 'work_acct') for this new account. Send /cancel to stop.")
    return UNIQUE_NAME_GEN

@owner_only
async def get_unique_name_for_generate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Saves unique name, selects persistent device model, and asks for phone number OR starts QR flow."""
    raw_input = update.message.text.strip().split()[0]
    unique_name = sanitize_unique_name(raw_input)
    
    if accounts_collection.find_one({"unique_name": unique_name}):
        await update.message.reply_text(f"The name '{unique_name}' is already taken. Please choose another one.")
        return UNIQUE_NAME_GEN 

    persistent_device_model = generate_device_name()
    context.user_data['unique_name'] = unique_name
    context.user_data['persistent_device_model'] = persistent_device_model
    
    if context.user_data.get('is_qr_flow'):
        status_msg = await update.message.reply_text(f"Name: <b>{unique_name}</b>\nPreparing QR Code... ⏳", parse_mode=ParseMode.HTML)
        return await qr_login_handler(update, context, status_msg)

    await update.message.reply_text(f"Name: <b>{unique_name}</b>\nInput Phone Number", parse_mode=ParseMode.HTML)
    return PHONE

async def qr_login_handler(update: Update, context: ContextTypes.DEFAULT_TYPE, status_msg=None):
    """Handles the QR Code Login using Client.sign_in_qrcode() from Pyroblack."""
    unique_name = context.user_data.get('unique_name')
    persistent_device_model = context.user_data.get('persistent_device_model')
    
    # 1. Initialize Client
    client = Client(
        name=f"temp_gen_{update.effective_user.id}_{int(time.time())}", 
        in_memory=True, 
        api_id=TD_API_ID,
        api_hash=TD_API_HASH,
        workers=1,
        device_model=persistent_device_model, 
        system_version=TD_SYSTEM_VERSION,
        app_version=TD_APP_VERSION,
        lang_code=TD_LANG_CODE,
        system_lang_code=TD_SYSTEM_LANG_CODE,
        lang_pack=TD_LANG_PACK
    )
    context.user_data['temp_client'] = client

    if not status_msg:
        status_msg = await update.message.reply_text("⏳ Connecting...")
    else:
        await status_msg.edit_text("⏳ Connecting...")

    try:
        await client.connect()
    except Exception as e:
        await status_msg.edit_text(f"❌ Connection Failed: {e}")
        return ConversationHandler.END

    # 2. QR Loop
    start_time = time.time()
    total_timeout = 180 
    
    qr_message_id = status_msg.message_id
    chat_id = update.effective_chat.id
    last_qr_string = None
    
    while True:
        elapsed = time.time() - start_time
        if elapsed > total_timeout:
            if qr_message_id:
                try: await context.bot.delete_message(chat_id=chat_id, message_id=qr_message_id)
                except: pass
            await context.bot.send_message(chat_id=chat_id, text="❌ QR add timed out.")
            if client.is_connected: await client.disconnect()
            context.user_data.clear()
            return ConversationHandler.END

        try:
            # --- USE PYROBLACK NATIVE METHOD ---
            # This returns either a User (success) or a LoginToken (pending)
            result = await client.sign_in_qrcode()
            
            # --- CASE 1: LOGGED IN (User Object) ---
            if isinstance(result, types.User):
                if qr_message_id:
                    try: await context.bot.delete_message(chat_id=chat_id, message_id=qr_message_id)
                    except: pass
                
                success_msg = await context.bot.send_message(chat_id=chat_id, text="✅ QR Scanned! Verifying...")
                return await finalize_login(update, context, client, success_msg)

            # --- CASE 2: PENDING (LoginToken) ---
            elif isinstance(result, types.LoginToken):
                # Extract URL (Pyroblack tokens usually have .url, otherwise construct it)
                qr_url = getattr(result, 'url', None)
                if not qr_url:
                    # Fallback: Construct URL from token bytes
                    safe_token = base64.urlsafe_b64encode(result.token).decode('utf-8').rstrip('=')
                    qr_url = f"tg://login?token={safe_token}"

                # Only update message if the code changed
                if last_qr_string != qr_url:
                    last_qr_string = qr_url
                    
                    qr = qrcode.QRCode(border=2)
                    qr.add_data(qr_url)
                    qr.make(fit=True)
                    img = qr.make_image(fill='black', back_color='white')
                    
                    bio = io.BytesIO()
                    img.save(bio)
                    bio.seek(0)
                    
                    remaining = int(total_timeout - elapsed)
                    caption = (
                        "⚡️ <b>QR Login</b>\n"
                        "👆 Scan the QR code above for a quick and easy account adding.\n\n"
                        f"⏳ <b>In total, you have {remaining} seconds.</b>\n"
                        "-> This QR code is refreshed every ~30 seconds."
                    )
                    
                    keyboard = [[InlineKeyboardButton("❌ Cancel", callback_data="cancel_qr")]]
                    reply_markup = InlineKeyboardMarkup(keyboard)

                    try:
                        await context.bot.edit_message_media(
                            chat_id=chat_id,
                            message_id=qr_message_id,
                            media=InputMediaPhoto(media=bio, caption=caption, parse_mode=ParseMode.HTML),
                            reply_markup=reply_markup
                        )
                    except Exception:
                        try: await context.bot.delete_message(chat_id=chat_id, message_id=qr_message_id)
                        except: pass
                        sent = await context.bot.send_photo(chat_id=chat_id, photo=bio, caption=caption, parse_mode=ParseMode.HTML, reply_markup=reply_markup)
                        qr_message_id = sent.message_id
                
                # Wait briefly before checking status again
                await asyncio.sleep(1.5)
                continue
            
            else:
                # Unexpected result type
                await asyncio.sleep(1)
                continue

        except SessionPasswordNeeded:
            # 2FA Triggered
            if qr_message_id:
                try: await context.bot.delete_message(chat_id=chat_id, message_id=qr_message_id)
                except: pass
            
            hint = await client.get_password_hint()
            hint_text = f" (Hint: {escape_html(hint)})" if hint else ""
            
            pwd_msg = await context.bot.send_message(
                chat_id=chat_id, 
                text=f"🔐 <b>2FA Required</b>{hint_text}\n\nPlease enter your password.", 
                parse_mode=ParseMode.HTML
            )
            context.user_data['pwd_msg_id'] = pwd_msg.message_id
            return PASSWORD
        
        except (AuthTokenExpired, RPCError):
            # Token expired or network blip, retry loop to get new one
            await asyncio.sleep(1)
            continue
        
        except Exception as e:
            logger.error(f"QR Error: {e}")
            await asyncio.sleep(1)
            continue


@owner_only
async def cancel_qr_login(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    try: await query.message.delete()
    except: pass
    await context.bot.send_message(chat_id=update.effective_chat.id, text="❌ QR add cancelled.")
    if 'temp_client' in context.user_data:
        client = context.user_data.get('temp_client')
        if client and client.is_connected: await client.disconnect()
    context.user_data.clear()
    return ConversationHandler.END

@owner_only
async def get_phone_number(update: Update, context: ContextTypes.DEFAULT_TYPE):
    phone = update.message.text.strip()
    msg = await update.message.reply_text("⏳ Connecting to Telegram...")
    
    persistent_device_model = context.user_data.get('persistent_device_model')
    client = Client(
        name=f"temp_gen_{update.effective_user.id}", 
        in_memory=True, 
        api_id=TD_API_ID,
        api_hash=TD_API_HASH,
        workers=1,
        device_model=persistent_device_model, 
        system_version=TD_SYSTEM_VERSION,
        app_version=TD_APP_VERSION,
        lang_code=TD_LANG_CODE,
        system_lang_code=TD_SYSTEM_LANG_CODE,
        lang_pack=TD_LANG_PACK
    )

    try:
        await asyncio.wait_for(client.connect(), timeout=30.0)
    except Exception as e:
        await msg.edit_text(f"❌ <b>Connection Error:</b> <code>{escape_html(str(e))}</code>\nCancelled.", parse_mode=ParseMode.HTML)
        context.user_data.clear()
        return ConversationHandler.END
    
    sent_code = None
    while True:
        try:
            sent_code = await client.send_code(phone)
            break 
        except FloodWait as e:
            wait_time = e.value
            await msg.edit_text(f"⏳ <b>Telegram says 'Wait':</b> {wait_time} seconds...\n<i>(Retrying automatically)</i>", parse_mode=ParseMode.HTML)
            await asyncio.sleep(wait_time)
        except (PhoneNumberInvalid, PhoneNumberBanned) as e:
            await msg.edit_text(f"❌ <b>Phone Error:</b> {e}\nCancelled.", parse_mode=ParseMode.HTML)
            if client.is_connected: await client.disconnect()
            context.user_data.clear()
            return ConversationHandler.END
        except Exception as e:
            await msg.edit_text(f"❌ <b>Error:</b> <code>{escape_html(str(e))}</code>\nCancelled.", parse_mode=ParseMode.HTML)
            if client.is_connected: await client.disconnect()
            context.user_data.clear()
            return ConversationHandler.END

    context.user_data.update({'phone': phone, 'phone_code_hash': sent_code.phone_code_hash, 'temp_client': client})
    
    delivery_text = "Send login code"
    if sent_code.type:
        type_str = str(sent_code.type).upper()
        if "EMAIL" in type_str:
            delivery_text = "☑️ Code sent to <b>EMAIL</b>"
        elif "APP" in type_str:
            delivery_text = "☑️ Code sent to <b>TG</b>"
        elif "SMS" in type_str:
            delivery_text = "☑️ Code sent via <b>SMS</b>"
        else:
            delivery_text = f"☑️ Code sent via <b>{escape_html(type_str)}</b>"
    
    delivery_text += "\n\n👇 Send the code below."
    await msg.edit_text(delivery_text, parse_mode=ParseMode.HTML)
    return CODE

@owner_only
async def get_login_code(update: Update, context: ContextTypes.DEFAULT_TYPE):
    code, client = update.message.text, context.user_data['temp_client']
    phone, phone_code_hash = context.user_data['phone'], context.user_data['phone_code_hash']
    
    msg = await update.message.reply_text("⏳ Signing in...")
    try:
        await client.sign_in(phone, phone_code_hash, code)
        await msg.edit_text("✅ Signed in! Adding account...")
        return await finalize_login(update, context, client, msg)

    except SessionPasswordNeeded:
        hint = await client.get_password_hint()
        context.user_data['password_attempts'] = 0
        hint_text = f" (Hint: {escape_html(hint)})" if hint else ""
        await msg.edit_text(f"🔐 2FA is enabled{hint_text}.\nSend password.")
        return PASSWORD
    except Exception as e:
        await msg.edit_text(f"❌ <b>Error:</b> <code>{escape_html(str(e))}</code>. Cancelled.", parse_mode=ParseMode.HTML)
        if client.is_connected: await client.disconnect()
        context.user_data.clear()
        return ConversationHandler.END

@owner_only
async def get_2fa_password(update: Update, context: ContextTypes.DEFAULT_TYPE):
    password, client = update.message.text, context.user_data['temp_client']
    
    msg = None
    if 'pwd_msg_id' in context.user_data:
        msg = await update.message.reply_text("⏳ Checking password...")
    else:
        msg = await update.message.reply_text("⏳ Checking password...")

    try:
        await client.check_password(password)
        await msg.edit_text("✅ Password correct! Adding account...")
        context.user_data['successful_2fa_pwd'] = password
        return await finalize_login(update, context, client, msg)

    except PasswordHashInvalid:
        attempts = context.user_data.get('password_attempts', 0) + 1
        context.user_data['password_attempts'] = attempts
        if attempts < 3:
            await msg.edit_text(f"❌ Incorrect password (Attempt {attempts}/3).\nTry again.")
            return PASSWORD
        else:
            await msg.edit_text("❌ Incorrect password. Attempts (3/3). Cancelled.")
            if client.is_connected: await client.disconnect()
            context.user_data.clear()
            return ConversationHandler.END
    except Exception as e:
        await msg.edit_text(f"❌ <b>Error:</b> <code>{escape_html(str(e))}</code>. Cancelled.", parse_mode=ParseMode.HTML)
        if client.is_connected: await client.disconnect()
        context.user_data.clear()
        return ConversationHandler.END

async def finalize_login(update: Update, context: ContextTypes.DEFAULT_TYPE, client: Client, status_message=None):
    unique_name = context.user_data.get('unique_name')
    persistent_device_model = context.user_data.get('persistent_device_model')
    
    try: await client.get_me()
    except: pass
    
    session_string = await client.export_session_string()
    
    if client.is_connected: 
        await client.disconnect()
    
    status, user_info, detail = await start_userbot(
        session_string, 
        context.application, 
        update_info=True, 
        unique_name=unique_name,
        run_acquaintance=True,
        device_model_to_use=persistent_device_model 
    )
    
    pwd = context.user_data.get('successful_2fa_pwd')
    if status == "success" and pwd and accounts_collection is not None:
        try:
            accounts_collection.update_one(
                {"user_id": user_info.id},
                {"$set": {"two_fa_password": pwd}}
            )
        except Exception: pass

    final_text = ""
    if status == "success":
        final_text = f"✅ Account <code>{escape_html(user_info.first_name)}</code> (<code>{escape_html(unique_name)}</code>) added successfully!"
    else:
        final_text = f"⚠️ Error adding account: {detail}"

    if status_message:
        try:
            await status_message.edit_text(final_text, parse_mode=ParseMode.HTML)
        except Exception:
            try: await context.bot.send_message(chat_id=update.effective_chat.id, text=final_text, parse_mode=ParseMode.HTML)
            except: pass
    else:
        try:
            await context.bot.send_message(chat_id=update.effective_chat.id, text=final_text, parse_mode=ParseMode.HTML)
        except: pass

    context.user_data.clear()
    return ConversationHandler.END

@owner_only
async def cancel_command_conv(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if 'temp_client' in context.user_data:
        client = context.user_data.get('temp_client')
        if client and client.is_connected: await client.disconnect()
    context.user_data.clear()
    
    cancel_text = "✖️ Process cancelled."
    if update.callback_query:
        await update.callback_query.answer()
        try: await update.callback_query.edit_message_text(cancel_text)
        except: await update.callback_query.message.reply_text(cancel_text)
    else:
        await update.message.reply_text(cancel_text)
    return ConversationHandler.END

gen_conv = ConversationHandler(
    entry_points=[
        CommandHandler("add", generate_command), 
        CallbackQueryHandler(generate_command, pattern="^call_add_command$") 
    ],
    states={
        UNIQUE_NAME_GEN: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_unique_name_for_generate)],
        PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_phone_number)],
        CODE: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_login_code)],
        PASSWORD: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_2fa_password)],
        QR_LOGIN: [
            CallbackQueryHandler(cancel_qr_login, pattern="^cancel_qr$"),
            MessageHandler(filters.TEXT, cancel_command_conv)
        ]
    },
    fallbacks=[
        CommandHandler("cancel", cancel_command_conv),
        *COMMAND_FALLBACKS 
    ],
    conversation_timeout=300,
)
