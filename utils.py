from functools import wraps
from telegram import Update
from telegram.ext import ContextTypes
from config import OWNER_ID

def owner_only(func):
    """Decorator to restrict access to the owner."""
    @wraps(func)
    async def wrapped(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        if not update or not update.effective_user:
            return  # Cannot check owner if there's no user context

        if update.effective_user.id != OWNER_ID:
            if update.message:
                await update.message.reply_text("⛔️ You are not authorized to use this command.")
            elif update.callback_query:
                await update.callback_query.answer("⛔️ Unauthorized", show_alert=True)
            return
        return await func(update, context, *args, **kwargs)
    return wrapped
