import os
import logging
import asyncio
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from flask import Flask, request

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Hello, World!")

@app.route("/webhook", methods=["POST"])
def webhook():
    if not BOT_TOKEN:
        return "No token", 500

    data = request.get_json(force=True)

    async def process():
        application = Application.builder().token(BOT_TOKEN).build()
        application.add_handler(CommandHandler("start", start))
        await application.initialize()
        update = Update.de_json(data, application.bot)
        await application.process_update(update)

    asyncio.run(process())
    return "ok", 200

@app.route("/", methods=["GET"])
def index():
    token_status = "Token found" if BOT_TOKEN else "Token MISSING"
    return f"Bot is running! {token_status}", 200

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)