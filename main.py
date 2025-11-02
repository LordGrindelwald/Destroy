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

# --- MODIFIED: Main function is now async ---
async def main() -> None:
    """Configures and runs the bot non-blockingly."""
    
    # --- Application Setup ---
    application = Application.builder().token(BOT_TOKEN).build()

    # --- Register Handlers ---
    
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
    
    # --- MODIFIED: Initialization is now outside the final try block ---
    logger.info("Bot is starting...")
    
    # Run initialization tasks
    await application.initialize()
    await application.bot.get_me()
    logger.info(f"Management bot @{application.bot.username} started.")
    
    # Start userbots
    await start_all_userbots_from_db(application)
    
    logger.info("Bot is now running. Press Ctrl-C to stop.")
    
    try:
        # Start polling. Any exception here will be handled by the outer asyncio.run
        await application.run_polling(poll_interval=0.5, allowed_updates=Update.ALL_TYPES)
        
    # We remove the "except Exception as e:" block here to allow exceptions 
    # to propagate for proper event loop cleanup.
    finally:
        # ONLY shut down Pyrogram clients here.
        logger.info("Shutting down userbots...")
        for client in active_userbots.values():
            if client.is_connected:
                await client.stop()
        
        logger.info("Shutdown complete.")


if __name__ == "__main__":
    if not BOT_TOKEN:
        logger.critical("BOT_TOKEN environment variable not set. Exiting.")
    elif not all([MONGO_URI, OWNER_ID]):
        logger.critical("One or more environment variables (MONGO_URI, OWNER_ID) are missing.")
    else:
        # --- MODIFIED: Use asyncio.run to start the async main function ---
        try:
            asyncio.run(main())
        except KeyboardInterrupt:
            logger.info("Bot stopped manually.")