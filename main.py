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

# Build once at module load, not per-request
telegram_app = Application.builder().token(BOT_TOKEN).build()
telegram_app.add_handler(CommandHandler("start",        cmd_start))
telegram_app.add_handler(CommandHandler("register",     cmd_register))
telegram_app.add_handler(CommandHandler("user",         cmd_user))
telegram_app.add_handler(CommandHandler("updateweight", cmd_updateweight))
telegram_app.add_handler(CommandHandler("track",        cmd_track))
telegram_app.add_handler(CommandHandler("resettrack",   cmd_resettrack))
telegram_app.add_handler(CommandHandler("caloriegraph", cmd_caloriegraph))
telegram_app.add_handler(CommandHandler("weightgraph",  cmd_weightgraph))
telegram_app.add_handler(CommandHandler("help",         cmd_help))
telegram_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
telegram_app.add_handler(MessageHandler(filters.COMMAND, cmd_unknown))

_initialized = False


@app.route("/webhook", methods=["POST"])
def webhook():
    if not BOT_TOKEN:
        return "No token", 500
    data = request.get_json(force=True)

    async def process():
        global _initialized
        if not _initialized:
            await telegram_app.initialize()
            _initialized = True
        update = Update.de_json(data, telegram_app.bot)
        await telegram_app.process_update(update)

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