from telegram.ext import ContextTypes
from config import paused_forwarding, paused_notifications, OWNER_ID, logger
from telegram.constants import ParseMode

async def resume_forwarding_job(context: ContextTypes.DEFAULT_TYPE):
    """Job callback to resume OTP processing for a single user."""
    job_data = context.job.data
    user_id_to_resume = job_data['user_id']
    pause_id = job_data['pause_id']
    message_id = job_data['message_id']
    
    paused_forwarding.discard(user_id_to_resume)
    resumed_text = f"Resumed OTP processing for user ID {user_id_to_resume}."
    
    if context.bot_data.get(pause_id): # Check if notifications were also paused
        paused_notifications.discard(OWNER_ID)
        resumed_text = f"Resumed OTP processing and notifications for user ID {user_id_to_resume}."
    
    logger.info(resumed_text)
    await context.bot.send_message(OWNER_ID, resumed_text)
    try:
        await context.bot.edit_message_text(chat_id=OWNER_ID, message_id=message_id, 
                                            text=f"<i>Pause ended for user ID {user_id_to_resume}.</i>", 
                                            parse_mode=ParseMode.HTML)
    except Exception:
        pass # Message might have been deleted, ignore error
    
    if pause_id in context.bot_data:
        del context.bot_data[pause_id]

async def resume_all_job(context: ContextTypes.DEFAULT_TYPE):
    """Job callback to resume all OTP processing and notifications."""
    paused_forwarding.clear()
    paused_notifications.discard(OWNER_ID)
    logger.info("Resumed all OTP processing and notifications.")
    await context.bot.send_message(OWNER_ID, "Resumed all OTP processing and notifications.")
