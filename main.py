import logging
import asyncio
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, ConversationHandler,
    MessageHandler, filters
)

# Import configurations and handlers
from config import (
    BOT_TOKEN, GET_CUSTOM_NAME, GET_PHONE_NUMBER, GET_PHONE_CODE, GET_2FA_PASSWORD,
    GET_STRING_NAME, GET_SESSION_STRING, SELECT_ACCOUNT_SINGLE, TERMINATE_SESSION_CONFIRM,
    REMOVE_ACCOUNT_CONFIRM
)
from userbot_manager import start_all_userbots
from handlers.command_handlers import (
    start_command, accs_command, ping_command, refresh_command
)
from handlers.add_handlers import (
    add_interactive_command, get_custom_name, get_phone_number, get_phone_code,
    get_2fa_password, complete_login, add_string_command, get_string_name,
    get_session_string, cancel_add
)
from handlers.menu_handlers import (
    menu_entry, change_page, handle_single_selection, terminate_session,
    confirm_remove_account, do_remove_account, cancel_selection
)

# --- Logging Setup ---
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
# Reduce verbosity of third-party libraries
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("pyrogram").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

def main() -> None:
    """Configures and runs the Telegram bot."""
    
    # --- Application Setup ---
    application = Application.builder().token(BOT_TOKEN).build()

    # --- Job Queue for Startup ---
    # Run the userbot startup asynchronously after the bot has started polling
    application.job_queue.run_once(
        lambda ctx: asyncio.create_task(start_all_userbots(ctx.application)),
        5  # Start after a 5-second delay
    )

    # --- Conversation Handlers ---
    add_interactive_conv = ConversationHandler(
        entry_points=[CommandHandler("add", add_interactive_command)],
        states={
            GET_CUSTOM_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_custom_name)],
            GET_PHONE_NUMBER: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_phone_number)],
            GET_PHONE_CODE: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_phone_code)],
            GET_2FA_PASSWORD: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_2fa_password)],
        },
        fallbacks=[CommandHandler("cancel", cancel_add)],
        name="add_interactive_conv",
        persistent=False
    )

    add_string_conv = ConversationHandler(
        entry_points=[CommandHandler("add_string", add_string_command)],
        states={
            GET_STRING_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_string_name)],
            GET_SESSION_STRING: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_session_string)],
        },
        fallbacks=[CommandHandler("cancel", cancel_add)],
        name="add_string_conv",
        persistent=False
    )

    menu_conv = ConversationHandler(
        entry_points=[
            CommandHandler("sessions", menu_entry),
            CommandHandler("remove", menu_entry),
        ],
        states={
            SELECT_ACCOUNT_SINGLE: [
                CallbackQueryHandler(handle_single_selection, pattern="^select_single_"),
                CallbackQueryHandler(change_page, pattern="^page_single_"),
            ],
            TERMINATE_SESSION_CONFIRM: [
                CallbackQueryHandler(terminate_session, pattern="^term_"),
            ],
            REMOVE_ACCOUNT_CONFIRM: [
                CallbackQueryHandler(do_remove_account, pattern="^remove_confirm_yes$")
            ]
        },
        fallbacks=[CallbackQueryHandler(cancel_selection, pattern="^cancel_selection")],
        name="menu_conv",
        persistent=False
    )

    # --- Add all handlers to the application ---
    # Standard commands
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("accs", accs_command))
    application.add_handler(CommandHandler("ping", ping_command))
    application.add_handler(CommandHandler("refresh", refresh_command))

    # Conversation handlers
    application.add_handler(add_interactive_conv)
    application.add_handler(add_string_conv)
    application.add_handler(menu_conv)

    # --- Start the Bot ---
    logger.info("Bot is starting and beginning to poll for updates...")
    application.run_polling()

if __name__ == "__main__":
    main()
