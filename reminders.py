import os
import asyncio
import threading
import schedule
import time
import logging
from datetime import datetime
from zoneinfo import ZoneInfo
from telegram.ext import Application
from state import subscribed_users
from sheets import get_sheets_client, get_user_sheet, write_subscription_status

logger   = logging.getLogger(__name__)
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
SGT      = ZoneInfo("Asia/Singapore")


async def send_reminder(user_id, message):
    try:
        application = Application.builder().token(BOT_TOKEN).build()
        await application.bot.send_message(chat_id=user_id, text=message, parse_mode="Markdown")
        logger.info(f"Reminder sent to {user_id}")
    except Exception as e:
        logger.error(f"Reminder failed for {user_id}: {e}")


def fire_reminder(message):
    if not subscribed_users:
        return
    for user_id in list(subscribed_users):
        asyncio.run(send_reminder(user_id, message))


def job_midday():
    now = datetime.now(SGT)
    if now.weekday() >= 5:
        return
    fire_reminder(
        "🍽️ *Afternoon Check-in!*\n\n"
        "Don't forget to log your lunch calories!\n"
        "Use /track to add them to today's total. 💪"
    )


def job_evening():
    now = datetime.now(SGT)
    if now.weekday() >= 5:
        return
    if now.weekday() == 4:  # Friday
        fire_reminder(
            "🍽️ *End of Day Check-in!*\n\n"
            "Don't forget to log your dinner calories!\n"
            "Use /track to add them to today's total.\n\n"
            "⚖️ *It's Friday — time for your weekly weigh-in!*\n"
            "Log your current weight with /updateweight to track your progress. 💪"
        )
    else:
        fire_reminder(
            "🍽️ *End of Day Check-in!*\n\n"
            "Don't forget to log your dinner calories!\n"
            "Use /track to add them to today's total. 💪"
        )


def run_scheduler():
    # ── TESTING: 15:35 SGT = 07:35 UTC ───────────────────────────
    schedule.every().day.at("07:35").do(job_midday)
    schedule.every().day.at("07:35").do(job_evening)
    # ── PRODUCTION (swap these in when done testing): ─────────────
    # schedule.every().day.at("06:00").do(job_midday)   # 14:00 SGT
    # schedule.every().day.at("12:30").do(job_evening)  # 20:30 SGT
    logger.info("Scheduler started")
    while True:
        schedule.run_pending()
        time.sleep(60)


def start_scheduler_thread():
    t = threading.Thread(target=run_scheduler, daemon=True)
    t.start()
    logger.info("Scheduler thread launched")


async def cmd_subscribe(update, context):
    user_id = update.effective_user.id
    if user_id in subscribed_users:
        await update.message.reply_text("🔔 Already subscribed! Use /unsubscribe to turn off.")
        return
    subscribed_users.add(user_id)
    threading.Thread(target=write_subscription_status, args=(user_id, True), daemon=True).start()
    await update.message.reply_text(
        "🔔 *Subscribed to daily reminders!*\n\n"
        "🍽️ *14:00* — After lunch check-in\n"
        "🍽️ *20:30* — After dinner check-in\n"
        "⚖️ *Fridays 20:30* — Weekly weigh-in reminder\n\n"
        "Monday to Friday only. Use /unsubscribe to stop.",
        parse_mode="Markdown"
    )


async def cmd_unsubscribe(update, context):
    user_id = update.effective_user.id
    if user_id not in subscribed_users:
        await update.message.reply_text("🔕 Not subscribed. Use /subscribe to turn on.")
        return
    subscribed_users.discard(user_id)
    threading.Thread(target=write_subscription_status, args=(user_id, False), daemon=True).start()
    await update.message.reply_text(
        "🔕 *Unsubscribed from reminders.*\n\nUse /subscribe anytime to turn back on.",
        parse_mode="Markdown"
    )