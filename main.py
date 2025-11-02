from functools import partial
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    filters,
)

# Import from our own modules
from config import BOT_TOKEN, logger
from userbot_logic import start_all_userbots_from_db
from session_generator import gen_conv, generate_command # Import conv handler and its entry point
from bot_handlers import (
    start_command, settings_command, add_command, remove_account_menu,
    status_command, temp_pause_command, temp_pause_all, ping_command,
    refresh_command, cancel_command,
    accounts_menu, execute_remove_account, set_next_step,
    pause_notifications_callback, handle_text_input
)

def main() -> None:
    """Configures and runs the bot."""
    
    # --- Application Setup ---
    application = Application.builder().token(BOT_TOKEN).build()
    
    async def post_init_task(app: Application):
        """Task to run after bot is initialized but before polling."""
        await app.bot.get_me()
        logger.info(f"Management bot @{app.bot.username} started.")
        await start_all_userbots_from_db(app)

    application.post_init = post_init_task

    # --- Register Handlers ---
    
    # 1. Conversation Handler
    application.add_handler(gen_conv, group=0)

    # 2. Command Handlers
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("settings", settings_command))
    application.add_handler(CommandHandler("add", add_command))
    application.add_handler(CommandHandler("remove", remove_account_menu))
    application.add_handler(CommandHandler("status", status_command))
    application.add_handler(CommandHandler("temp", temp_pause_command))
    application.add_handler(CommandHandler("temp_fwd", temp_pause_all))
    application.add_handler(CommandHandler("ping", ping_command))
    application.add_handler(CommandHandler("refresh", refresh_command))
    application.add_handler(CommandHandler("cancel", cancel_command)) # General cancel
    
    # 3. CallbackQuery Handlers
    application.add_handler(CallbackQueryHandler(pause_notifications_callback, pattern=r"^pause_notify_"))
    application.add_handler(CallbackQueryHandler(partial(set_next_step, step='awaiting_single_account', text="Please paste the session string."), pattern="^add_single$"))
    application.add_handler(CallbackQueryHandler(partial(set_next_step, step='awaiting_multiple_accounts', text="Please paste all session strings, separated by a space or new line."), pattern="^add_multiple$"))
    application.add_handler(CallbackQueryHandler(settings_command, pattern="^main_settings$"))
    application.add_handler(CallbackQueryHandler(add_command, pattern="^call_add_command$"))
    # Note: call_generate is handled by gen_conv
    application.add_handler(CallbackQueryHandler(accounts_menu, pattern="^manage_accounts$"))
    application.add_handler(CallbackQueryHandler(execute_remove_account, pattern=r"^delete_account_"))
    
    # 4. Message Handler (must be low priority)
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_input), group=1)
    
    # --- Start Bot ---
    logger.info("Bot is starting polling...")
    application.run_polling()

if __name__ == "__main__":
    if not BOT_TOKEN:
        logger.critical("BOT_TOKEN environment variable not set. Exiting.")
    else:
        main()
