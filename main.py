import os
import asyncio
import logging
from flask import Flask, request
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters
from handlers import (cmd_start, cmd_register, cmd_user, cmd_updateweight,
                      cmd_track, cmd_resettrack, cmd_caloriegraph,
                      cmd_weightgraph, cmd_unknown, handle_message)
from reminders import fire_reminder_async, build_midday_message, fire_evening_reminder_async
from handlers import cmd_help


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app       = Flask(__name__)
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
SHEETS_CREDS_RAW = os.environ.get("SHEETS_CREDENTIALS")
SHEETS_ID        = os.environ.get("SHEETS_ID")


@app.route("/webhook", methods=["POST"])
def webhook():
    if not BOT_TOKEN:
        return "No token", 500
    data = request.get_json(force=True)

    async def process():
        application = Application.builder().token(BOT_TOKEN).build()
        application.add_handler(CommandHandler("start",        cmd_start))
        application.add_handler(CommandHandler("register",     cmd_register))
        application.add_handler(CommandHandler("user",         cmd_user))
        application.add_handler(CommandHandler("updateweight", cmd_updateweight))
        application.add_handler(CommandHandler("track",        cmd_track))
        application.add_handler(CommandHandler("resettrack",   cmd_resettrack))
        application.add_handler(CommandHandler("caloriegraph", cmd_caloriegraph))
        application.add_handler(CommandHandler("weightgraph",  cmd_weightgraph))
        application.add_handler(CommandHandler("help", cmd_help))
        application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
        application.add_handler(MessageHandler(filters.COMMAND, cmd_unknown))
        await application.initialize()
        update = Update.de_json(data, application.bot)
        await application.process_update(update)

    asyncio.run(process())
    return "ok", 200


@app.route("/", methods=["GET"])
def index():
    token_status  = "Token found"              if BOT_TOKEN        else "Token MISSING"
    sheets_status = "Sheets credentials found" if SHEETS_CREDS_RAW else "Sheets credentials MISSING"
    sheets_id     = f"Sheet ID: {SHEETS_ID}"   if SHEETS_ID        else "Sheet ID MISSING"
    return f"Bot is running! {token_status} | {sheets_status} | {sheets_id}", 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))

@app.route("/remind", methods=["POST"])
def remind():
    data          = request.get_json(force=True) or {}
    reminder_type = data.get("type", "")

    if reminder_type == "midday":
        asyncio.run(fire_reminder_async(build_midday_message()))
    elif reminder_type == "evening":
        asyncio.run(fire_evening_reminder_async())
    else:
        return "Unknown reminder type", 400

    return "ok", 200

@app.route("/test-infographics", methods=["POST"])
def test_infographics():
    from reminders import send_exercise_infographics
    data = request.get_json(force=True) or {}
    test_user_id = data.get("user_id")
    if not test_user_id:
        return "Missing user_id", 400
    asyncio.run(send_exercise_infographics(int(test_user_id)))
    return "sent", 200

@app.route("/test-full-evening", methods=["POST"])
def test_full_evening():
    from reminders import _build_evening_base_message, send_reminder, send_exercise_infographics
    data = request.get_json(force=True) or {}
    test_user_id = data.get("user_id")
    force_thursday = data.get("thursday", False)
    force_over_target = data.get("over_target", False)

    if not test_user_id:
        return "Missing user_id", 400

    async def run():
        msg = _build_evening_base_message(is_thursday=force_thursday)
        if force_over_target:
            msg += (
                "\n\n🔥 *You're 350 kcal over your target today!*\n"
                "Consider burning it off with some exercise. 👇"
            )
        await send_reminder(int(test_user_id), msg)
        if force_over_target:
            await send_exercise_infographics(int(test_user_id))

    asyncio.run(run())
    return "sent", 200