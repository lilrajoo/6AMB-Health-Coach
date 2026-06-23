import os
import logging
import asyncio
from flask import Flask, request
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters

# ── Logging setup ─────────────────────────────────────────────────
# Prints logs to Cloud Run console so you can debug issues live
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ── Flask app setup ───────────────────────────────────────────────
# Flask is the web server that receives incoming webhook requests from Telegram
app = Flask(__name__)

# ── Bot token ─────────────────────────────────────────────────────
# Reads the bot token from the environment variable injected by Cloud Run
# This is the token you stored in GCP Secret Manager as "tele-bot-token"
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")

# ── In-memory state store ─────────────────────────────────────────
# These dictionaries store each user's data using their Telegram user ID as the key
# WARNING: This data is lost if Cloud Run restarts the container (e.g. after inactivity)
# Example: user_name[123456789] = "John Doe"
user_state    = {}  # Tracks what step of a conversation the user is on
user_name     = {}  # Stores the user's registered name
user_height   = {}  # Stores the user's height in cm
user_weight   = {}  # Stores the user's weight in kg
user_calories = {}  # Stores the user's total calorie intake for the day

# ── BMI calculator helper ─────────────────────────────────────────
# Takes height in cm and weight in kg, returns (bmi_value, category_string)
# Called whenever we need to display or recalculate a user's BMI
def calc_bmi(height_cm, weight_kg):
    bmi = weight_kg / ((height_cm / 100) ** 2)
    # Determine category based on standard BMI ranges
    if bmi < 18.5:
        cat = "🔵 Underweight"
    elif bmi < 25:
        cat = "🟢 Normal weight"
    elif bmi < 30:
        cat = "🟡 Overweight"
    else:
        cat = "🔴 Obese"
    return round(bmi, 1), cat


# ════════════════════════════════════════════════════════════════════
#  COMMAND HANDLERS
#  These functions are triggered when a user sends a specific command
#  e.g. sending "/start" triggers cmd_start()
# ════════════════════════════════════════════════════════════════════

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /start — Welcome message shown when user first opens the bot
    Also resets the user's conversation state back to idle
    """
    user_id = update.effective_user.id
    user_state[user_id] = "idle"  # Reset any ongoing conversation
    await update.message.reply_text(
        "👋 *Welcome to the BMI & Calorie Tracker Bot!*\n\n"
        "Available commands:\n\n"
        "📋 /register — Register your Name, Height & Weight\n"
        "👤 /user — View your profile & BMI\n"
        "⚖️ /updateweight — Update your weight & recalculate BMI\n"
        "🍽️ /track — Log caloric intake (adds to daily total)\n"
        "🔄 /resettrack — Reset today's calorie total to zero",
        parse_mode="Markdown"  # Enables bold/italic formatting in the message
    )

async def cmd_register(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /register — Starts the registration conversation flow
    Sets state to 'awaiting_name' so the next message is treated as their name
    """
    user_id = update.effective_user.id
    user_state[user_id] = "awaiting_name"  # Next message will be captured as name
    await update.message.reply_text(
        "📝 *Registration started!*\n\nPlease enter your rank & full name:",
        parse_mode="Markdown"
    )

async def cmd_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /user — Displays the user's saved profile and current BMI
    Shows an error if the user hasn't registered yet
    """
    user_id = update.effective_user.id
    name   = user_name.get(user_id)
    height = user_height.get(user_id)
    weight = user_weight.get(user_id)

    # Check if the user has completed registration before showing profile
    if not name or not height or not weight:
        await update.message.reply_text("⚠️ No profile found. Please use /register first.")
        return

    bmi, cat = calc_bmi(height, weight)
    await update.message.reply_text(
        f"👤 *Your Profile*\n\n"
        f"Name: {name}\n"
        f"Height: {height} cm\n"
        f"Weight: {weight} kg\n"
        f"BMI: {bmi} — {cat}",
        parse_mode="Markdown"
    )

async def cmd_updateweight(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /updateweight — Lets the user update their weight and recalculate BMI
    Requires the user to have registered first (needs height for BMI calc)
    Sets state to 'awaiting_update_weight' so the next message is their new weight
    """
    user_id = update.effective_user.id

    # Block if user hasn't registered — we need their height for BMI
    if not user_name.get(user_id):
        await update.message.reply_text("⚠️ No profile found. Please use /register first.")
        return

    user_state[user_id] = "awaiting_update_weight"  # Next message will be new weight
    await update.message.reply_text(
        "⚖️ Enter your *new weight in kilograms* (e.g. 68):",
        parse_mode="Markdown"
    )

