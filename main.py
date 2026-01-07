import asyncio
import math 
from functools import partial
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    filters,
    ContextTypes,
)

# Import from our own modules
from config import (
    BOT_TOKEN, logger, MONGO_URI, 
    OWNER_ID, active_userbots
)
from userbot_logic import start_all_userbots_from_db
from session_generator import gen_conv 
from bot_handlers import (
    start_command, settings_command,
    status_command, temp_pause_command, temp_pause_all, ping_command,
    refresh_command, cancel_command, rename_command,
    deduplicate_db_command,
    accounts_menu, set_next_step,
    pause_notifications_callback, handle_text_input,
    paste_single_conv, accounts_command, restart_command,
    online_interval_conv, remove_conv, 
    account_detail_command, toggle_otp_destroy_command, 
    two_fa_conv, update_2fa_password_command,
    # NEW IMPORTS
    backup_command, restore_command, encrypt_past_command, fix_db_command, debug_account_command, hard_delete_command
)

async def do_nothing(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Dummy handler for /init_abc to avoid 'command not found' logs if user types it."""
    return

async def post_init_tasks(application: Application):
    """
    Called after the bot has started.
    Starts all userbots stored in the database.
    """
    await application.bot.get_me()
    logger.info(f"Management bot @{application.bot.username} started.")
    
    # Start Userbots
    await start_all_userbots_from_db(application)
    
    logger.info("Bot is now running. Press Ctrl-C to stop.")

async def post_shutdown_tasks(application: Application):
    """
    Called when the bot is stopping.
    Gracefully stops all userbots.
    """
    logger.info("Shutting down userbots...")
    stop_tasks = [client.stop() for client in active_userbots.values() if client.is_connected]
    if stop_tasks:
        await asyncio.gather(*stop_tasks, return_exceptions=True)
    logger.info("Shutdown complete.")

def main() -> None:
    application = Application.builder().token(BOT_TOKEN) \
        .post_init(post_init_tasks) \
        .post_shutdown(post_shutdown_tasks) \
        .build()

    # 1. Conversation Handlers (Must be added first)
    application.add_handler(gen_conv, group=0) 
    application.add_handler(paste_single_conv, group=0)
    application.add_handler(online_interval_conv, group=0)
    application.add_handler(remove_conv, group=0) 
    application.add_handler(two_fa_conv, group=0) 

    # 2. Command Handlers
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("settings", settings_command))
    application.add_handler(CommandHandler("rename", rename_command))
    application.add_handler(CommandHandler("status", status_command))
    application.add_handler(CommandHandler("temp", temp_pause_command))
    application.add_handler(CommandHandler("temp_fwd", temp_pause_all))
    application.add_handler(CommandHandler("ping", ping_command))
    application.add_handler(CommandHandler("refresh", refresh_command))
    application.add_handler(CommandHandler("accs", accounts_command)) 
    application.add_handler(CommandHandler("acc", account_detail_command)) 
    application.add_handler(CommandHandler("toggle_otp_destroy", toggle_otp_destroy_command)) 
    application.add_handler(CommandHandler("update2fa", update_2fa_password_command)) 
    application.add_handler(CommandHandler("cancel", cancel_command))
    application.add_handler(CommandHandler("restart", restart_command))
    application.add_handler(CommandHandler("deduplicate_db", deduplicate_db_command))
    application.add_handler(CommandHandler("nuke", hard_delete_command))
    application.add_handler(CommandHandler("init_abc", do_nothing))

    # --- NEW COMMANDS ---
    application.add_handler(CommandHandler("backup", backup_command))
    application.add_handler(CommandHandler("restore", restore_command))
    application.add_handler(CommandHandler("encrpast", encrypt_past_command))
    application.add_handler(CommandHandler("fix_db", fix_db_command))
    application.add_handler(CommandHandler("debug_acc", debug_account_command))
    
    # 3. CallbackQuery Handlers
    # For paused notification button
    application.add_handler(CallbackQueryHandler(pause_notifications_callback, pattern=r"^pause_notify_"))
    
    # For "Add New Account" -> "Multiple Strings" button
    application.add_handler(CallbackQueryHandler(partial(set_next_step, step='awaiting_multiple_accounts', text="Please paste all session strings, separated by a space or new line."), pattern="^add_multiple$"))
    
    # For "Restart" button
    application.add_handler(CallbackQueryHandler(restart_command, pattern="^call_restart$"))
    
    # Navigation Back
    application.add_handler(CallbackQueryHandler(settings_command, pattern="^main_settings$"))
    application.add_handler(CallbackQueryHandler(accounts_menu, pattern="^manage_accounts$"))
    
    # 4. Message Handler (for pasting multiple strings)
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_input), group=1)
    
    logger.info("Bot is starting...")
    application.run_polling(poll_interval=0.5, allowed_updates=Update.ALL_TYPES)
        

if __name__ == "__main__":
    # Basic env check
    if not BOT_TOKEN:
        logger.critical("BOT_TOKEN environment variable not set. Exiting.")
    elif not all([MONGO_URI, OWNER_ID]):
        logger.critical("One or more environment variables (MONGO_URI, OWNER_ID) are missing.")
    else:
        try:
            main()
        except KeyboardInterrupt:
            logger.info("Bot stopped manually.")
        except SystemExit as e:
            if e.code == 1:
                # Docker restart policy will restart the container
                logger.info("SystemExit(1) received, triggering container restart.")
                raise 
            else:
                logger.info("SystemExit received. Exiting gracefully.")
        except Exception:
            import traceback
            logger.error(f"FATAL UNHANDLED EXCEPTION: {traceback.format_exc()}")
