import os
import io
import json
import logging
import asyncio
from datetime import datetime, timedelta

import gspread
import matplotlib
matplotlib.use("Agg")  # Use non-interactive backend so matplotlib works without a display
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import numpy as np
from flask import Flask, request
from google.oauth2.service_account import Credentials
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters

# ── Logging setup ─────────────────────────────────────────────────
# Prints logs to Cloud Run console so you can debug issues live
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ── Flask app setup ───────────────────────────────────────────────
# Flask is the web server that receives incoming webhook requests from Telegram
app = Flask(__name__)

# ── Environment variables ─────────────────────────────────────────
# All three are injected by Cloud Run from GCP Secret Manager at runtime
BOT_TOKEN        = os.environ.get("TELEGRAM_BOT_TOKEN")   # Telegram bot token
SHEETS_CREDS_RAW = os.environ.get("SHEETS_CREDENTIALS")   # Full JSON string of service account key
SHEETS_ID        = os.environ.get("SHEETS_ID")            # Google Sheet file ID

# ── In-memory state store ─────────────────────────────────────────
# These dictionaries store each user's current conversation state and profile
# data in memory for fast access during active conversations.
# WARNING: This is lost if Cloud Run restarts — profile data is permanently
# stored in Google Sheets and reloaded when needed
user_state    = {}  # Tracks what step of a conversation the user is on
user_name     = {}  # Stores the user's registered name
user_height   = {}  # Stores the user's height in cm
user_weight   = {}  # Stores the user's weight in kg
user_calories = {}  # Stores the user's running calorie total for today
user_age    = {}  # Stores the user's age
user_gender = {}  # Stores the user's gender ("male" or "female")


# ════════════════════════════════════════════════════════════════════
#  GOOGLE SHEETS HELPERS
#  These functions handle all read and write operations to Google Sheets
#  Each user has their own tab named after their Telegram user ID
# ════════════════════════════════════════════════════════════════════

def get_sheets_client():
    """
    Authenticates with Google Sheets using the service account credentials
    stored in the SHEETS_CREDENTIALS environment variable.
    Returns a gspread client that can open and edit sheets.
    """
    creds_dict = json.loads(SHEETS_CREDS_RAW)  # Parse JSON string into dict
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive"
    ]
    creds  = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    client = gspread.authorize(creds)
    return client


def get_user_sheet(client, user_id):
    """
    Opens the master Google Sheet and returns the tab for this specific user.
    If the user's tab doesn't exist yet, creates it with the correct headers.
    Tab name is the user's Telegram user ID as a string (e.g. "123456789").
    """
    spreadsheet = client.open_by_key(SHEETS_ID)
    tab_name    = str(user_id)

    try:
        # Try to find an existing tab for this user
        worksheet = spreadsheet.worksheet(tab_name)
    except gspread.exceptions.WorksheetNotFound:
        # No tab found — create a fresh one for this user
        worksheet = spreadsheet.add_worksheet(title=tab_name, rows=1000, cols=5)

        # Row 1: profile header — name is stored in B1
        worksheet.update("A1:B1", [["NAME", ""]])

        # Row 2: blank spacer row

        # Row 3: column headers for the data log
        worksheet.update("A3:C3", [["DATE", "TYPE", "VALUE"]])

    return worksheet


def write_profile_name(worksheet, name):
    """
    Writes the user's name into cell B1 of their sheet tab.
    Called once during /register and updated if name ever changes.
    """
    worksheet.update("B1", [[name]])


def append_data_row(worksheet, entry_type, value):
    """
    Appends a new row to the user's data log with today's date.
    entry_type is either "calories" or "weight".
    Finds the first empty row after row 3 and writes there.
    """
    today = datetime.now().strftime("%Y-%m-%d")  # Format: 2025-06-01

    # Get all existing data to find the next empty row
    all_values = worksheet.get_all_values()
    next_row   = len(all_values) + 1  # Next empty row index (1-based)

    # Make sure we never write above row 4 (rows 1-3 are profile + headers)
    if next_row < 4:
        next_row = 4

    worksheet.update(f"A{next_row}:C{next_row}", [[today, entry_type, value]])