async def cmd_track(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /track — Shows current calorie total and prompts user to log more calories
    Sets state to 'awaiting_calories' so the next message is treated as calories
    """
    user_id = update.effective_user.id
    total = user_calories.get(user_id, 0)  # Default to 0 if no calories logged yet
    user_state[user_id] = "awaiting_calories"  # Next message will be calorie amount
    await update.message.reply_text(
        f"🍽️ *Calorie Tracker*\n\n"
        f"Current daily total: *{total} kcal*\n\n"
        f"Enter calories to add (e.g. 350):",
        parse_mode="Markdown"
    )

async def cmd_resettrack(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /resettrack — Resets the user's daily calorie total back to zero
    Does not require any follow-up message, executes immediately
    """
    user_id = update.effective_user.id
    user_calories[user_id] = 0  # Wipe the calorie counter for this user
    await update.message.reply_text(
        "🔄 Calorie total reset to *0 kcal*. Use /track to start logging again.",
        parse_mode="Markdown"
    )


# ════════════════════════════════════════════════════════════════════
#  CONVERSATION STATE HANDLER
#  Handles all plain text messages (not commands)
#  Checks the user's current state to know what input we're expecting
#  This is how multi-step flows like registration work
# ════════════════════════════════════════════════════════════════════

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text    = update.message.text.strip()
    state   = user_state.get(user_id, "idle")  # Default to idle if no state set

    # ── Step 1 of registration: capture name ──────────────────────
    if state == "awaiting_name":
        user_name[user_id]  = text               # Save whatever they typed as their name
        user_state[user_id] = "awaiting_height"  # Move to next step
        await update.message.reply_text(
            "📏 Enter your *height in centimetres* (e.g. 175):",
            parse_mode="Markdown"
        )

    # ── Step 2 of registration: capture and validate height ───────
    elif state == "awaiting_height":
        try:
            h = float(text)
            if h < 50 or h > 300:  # Reject obviously wrong values
                raise ValueError
            user_height[user_id] = h
            user_state[user_id]  = "awaiting_weight"  # Move to next step
            await update.message.reply_text(
                "⚖️ Enter your *weight in kilograms* (e.g. 70):",
                parse_mode="Markdown"
            )
        except ValueError:
            # Don't change state — keep asking for height until valid
            await update.message.reply_text(
                "⚠️ Invalid height. Enter a number between 50 and 300 cm (e.g. 175):"
            )

    # ── Step 3 of registration: capture weight and show BMI result ─
    elif state == "awaiting_weight":
        try:
            w = float(text)
            if w < 10 or w > 500:  # Reject obviously wrong values
                raise ValueError
            user_weight[user_id] = w
            user_state[user_id]  = "idle"  # Registration complete, back to idle
            name   = user_name.get(user_id, "N/A")
            height = user_height.get(user_id)
            bmi, cat = calc_bmi(height, w)
            await update.message.reply_text(
                f"✅ *Registration Complete!*\n\n"
                f"Name: {name}\n"
                f"Height: {height} cm\n"
                f"Weight: {w} kg\n"
                f"BMI: {bmi} — {cat}",
                parse_mode="Markdown"
            )
        except ValueError:
            # Don't change state — keep asking for weight until valid
            await update.message.reply_text(
                "⚠️ Invalid weight. Enter a number between 10 and 500 kg (e.g. 70):"
            )

    # ── Update weight flow: capture new weight and recalculate BMI ─
    elif state == "awaiting_update_weight":
        try:
            w = float(text)
            if w < 10 or w > 500:
                raise ValueError
            user_weight[user_id] = w            # Overwrite old weight
            user_state[user_id]  = "idle"       # Back to idle after update
            name   = user_name.get(user_id, "N/A")
            height = user_height.get(user_id)
            bmi, cat = calc_bmi(height, w)      # Recalculate BMI with new weight
            await update.message.reply_text(
                f"✅ *Weight Updated!*\n\n"
                f"Name: {name}\n"
                f"Height: {height} cm\n"
                f"Weight: {w} kg\n"
                f"BMI: {bmi} — {cat}",
                parse_mode="Markdown"
            )
        except ValueError:
            await update.message.reply_text(
                "⚠️ Invalid weight. Enter a number between 10 and 500 kg (e.g. 70):"
            )

    # ── Calorie tracking flow: add calories to daily total ─────────
    elif state == "awaiting_calories":
        try:
            cal = float(text)
            if cal < 0:  # Can't log negative calories
                raise ValueError
            prev  = user_calories.get(user_id, 0)
            total = prev + cal                      # Add to running daily total
            user_calories[user_id] = total
            user_state[user_id]    = "idle"         # Back to idle after logging

            # Generate a contextual note based on total intake so far today
            if total == 0:
                note = "No intake logged yet."
            elif total < 1200:
                note = "⚠️ Very low — consider eating more."
            elif total < 1800:
                note = "✅ Light intake range."
            elif total < 2500:
                note = "✅ Moderate intake range."
            elif total < 3200:
                note = "🟡 High intake — monitor if on a cut."
            else:
                note = "🔴 Very high intake today."

            await update.message.reply_text(
                f"🍽️ *Calorie Log Updated*\n\n"
                f"Total today: *{total} kcal*\n"
                f"{note}\n\n"
                f"Use /track to add more or /resettrack to reset.",
                parse_mode="Markdown"
            )
        except ValueError:
            await update.message.reply_text(
                "⚠️ Invalid entry. Please enter a positive number for calories (e.g. 350):"
            )

    # ── Fallback: user sent a message with no active conversation ──
    else:
        await update.message.reply_text(
            "❓ Unknown command. Use /start to see all available commands."
        )


# ════════════════════════════════════════════════════════════════════
#  FLASK ROUTES
#  These are the HTTP endpoints that Cloud Run exposes
# ════════════════════════════════════════════════════════════════════

@app.route("/webhook", methods=["POST"])
def webhook():
    """
    /webhook — The main endpoint Telegram sends all updates to
    Every time a user messages the bot, Telegram POSTs the update here
    We build a fresh bot application, register all handlers, then process it
    """
    if not BOT_TOKEN:
        return "No token", 500

    data = request.get_json(force=True)  # Parse the incoming Telegram update as JSON

    async def process():
        # Build the bot application with our token
        application = Application.builder().token(BOT_TOKEN).build()

        # Register all command handlers (these match /command messages)
        application.add_handler(CommandHandler("start",        cmd_start))
        application.add_handler(CommandHandler("register",     cmd_register))
        application.add_handler(CommandHandler("user",         cmd_user))
        application.add_handler(CommandHandler("updateweight", cmd_updateweight))
        application.add_handler(CommandHandler("track",        cmd_track))
        application.add_handler(CommandHandler("resettrack",   cmd_resettrack))

        # Register the plain text handler (catches everything that isn't a command)
        # filters.TEXT & ~filters.COMMAND means: text messages that are NOT commands
        application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

        await application.initialize()
        update = Update.de_json(data, application.bot)  # Convert raw JSON to Update object
        await application.process_update(update)        # Run through the matching handler

    asyncio.run(process())
    return "ok", 200  # Must return 200 so Telegram knows we received the update


@app.route("/", methods=["GET"])
def index():
    """
    / — Health check endpoint
    Visit your Cloud Run URL in the browser to confirm the bot is running
    Also shows whether the secret token was successfully injected
    """
    token_status = "Token found" if BOT_TOKEN else "Token MISSING"
    return f"Bot is running! {token_status}", 200


# ── Local development entry point ─────────────────────────────────
# This block only runs when you execute `python bot.py` directly
# In production, gunicorn starts the app instead (see Dockerfile CMD)
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)