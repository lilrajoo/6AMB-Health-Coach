import os
import asyncio
import logging
from flask import Flask, request
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters
from handlers import (cmd_start, cmd_register, cmd_user, cmd_updateweight,
                      cmd_track, cmd_resettrack, cmd_caloriegraph,
                      cmd_weightgraph, cmd_unknown, handle_message)
from reminders import (cmd_subscribe, cmd_unsubscribe,
                       start_scheduler_thread)
from sheets import load_subscriptions_from_sheets

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
        application.add_handler(CommandHandler("subscribe",    cmd_subscribe))
        application.add_handler(CommandHandler("unsubscribe",  cmd_unsubscribe))
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


# Load subscriptions and start reminder scheduler on startup
load_subscriptions_from_sheets()
start_scheduler_thread()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))