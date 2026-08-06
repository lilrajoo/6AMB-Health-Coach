import os
import asyncio
import logging
from datetime import datetime
from zoneinfo import ZoneInfo
from telegram.ext import Application
from sheets import get_sheets_client, get_todays_calories, read_data_rows
from helpers import calc_tdee

logger    = logging.getLogger(__name__)
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
SGT       = ZoneInfo("Asia/Singapore")

# Placeholder link for exercise infographic — fill this in when ready
EXERCISE_LINK = "https://your-exercise-infographic-link-here"


def get_all_user_ids():
    # Scans all tabs in the master sheet and returns a list of valid user IDs
    # Skips Master and _data tabs — all other tab names are Telegram user IDs
    try:
        client      = get_sheets_client()
        spreadsheet = client.open_by_key(os.environ.get("SHEETS_ID"))
        ids = []
        for sheet in spreadsheet.worksheets():
            if sheet.title.lower() in ("master", "_data"):
                continue
            try:
                int(sheet.title)  # valid user ID tabs are numeric
                ids.append(int(sheet.title))
            except ValueError:
                continue
        return ids
    except Exception as e:
        logger.error(f"Error fetching user IDs: {e}")
        return []


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
    # Sends reminder to all registered users concurrently
    # No subscription needed — all registered users receive reminders
    user_ids = get_all_user_ids()
    if not user_ids:
        logger.info("No registered users — skipping reminder")
        return
    tasks = [send_reminder(user_id, message) for user_id in user_ids]
    await asyncio.gather(*tasks)


async def fire_evening_reminder_async():
    # Evening reminder checks each user's calorie intake vs their TDEE
    # If they are over their target, adds an exercise nudge with infographic link
    user_ids = get_all_user_ids()
    if not user_ids:
        return

    now        = datetime.now(SGT)
    is_friday  = now.weekday() == 4

    try:
        client      = get_sheets_client()
        spreadsheet = client.open_by_key(os.environ.get("SHEETS_ID"))
    except Exception as e:
        logger.error(f"Sheets connection error in evening reminder: {e}")
        return

    async def send_to_user(user_id):
        try:
            sheet    = spreadsheet.worksheet(str(user_id))
            row1     = sheet.row_values(1)
            # Read profile: NAME | HEIGHT | AGE | GENDER | WEIGHT
            height   = float(row1[1]) if len(row1) > 1 and row1[1] else None
            age      = int(row1[2])   if len(row1) > 2 and row1[2] else None
            gender   = row1[3]        if len(row1) > 3 else None
            weight   = float(row1[4]) if len(row1) > 4 and row1[4] else None

            # Get today's calorie total from sheet
            today_cals = get_todays_calories(sheet)

            # Build the base message
            if is_friday:
                msg = (
                    "🍽️ *End of Day Check-in!*\n\n"
                    "Don't forget to log your dinner calories!\n"
                    "Use /track to add them to today's total.\n\n"
                    "⚖️ *It's Friday — time for your weekly weigh-in!*\n"
                    "Log your current weight with /updateweight to track your progress. 💪"
                )
            else:
                msg = (
                    "🍽️ *End of Day Check-in!*\n\n"
                    "Don't forget to log your dinner calories!\n"
                    "Use /track to add them to today's total. 💪"
                )

            # If profile complete and calories logged, check if over TDEE
            if height and age and gender and weight and today_cals > 0:
                tdee = calc_tdee(height, weight, age, gender)
                if today_cals > tdee:
                    excess = int(today_cals - tdee)
                    msg += (
                        f"\n\n🔥 *You're {excess} kcal over your target today!*\n"
                        f"Consider burning it off with some exercise.\n"
                        f"[View Exercise Infographic]({EXERCISE_LINK})"
                    )

            await send_reminder(user_id, msg)

        except Exception as e:
            logger.error(f"Evening reminder error for {user_id}: {e}")

    tasks = [send_to_user(user_id) for user_id in user_ids]
    await asyncio.gather(*tasks)