def read_data_rows(worksheet, entry_type):
    """
    Reads all data log rows for a specific entry type ("calories" or "weight").
    Returns a list of (date_string, value) tuples, oldest first.
    Skips the profile row and header row (rows 1-3).
    """
    all_values = worksheet.get_all_values()
    rows       = []

    # Start from index 3 (row 4 in Sheets) to skip profile and headers
    for row in all_values[3:]:
        if len(row) >= 3 and row[1].strip().lower() == entry_type:
            try:
                date_str = row[0].strip()
                value    = float(row[2].strip())
                rows.append((date_str, value))
            except ValueError:
                # Skip any rows with malformed data
                continue

    return rows


# ════════════════════════════════════════════════════════════════════
#  BMI HELPER
# ════════════════════════════════════════════════════════════════════

def calc_bmi(height_cm, weight_kg):
    """
    Calculates BMI from height (cm) and weight (kg).
    Returns (bmi_value rounded to 1dp, category string with emoji).
    """
    bmi = weight_kg / ((height_cm / 100) ** 2)
    if bmi < 18.5:
        cat = "🔵 Underweight"
    elif bmi < 25:
        cat = "🟢 Normal weight"
    elif bmi < 30:
        cat = "🟡 Overweight"
    else:
        cat = "🔴 Obese"
    return round(bmi, 1), cat

def calc_tdee(height_cm, weight_kg, age, gender):
    """
    Calculates Total Daily Energy Expenditure (TDEE) using the
    Mifflin-St Jeor Equation for Basal Metabolic Rate (BMR),
    assuming a sedentary activity level (x1.2) as a baseline.

    Formula:
        Male:   BMR = (10 x weight kg) + (6.25 x height cm) - (5 x age) + 5
        Female: BMR = (10 x weight kg) + (6.25 x height cm) - (5 x age) - 161

    TDEE = BMR x 1.2 (sedentary multiplier)

    Returns the TDEE as a rounded integer (kcal/day).
    """
    if gender.lower() == "male":
        bmr = (10 * weight_kg) + (6.25 * height_cm) - (5 * age) + 5
    else:
        bmr = (10 * weight_kg) + (6.25 * height_cm) - (5 * age) - 161

    tdee = bmr * 1.2  # Sedentary activity multiplier
    return round(tdee)


def get_calorie_note(total, tdee):
    """
    Returns a contextual message comparing the user's total calorie
    intake today against their personally calculated TDEE.

    Thresholds are relative to the user's TDEE rather than fixed numbers,
    so feedback is meaningful regardless of body size.
    """
    ratio = total / tdee  # How much of their daily need they've consumed

    if ratio < 0.5:
        return f"⚠️ Very low — under 50% of your daily target ({tdee} kcal)."
    elif ratio < 0.75:
        return f"🟡 Below target — aim for around {tdee} kcal today."
    elif ratio <= 1.0:
        return f"✅ On track — within your daily target of {tdee} kcal."
    elif ratio <= 1.2:
        return f"🟡 Slightly over your daily target of {tdee} kcal."
    else:
        return f"🔴 Significantly over your daily target of {tdee} kcal."

# ════════════════════════════════════════════════════════════════════
#  GRAPH HELPERS
#  These functions build the matplotlib charts and return them as
#  in-memory byte buffers ready to send directly to Telegram
# ════════════════════════════════════════════════════════════════════

