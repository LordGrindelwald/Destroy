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
from session_generator import gen_conv # Import generate flow
from bot_handlers import (
    start_command, settings_command,
    status_command, temp_pause_command, temp_pause_all, ping_command,
    refresh_command, cancel_command, rename_command,
    deduplicate_db_command,
    accounts_menu, set_next_step,
    pause_notifications_callback, handle_text_input,
    paste_single_conv, accounts_command, restart_command,
    online_interval_conv, remove_conv, # <-- NEW: Import remove_conv
    account_detail_command, toggle_otp_destroy_command, # <-- NEW: Import new commands
    two_fa_conv, update_2fa_password_command # <-- NEW: Import 2FA conv and update cmd
)

# --- New dummy function for silent command ---
async def do_nothing(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Does absolutely nothing."""
    return

# --- Asynchronous startup logic for Pyrogram clients ---
async def post_init_tasks(application: Application):
    """Runs after the bot application initializes."""
    await application.bot.get_me()
    logger.info(f"Management bot @{application.bot.username} started.")
    
    # Start userbots
    await start_all_userbots_from_db(application)
    
    logger.info("Bot is now running. Press Ctrl-C to stop.")

# --- Asynchronous shutdown logic for Pyrogram clients ---
async def post_shutdown_tasks(application: Application):
    """Runs before the bot application shuts down."""
    logger.info("Shutting down userbots...")
    
    # Use asyncio.gather for concurrent, faster shutdown
    stop_tasks = [client.stop() for client in active_userbots.values() if client.is_connected]
    if stop_tasks:
        await asyncio.gather(*stop_tasks, return_exceptions=True)
        logger.info("All userbots gracefully stopped.")
    
    logger.info("Shutdown complete.")

def main() -> None:
    """Configures and runs the bot."""
    
    # --- Application Setup ---
    application = Application.builder().token(BOT_TOKEN) \
        .post_init(post_init_tasks) \
        .post_shutdown(post_shutdown_tasks) \
        .build()

    # --- Register Handlers ---
    
    # 1. Conversation Handlers
    application.add_handler(gen_conv, group=0) # <-- Handles /add
    application.add_handler(paste_single_conv, group=0)
    application.add_handler(online_interval_conv, group=0)
    application.add_handler(remove_conv, group=0) # <-- NEW: Handles /remove
    application.add_handler(two_fa_conv, group=0) # <-- NEW: Handles /2fas

    # 2. Command Handlers
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("settings", settings_command))
    # /add is now in gen_conv
    # /remove is now in remove_conv
    application.add_handler(CommandHandler("rename", rename_command))
    application.add_handler(CommandHandler("status", status_command))
    application.add_handler(CommandHandler("temp", temp_pause_command))
    application.add_handler(CommandHandler("temp_fwd", temp_pause_all))
    application.add_handler(CommandHandler("ping", ping_command))
    application.add_handler(CommandHandler("refresh", refresh_command))
    application.add_handler(CommandHandler("accs", accounts_command)) # <-- NEW: Handles /accs and /accs -de
    application.add_handler(CommandHandler("acc", account_detail_command)) # <-- NEW: Handles /acc <name>
    application.add_handler(CommandHandler("toggle_otp_destroy", toggle_otp_destroy_command)) # <-- NEW
    application.add_handler(CommandHandler("update2fa", update_2fa_password_command)) # <-- NEW
    application.add_handler(CommandHandler("cancel", cancel_command))
    application.add_handler(CommandHandler("restart", restart_command))
    application.add_handler(CommandHandler("deduplicate_db", deduplicate_db_command))
    application.add_handler(CommandHandler("init_abc", do_nothing))
    
    # 3. CallbackQuery Handlers
    application.add_handler(CallbackQueryHandler(pause_notifications_callback, pattern=r"^pause_notify_"))
    
    # Multi-string paste setup
    application.add_handler(CallbackQueryHandler(partial(set_next_step, step='awaiting_multiple_accounts', text="Please paste all session strings, separated by a space or new line."), pattern="^add_multiple$"))
    
    # Navigation/Action callbacks
    application.add_handler(CallbackQueryHandler(restart_command, pattern="^call_restart$"))
    application.add_handler(CallbackQueryHandler(settings_command, pattern="^main_settings$"))
    # application.add_handler(CallbackQueryHandler(add_command, pattern="^call_add_command$")) # <-- REMOVED (now in gen_conv)
    application.add_handler(CallbackQueryHandler(accounts_menu, pattern="^manage_accounts$"))
    # application.add_handler(CallbackQueryHandler(execute_remove_account, pattern=r"^delete_account_")) # <-- REMOVED (now in remove_conv)
    
    # 4. Message Handler (must be low priority)
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_input), group=1)
    
    logger.info("Bot is starting...")
    
    application.run_polling(poll_interval=0.5, allowed_updates=Update.ALL_TYPES)
        

if __name__ == "__main__":
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
            # --- THIS IS THE RESTART FIX ---
            # Catch SystemExit. If it's code 1 (from restart), re-raise to exit non-zero
            if e.code == 1:
                logger.info("SystemExit(1) received, triggering container restart.")
                raise # Re-raise the SystemExit(1)
            else:
                # Otherwise, it's a normal graceful exit
                logger.info("SystemExit received. Exiting gracefully.")
        except Exception:
             # Ensure proper logging for unexpected crashes
            import traceback
            logger.error(f"FATAL UNHANDLED EXCEPTION: {traceback.format_exc()}")
