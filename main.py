import asyncio
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
    start_command, settings_command, add_command, remove_command,
    status_command, temp_pause_command, temp_pause_all, ping_command,
    refresh_command, cancel_command, set_unique_name_command,
    accounts_menu, execute_remove_account, set_next_step,
    pause_notifications_callback, handle_text_input,
    paste_single_conv, accounts_command # Import paste flow
)

# --- MODIFIED: New dummy function for silent command ---
async def do_nothing(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Does absolutely nothing."""
    return

# --- NEW: Asynchronous startup logic for Pyrogram clients ---
async def post_init_tasks(application: Application):
    """Runs after the bot application initializes."""
    await application.bot.get_me()
    logger.info(f"Management bot @{application.bot.username} started.")
    
    # Start userbots
    await start_all_userbots_from_db(application)
    
    logger.info("Bot is now running. Press Ctrl-C to stop.")

# --- NEW: Asynchronous shutdown logic for Pyrogram clients ---
async def post_shutdown_tasks(application: Application):
    """Runs before the bot application shuts down."""
    logger.info("Shutting down userbots...")
    for client in active_userbots.values():
        if client.is_connected:
            await client.stop()
    
    logger.info("Shutdown complete.")

# --- MODIFIED: main() is now a SYNCHRONOUS function ---
def main() -> None:
    """Configures and runs the bot."""
    
    # --- Application Setup ---
    # MODIFIED: Use post_init and post_shutdown for async logic
    application = Application.builder().token(BOT_TOKEN) \
        .post_init(post_init_tasks) \
        .post_shutdown(post_shutdown_tasks) \
        .build()

    # --- Register Handlers (all handlers remain the same) ---
    
    # 1. Conversation Handlers
    application.add_handler(gen_conv, group=0)
    application.add_handler(paste_single_conv, group=0)

    # 2. Command Handlers
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("settings", settings_command))
    application.add_handler(CommandHandler("add", add_command))
    application.add_handler(CommandHandler("remove", remove_command))
    application.add_handler(CommandHandler("xadd", set_unique_name_command))
    application.add_handler(CommandHandler("status", status_command))
    application.add_handler(CommandHandler("temp", temp_pause_command))
    application.add_handler(CommandHandler("temp_fwd", temp_pause_all))
    application.add_handler(CommandHandler("ping", ping_command))
    application.add_handler(CommandHandler("refresh", refresh_command))
    application.add_handler(CommandHandler("accs", accounts_command))
    application.add_handler(CommandHandler("cancel", cancel_command))
    application.add_handler(CommandHandler("init_abc", do_nothing))
    
    # 3. CallbackQuery Handlers
    application.add_handler(CallbackQueryHandler(pause_notifications_callback, pattern=r"^pause_notify_"))
    application.add_handler(CallbackQueryHandler(partial(set_next_step, step='awaiting_multiple_accounts', text="Please paste all session strings, separated by a space or new line."), pattern="^add_multiple$"))
    application.add_handler(CallbackQueryHandler(settings_command, pattern="^main_settings$"))
    application.add_handler(CallbackQueryHandler(add_command, pattern="^call_add_command$"))
    application.add_handler(CallbackQueryHandler(accounts_menu, pattern="^manage_accounts$"))
    application.add_handler(CallbackQueryHandler(execute_remove_account, pattern=r"^delete_account_"))
    
    # 4. Message Handler (must be low priority)
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_input), group=1)
    
    # --- MODIFIED: Start the bot blocking (synchronously) ---
    logger.info("Bot is starting...")
    
    try:
        # run_polling() is the final, synchronous, blocking call 
        # that handles the event loop correctly.
        application.run_polling(poll_interval=0.5, allowed_updates=Update.ALL_TYPES)
        
    except Exception as e:
        logger.critical(f"Bot failed during polling: {e}")


if __name__ == "__main__":
    if not BOT_TOKEN:
        logger.critical("BOT_TOKEN environment variable not set. Exiting.")
    elif not all([MONGO_URI, OWNER_ID]):
        logger.critical("One or more environment variables (MONGO_URI, OWNER_ID) are missing.")
    else:
        # --- MODIFIED: Call main() directly without asyncio.run ---
        try:
            main()
        except KeyboardInterrupt:
            logger.info("Bot stopped manually.")