def build_calorie_graph(calorie_rows):
    """
    Builds a bar chart of average daily calories per week for up to
    the last 4 complete weeks plus the current (possibly incomplete) week.

    calorie_rows: list of ("YYYY-MM-DD", calories_float) tuples

    Returns a BytesIO buffer containing the PNG image, or None if no data.
    """
    if not calorie_rows:
        return None

    # ── Work out the week boundaries ──────────────────────────────
    # "Week start" = Monday of that week
    today      = datetime.now().date()
    # Monday of the current week
    this_monday = today - timedelta(days=today.weekday())

    # Build a list of week start dates: current week + up to 4 previous weeks
    # Index 0 = oldest week, index 4 = current week
    week_starts = [this_monday - timedelta(weeks=i) for i in range(4, -1, -1)]

    # ── Bucket each calorie entry into its week ───────────────────
    # week_data[i] = list of calorie values logged in week i
    week_data = {ws: [] for ws in week_starts}

    for date_str, calories in calorie_rows:
        try:
            entry_date = datetime.strptime(date_str, "%Y-%m-%d").date()
        except ValueError:
            continue

        # Find which week this entry belongs to
        for ws in week_starts:
            week_end = ws + timedelta(days=6)
            if ws <= entry_date <= week_end:
                week_data[ws].append(calories)
                break

    # ── Calculate average daily calories per week ─────────────────
    # For complete weeks: sum all calories / 7 days
    # For current (incomplete) week: sum / number of days elapsed so far
    week_labels  = []
    week_avgs    = []
    has_data     = []  # Track which weeks actually have data

    for i, ws in enumerate(week_starts):
        values = week_data[ws]
        label  = f"{ws.strftime('%d %b')}"  # e.g. "02 Jun"
        week_labels.append(label)

        if not values:
            # No data for this week — append None to leave gap in chart
            week_avgs.append(None)
            has_data.append(False)
        else:
            if i == len(week_starts) - 1:
                # Current week — divide by days elapsed (at least 1)
                days_elapsed = today.weekday() + 1  # Monday=0 so +1 gives days
                avg = sum(values) / days_elapsed
            else:
                # Complete week — divide by 7
                avg = sum(values) / 7
            week_avgs.append(round(avg, 1))
            has_data.append(True)

    # ── Build the chart ───────────────────────────────────────────
    fig, ax = plt.subplots(figsize=(10, 6))
    fig.patch.set_facecolor("#1e1e2e")   # Dark background for the figure
    ax.set_facecolor("#2a2a3e")          # Slightly lighter background for the plot area

    x_positions = list(range(len(week_labels)))

    # Draw bars only for weeks that have data
    for i, (avg, label) in enumerate(zip(week_avgs, week_labels)):
        if avg is not None:
            bar = ax.bar(i, avg, color="#7c6af7", width=0.6, zorder=3)
            # Add the value label on top of each bar
            ax.text(i, avg + 20, f"{avg:.0f}", ha="center", va="bottom",
                    color="white", fontsize=10, fontweight="bold")

    # ── Add trend line ────────────────────────────────────────────
    # Only draw trend line through weeks that actually have data
    trend_x = [i for i, h in enumerate(has_data) if h]
    trend_y = [avg for avg, h in zip(week_avgs, has_data) if h]

    if len(trend_x) >= 2:
        # numpy polyfit calculates the best-fit straight line
        z    = np.polyfit(trend_x, trend_y, 1)
        p    = np.poly1d(z)
        # Draw the trend line only across weeks that have data
        trend_x_range = np.linspace(trend_x[0], trend_x[-1], 100)
        ax.plot(trend_x_range, p(trend_x_range),
                color="#f7a76a", linewidth=2, linestyle="--",
                label="Trend", zorder=4)
        ax.legend(facecolor="#2a2a3e", labelcolor="white", fontsize=10)

    # ── Styling ───────────────────────────────────────────────────
    ax.set_xticks(x_positions)
    ax.set_xticklabels(week_labels, color="white", fontsize=10)
    ax.set_ylabel("Avg Daily Calories (kcal)", color="white", fontsize=11)
    ax.set_xlabel("Week Starting", color="white", fontsize=11)
    ax.set_title("Average Daily Calories Per Week\n(Last 4 Weeks + Current)",
                 color="white", fontsize=13, fontweight="bold", pad=15)
    ax.tick_params(colors="white")
    ax.yaxis.label.set_color("white")
    ax.spines["bottom"].set_color("#555577")
    ax.spines["left"].set_color("#555577")
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.grid(axis="y", color="#555577", linestyle="--", alpha=0.5, zorder=0)
    plt.tight_layout()

    # ── Save to buffer ────────────────────────────────────────────
    # Write the chart to an in-memory buffer instead of a file on disk
    buf = io.BytesIO()
    plt.savefig(buf, format="png", dpi=150, bbox_inches="tight")
    plt.close(fig)   # Free memory
    buf.seek(0)      # Rewind buffer to start so Telegram can read it
    return buf


