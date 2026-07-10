import os
import asyncio
import threading
import logging
from datetime import datetime
from zoneinfo import ZoneInfo
from telegram.ext import Application
from state import subscribed_users
from sheets import get_sheets_client, get_user_sheet, write_subscription_status

logger    = logging.getLogger(__name__)
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
SGT       = ZoneInfo("Asia/Singapore")


async def send_reminder(user_id, message):
    # Sends a reminder message to a single user
    try:
        application = Application.builder().token(BOT_TOKEN).build()
        await application.bot.send_message(
            chat_id=user_id, text=message, parse_mode="Markdown"
        )
        logger.info(f"Reminder sent to {user_id}")
    except Exception as e:
        logger.error(f"Reminder failed for {user_id}: {e}")


async def fire_reminder_async(message):
    # Sends reminder to all subscribed users concurrently
    if not subscribed_users:
        logger.info("No subscribed users — skipping reminder")
        return
    tasks = [send_reminder(user_id, message) for user_id in list(subscribed_users)]
    await asyncio.gather(*tasks)


async def cmd_subscribe(update, context):
    user_id = update.effective_user.id
    if user_id in subscribed_users:
        await update.message.reply_text("🔔 Already subscribed! Use /unsubscribe to turn off.")
        return
    subscribed_users.add(user_id)
    threading.Thread(
        target=write_subscription_status, args=(user_id, True), daemon=True
    ).start()
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
    threading.Thread(
        target=write_subscription_status, args=(user_id, False), daemon=True
    ).start()
    await update.message.reply_text(
        "🔕 *Unsubscribed from reminders.*\n\nUse /subscribe anytime to turn back on.",
        parse_mode="Markdown"
    )