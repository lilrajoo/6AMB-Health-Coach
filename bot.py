import os
import logging
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from flask import Flask, request

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)
BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]

application = Application.builder().token(BOT_TOKEN).build()

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Hello, World!")

application.add_handler(CommandHandler("start", start))

@app.route("/webhook", methods=["POST"])
def webhook():
    import asyncio
    from telegram import Update

    data = request.get_json(force=True)
    update = Update.de_json(data, application.bot)

    async def process():
        await application.initialize()
        await application.process_update(update)

    asyncio.run(process())
    return "ok", 200

@app.route("/", methods=["GET"])
def index():
    return "Bot is running!", 200

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)