def build_weight_graph(weight_rows):
    """
    Builds a line chart of the last 5 weight entries.
    If fewer than 5 exist, plots however many are available.
    Adds a trend line if there are at least 2 data points.

    weight_rows: list of ("YYYY-MM-DD", weight_float) tuples

    Returns a BytesIO buffer containing the PNG image, or None if no data.
    """
    if not weight_rows:
        return None

    # Take only the last 5 entries
    recent = weight_rows[-5:]

    # Parse dates and weights
    dates   = []
    weights = []
    for date_str, w in recent:
        try:
            dates.append(datetime.strptime(date_str, "%Y-%m-%d"))
            weights.append(w)
        except ValueError:
            continue

    if not dates:
        return None

    # ── Build the chart ───────────────────────────────────────────
    fig, ax = plt.subplots(figsize=(10, 6))
    fig.patch.set_facecolor("#1e1e2e")
    ax.set_facecolor("#2a2a3e")

    # Plot the weight line with circle markers at each data point
    ax.plot(dates, weights,
            color="#7c6af7", linewidth=2.5, marker="o",
            markersize=8, markerfacecolor="#f7a76a",
            markeredgecolor="white", markeredgewidth=1.5,
            zorder=3, label="Weight")

    # Add weight value labels above each data point
    for d, w in zip(dates, weights):
        ax.text(d, w + 0.3, f"{w} kg", ha="center", va="bottom",
                color="white", fontsize=10, fontweight="bold")

    # ── Add trend line ────────────────────────────────────────────
    if len(dates) >= 2:
        # Convert dates to numeric values for polyfit
        x_numeric = mdates.date2num(dates)
        z = np.polyfit(x_numeric, weights, 1)
        p = np.poly1d(z)

        # Draw the trend line across the full date range
        x_range    = np.linspace(x_numeric[0], x_numeric[-1], 100)
        trend_dates = mdates.num2date(x_range)
        ax.plot(trend_dates, p(x_range),
                color="#f7a76a", linewidth=2, linestyle="--",
                label="Trend", zorder=4)

    # ── Styling ───────────────────────────────────────────────────
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%d %b"))
    plt.xticks(rotation=30, ha="right", color="white", fontsize=10)
    ax.set_ylabel("Weight (kg)", color="white", fontsize=11)
    ax.set_xlabel("Date", color="white", fontsize=11)
    ax.set_title("Last 5 Weight Entries",
                 color="white", fontsize=13, fontweight="bold", pad=15)
    ax.tick_params(colors="white")
    ax.spines["bottom"].set_color("#555577")
    ax.spines["left"].set_color("#555577")
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.grid(axis="y", color="#555577", linestyle="--", alpha=0.5, zorder=0)
    ax.legend(facecolor="#2a2a3e", labelcolor="white", fontsize=10)
    plt.tight_layout()

    # ── Save to buffer ────────────────────────────────────────────
    buf = io.BytesIO()
    plt.savefig(buf, format="png", dpi=150, bbox_inches="tight")
    plt.close(fig)
    buf.seek(0)
    return buf


