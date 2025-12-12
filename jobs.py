from telegram.ext import ContextTypes
from config import (
    paused_forwarding, paused_notifications, 
    OWNER_ID, logger, active_userbots, accounts_collection
)
from telegram.constants import ParseMode
from utils import parse_interval
import traceback
# --- FIX: Import the errors used in the except block to prevent crashes ---
from pyrogram.errors import AuthKeyUnregistered, UserDeactivated 

# --- ONLINE INTERVAL JOB (Efficiency Check) ---
async def online_interval_job(context: ContextTypes.DEFAULT_TYPE):
    """
    Job callback to update online status for a userbot.
    This job schedules itself to run again based on the DB interval.
    Uses get_me() for a light check.
    """
    job_data = context.job.data
    user_id = job_data['user_id']
    
    # Check if bot is still active and in our control
    if user_id not in active_userbots:
        logger.warning(f"Interval job: Bot {user_id} is no longer active. Stopping job.")
        return # Do not reschedule
        
    client = active_userbots[user_id]
    
    if accounts_collection is None:
        logger.error("Interval job: DB connection lost. Retrying in 5 mins.")
        # Reschedule for 5 minutes later
        context.application.job_queue.run_once(
            online_interval_job, 
            300, 
            data={'user_id': user_id}, 
            name=f"interval_{user_id}"
        )
        return

    try:
        # 1. Perform the "online" action
        await client.get_me()
        
        # 2. Get account info to find next interval
        account = accounts_collection.find_one({"user_id": user_id})
        if not account:
            logger.warning(f"Interval job: Account {user_id} not in DB. Stopping job.")
            return # Do not reschedule

        # 3. Get interval and calculate next sleep time
        interval_str = account.get("online_interval", "1440")
        sleep_duration_sec = parse_interval(interval_str)
        
        logger.info(f"Interval job: Account {user_id} updated online. Next update in {sleep_duration_sec // 60} minutes.")
        
        # 4. Reschedule this same job for the future
        context.application.job_queue.run_once(
            online_interval_job, 
            sleep_duration_sec, 
            data={'user_id': user_id}, 
            name=f"interval_{user_id}"
        )

    except (AuthKeyUnregistered, UserDeactivated):
        logger.warning(f"Interval job: Account {user_id} session is invalid. Stopping job.")
        # Don't reschedule, the bot is dead
    except Exception as e:
        logger.error(f"Error in online_interval_job for {user_id}: {e}\n{traceback.format_exc()}")
        # Reschedule for 5 minutes later on unknown error
        context.application.job_queue.run_once(
            online_interval_job, 
            300, 
            data={'user_id': user_id}, 
            name=f"interval_{user_id}"
        )
# --- END ONLINE INTERVAL JOB ---

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