# ════════════════════════════════════════════════════════════════════
#  COMMAND HANDLERS
#  These functions are triggered when a user sends a specific command
# ════════════════════════════════════════════════════════════════════

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /start — Welcome message, resets conversation state to idle
    """
    user_id = update.effective_user.id
    user_state[user_id] = "idle"
    await update.message.reply_text(
        "👋 *Welcome to the BMI & Calorie Tracker Bot!*\n\n"
        "Available commands:\n\n"
        "📋 /register — Register your Name, Height & Weight\n"
        "👤 /user — View your profile & BMI\n"
        "⚖️ /updateweight — Update your weight & recalculate BMI\n"
        "🍽️ /track — Log caloric intake (adds to daily total)\n"
        "🔄 /resettrack — Reset today's calorie total to zero\n"
        "📊 /caloriegraph — Weekly average calorie chart\n"
        "📉 /weightgraph — Last 5 weight entries chart",
        parse_mode="Markdown"
    )


async def cmd_register(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /register — Starts the registration conversation flow
    Now collects: name → height → weight → age → gender
    Sets state to awaiting_name so the next message is treated as their name
    """
    user_id = update.effective_user.id
    user_state[user_id] = "awaiting_name"
    await update.message.reply_text(
        "📝 *Registration started!*\n\nPlease enter your rank & full name:",
        parse_mode="Markdown"
    )


async def cmd_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /user — Displays the user's saved profile, BMI and daily calorie target
    Now includes age, gender and TDEE in the profile output
    """
    user_id = update.effective_user.id
    name   = user_name.get(user_id)
    height = user_height.get(user_id)
    weight = user_weight.get(user_id)
    age    = user_age.get(user_id)
    gender = user_gender.get(user_id)

    # If not in memory (e.g. after a restart), try loading from sheet
    if not name or not height or not weight:
        try:
            client    = get_sheets_client()
            worksheet = get_user_sheet(client, user_id)
            all_vals  = worksheet.get_all_values()

            # Name is in B1
            if len(all_vals) > 0 and len(all_vals[0]) > 1:
                name = all_vals[0][1]
                user_name[user_id] = name

            # Most recent weight entry from data log
            weight_rows = read_data_rows(worksheet, "weight")
            if weight_rows:
                user_weight[user_id] = weight_rows[-1][1]
                weight = weight_rows[-1][1]
        except Exception as e:
            logger.error(f"Sheet read error for /user: {e}")

    if not name or not height or not weight or not age or not gender:
        await update.message.reply_text("⚠️ No profile found. Please use /register first.")
        return

    bmi, cat = calc_bmi(height, weight)
    tdee     = calc_tdee(height, weight, age, gender)

    await update.message.reply_text(
        f"👤 *Your Profile*\n\n"
        f"Name: {name}\n"
        f"Age: {age}\n"
        f"Gender: {gender.capitalize()}\n"
        f"Height: {height} cm\n"
        f"Weight: {weight} kg\n"
        f"BMI: {bmi} — {cat}\n"
        f"Daily Calorie Target: {tdee} kcal",
        parse_mode="Markdown"
    )

async def cmd_updateweight(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /updateweight — Prompts the user to enter a new weight
    Requires registration first so we have their height for BMI
    """
    user_id = update.effective_user.id
    if not user_name.get(user_id):
        await update.message.reply_text("⚠️ No profile found. Please use /register first.")
        return
    user_state[user_id] = "awaiting_update_weight"
    await update.message.reply_text(
        "⚖️ Enter your *new weight in kilograms* (e.g. 68):",
        parse_mode="Markdown"
    )


async def cmd_track(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /track — Shows current daily calorie total and prompts to log more
    """
    user_id = update.effective_user.id
    total   = user_calories.get(user_id, 0)
    user_state[user_id] = "awaiting_calories"
    await update.message.reply_text(
        f"🍽️ *Calorie Tracker*\n\n"
        f"Current daily total: *{total} kcal*\n\n"
        f"Enter calories to add (e.g. 350):",
        parse_mode="Markdown"
    )


async def cmd_resettrack(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /resettrack — Resets in-memory daily calorie total to zero
    Does NOT delete sheet history — all past entries are preserved
    """
    user_id = update.effective_user.id
    user_calories[user_id] = 0
    await update.message.reply_text(
        "🔄 Calorie total reset to *0 kcal*. Use /track to start logging again.",
        parse_mode="Markdown"
    )


async def cmd_caloriegraph(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /caloriegraph — Generates and sends a bar chart of average daily calories
    per week for the last 4 complete weeks plus the current week.
    Reads calorie history from the user's Google Sheet tab.
    """
    user_id = update.effective_user.id

    # Tell the user we're working on it — graph generation takes a moment
    await update.message.reply_text("📊 Generating your calorie graph, please wait...")

    try:
        client        = get_sheets_client()
        worksheet     = get_user_sheet(client, user_id)
        calorie_rows  = read_data_rows(worksheet, "calories")

        if not calorie_rows:
            await update.message.reply_text(
                "⚠️ No calorie data found. Use /track to start logging your calories."
            )
            return

        # Build the chart and get back a PNG buffer
        buf = build_calorie_graph(calorie_rows)

        if buf is None:
            await update.message.reply_text("⚠️ Could not generate graph. Please try again.")
            return

        # Send the PNG buffer directly as a Telegram photo
        await update.message.reply_photo(
            photo=buf,
            caption="📊 *Your Weekly Average Calorie Chart*\nDashed line shows your trend.",
            parse_mode="Markdown"
        )

    except Exception as e:
        logger.error(f"Calorie graph error for user {user_id}: {e}")
        await update.message.reply_text("⚠️ Something went wrong generating the graph. Please try again.")


async def cmd_weightgraph(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /weightgraph — Generates and sends a line chart of the user's last 5 weight entries.
    Reads weight history from the user's Google Sheet tab.
    """
    user_id = update.effective_user.id

    await update.message.reply_text("📉 Generating your weight graph, please wait...")

    try:
        client       = get_sheets_client()
        worksheet    = get_user_sheet(client, user_id)
        weight_rows  = read_data_rows(worksheet, "weight")

        if not weight_rows:
            await update.message.reply_text(
                "⚠️ No weight data found. Use /updateweight to start logging your weight."
            )
            return

        buf = build_weight_graph(weight_rows)

        if buf is None:
            await update.message.reply_text("⚠️ Could not generate graph. Please try again.")
            return

        await update.message.reply_photo(
            photo=buf,
            caption="📉 *Your Weight Progress Chart*\nDashed line shows your trend.",
            parse_mode="Markdown"
        )

    except Exception as e:
        logger.error(f"Weight graph error for user {user_id}: {e}")
        await update.message.reply_text("⚠️ Something went wrong generating the graph. Please try again.")


# ════════════════════════════════════════════════════════════════════
#  CONVERSATION STATE HANDLER
#  Handles all plain text messages (not commands)
#  Checks the user's current state to know what input we're expecting
# ════════════════════════════════════════════════════════════════════

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text    = update.message.text.strip()
    state   = user_state.get(user_id, "idle")

    # ── Step 1 of registration: capture name ──────────────────────
    if state == "awaiting_name":
        user_name[user_id]  = text
        user_state[user_id] = "awaiting_height"
        await update.message.reply_text(
            "📏 Enter your *height in centimetres* (e.g. 175):",
            parse_mode="Markdown"
        )

    # ── Step 2 of registration: capture and validate height ───────
    elif state == "awaiting_height":
        try:
            h = float(text)
            if h < 50 or h > 300:
                raise ValueError
            user_height[user_id] = h
            user_state[user_id]  = "awaiting_weight"
            await update.message.reply_text(
                "⚖️ Enter your *weight in kilograms* (e.g. 70):",
                parse_mode="Markdown"
            )
        except ValueError:
            # Keep asking until valid — don't advance state
            await update.message.reply_text(
                "⚠️ Invalid height. Enter a number between 50 and 300 cm (e.g. 175):"
            )

    # ── Step 3 of registration: capture and validate weight ───────
    elif state == "awaiting_weight":
        try:
            w = float(text)
            if w < 10 or w > 500:
                raise ValueError
            user_weight[user_id] = w
            user_state[user_id]  = "awaiting_age"  # Move to new age step
            await update.message.reply_text(
                "🎂 Enter your *age* (e.g. 25):",
                parse_mode="Markdown"
            )
        except ValueError:
            await update.message.reply_text(
                "⚠️ Invalid weight. Enter a number between 10 and 500 kg (e.g. 70):"
            )

    # ── Step 4 of registration: capture and validate age ──────────
    elif state == "awaiting_age":
        try:
            age = int(text)
            if age < 10 or age > 120:
                raise ValueError
            user_age[user_id]   = age
            user_state[user_id] = "awaiting_gender"  # Move to gender step
            await update.message.reply_text(
                "⚧ Enter your *gender* — type *male* or *female*:",
                parse_mode="Markdown"
            )
        except ValueError:
            await update.message.reply_text(
                "⚠️ Invalid age. Enter a number between 10 and 120 (e.g. 25):"
            )

    # ── Step 5 of registration: capture gender, complete registration ─
    elif state == "awaiting_gender":
        gender_input = text.lower().strip()
        if gender_input not in ("male", "female"):
            # Only accept exactly "male" or "female"
            await update.message.reply_text(
                "⚠️ Please type *male* or *female*:",
                parse_mode="Markdown"
            )
            return

        user_gender[user_id] = gender_input
        user_state[user_id]  = "idle"  # Registration complete

        name   = user_name.get(user_id, "N/A")
        height = user_height.get(user_id)
        weight = user_weight.get(user_id)
        age    = user_age.get(user_id)
        bmi, cat = calc_bmi(height, weight)
        tdee     = calc_tdee(height, weight, age, gender_input)

        # Write profile and initial weight entry to Google Sheets
        try:
            client    = get_sheets_client()
            worksheet = get_user_sheet(client, user_id)
            write_profile_name(worksheet, name)
            append_data_row(worksheet, "weight", weight)
        except Exception as e:
            logger.error(f"Sheet write error during registration: {e}")

        await update.message.reply_text(
            f"✅ *Registration Complete!*\n\n"
            f"Name: {name}\n"
            f"Age: {age}\n"
            f"Gender: {gender_input.capitalize()}\n"
            f"Height: {height} cm\n"
            f"Weight: {weight} kg\n"
            f"BMI: {bmi} — {cat}\n"
            f"Daily Calorie Target: {tdee} kcal",
            parse_mode="Markdown"
        )

    # ── Update weight: capture new weight, write to sheet ─────────
    elif state == "awaiting_update_weight":
        try:
            w = float(text)
            if w < 10 or w > 500:
                raise ValueError
            user_weight[user_id] = w
            user_state[user_id]  = "idle"
            name   = user_name.get(user_id, "N/A")
            height = user_height.get(user_id)
            age    = user_age.get(user_id)
            gender = user_gender.get(user_id)
            bmi, cat = calc_bmi(height, w)
            tdee     = calc_tdee(height, w, age, gender) if age and gender else None

            # Append new weight row — builds history for /weightgraph
            try:
                client    = get_sheets_client()
                worksheet = get_user_sheet(client, user_id)
                append_data_row(worksheet, "weight", w)
            except Exception as e:
                logger.error(f"Sheet write error during weight update: {e}")

            tdee_line = f"\nDaily Calorie Target: {tdee} kcal" if tdee else ""
            await update.message.reply_text(
                f"✅ *Weight Updated!*\n\n"
                f"Name: {name}\n"
                f"Height: {height} cm\n"
                f"Weight: {w} kg\n"
                f"BMI: {bmi} — {cat}"
                f"{tdee_line}",
                parse_mode="Markdown"
            )
        except ValueError:
            await update.message.reply_text(
                "⚠️ Invalid weight. Enter a number between 10 and 500 kg (e.g. 70):"
            )

    # ── Calorie tracking: add to daily total, compare against TDEE ─
    elif state == "awaiting_calories":
        try:
            cal = float(text)
            if cal < 0:
                raise ValueError
            prev  = user_calories.get(user_id, 0)
            total = prev + cal
            user_calories[user_id] = total
            user_state[user_id]    = "idle"

            # Write calorie entry to sheet for graph history
            try:
                client    = get_sheets_client()
                worksheet = get_user_sheet(client, user_id)
                append_data_row(worksheet, "calories", cal)
            except Exception as e:
                logger.error(f"Sheet write error during calorie log: {e}")

            # Generate personalised feedback using TDEE if profile is complete
            height = user_height.get(user_id)
            weight = user_weight.get(user_id)
            age    = user_age.get(user_id)
            gender = user_gender.get(user_id)

            if height and weight and age and gender:
                # Personalised threshold based on Mifflin-St Jeor TDEE
                tdee = calc_tdee(height, weight, age, gender)
                note = get_calorie_note(total, tdee)
                target_line = f"Your daily target: *{tdee} kcal*\n"
            else:
                # Fallback if profile incomplete for some reason
                note        = "Use /register to get personalised calorie targets."
                target_line = ""

            await update.message.reply_text(
                f"🍽️ *Calorie Log Updated*\n\n"
                f"Total today: *{total} kcal*\n"
                f"{target_line}"
                f"{note}\n\n"
                f"Use /track to add more or /resettrack to reset.",
                parse_mode="Markdown"
            )
        except ValueError:
            await update.message.reply_text(
                "⚠️ Invalid entry. Please enter a positive number for calories (e.g. 350):"
            )

    # ── Fallback: no active conversation state ─────────────────────
    else:
        await update.message.reply_text(
            "❓ Unknown command. Use /start to see all available commands."
        )

# ════════════════════════════════════════════════════════════════════
#  FLASK ROUTES
# ════════════════════════════════════════════════════════════════════

@app.route("/webhook", methods=["POST"])
def webhook():
    """
    /webhook — Main endpoint Telegram POSTs all updates to
    Builds a fresh bot application per request, registers all handlers,
    then processes the incoming update
    """
    if not BOT_TOKEN:
        return "No token", 500

    data = request.get_json(force=True)

    async def process():
        application = Application.builder().token(BOT_TOKEN).build()

        # Register all command handlers
        application.add_handler(CommandHandler("start",        cmd_start))
        application.add_handler(CommandHandler("register",     cmd_register))
        application.add_handler(CommandHandler("user",         cmd_user))
        application.add_handler(CommandHandler("updateweight", cmd_updateweight))
        application.add_handler(CommandHandler("track",        cmd_track))
        application.add_handler(CommandHandler("resettrack",   cmd_resettrack))
        application.add_handler(CommandHandler("caloriegraph", cmd_caloriegraph))
        application.add_handler(CommandHandler("weightgraph",  cmd_weightgraph))

        # Catch all plain text messages for conversation state handling
        application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

        await application.initialize()
        update = Update.de_json(data, application.bot)
        await application.process_update(update)

    asyncio.run(process())
    return "ok", 200


@app.route("/", methods=["GET"])
def index():
    """
    / — Health check endpoint
    Visit your Cloud Run URL in a browser to confirm the bot is alive
    """
    token_status  = "Token found" if BOT_TOKEN else "Token MISSING"
    sheets_status = "Sheets credentials found" if SHEETS_CREDS_RAW else "Sheets credentials MISSING"
    sheets_id     = f"Sheet ID: {SHEETS_ID}" if SHEETS_ID else "Sheet ID MISSING"
    return f"Bot is running! {token_status} | {sheets_status} | {sheets_id}", 200


# ── Local development entry point ─────────────────────────────────
# Only runs when you execute `python bot.py` directly
# In production, gunicorn starts the app (see Dockerfile CMD)
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)