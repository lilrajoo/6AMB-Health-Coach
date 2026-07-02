import os
import io
import json
import logging
import asyncio
import threading
import schedule
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import gspread
import matplotlib
matplotlib.use("Agg")  # Non-interactive backend, no display needed
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import numpy as np
from flask import Flask, request
from google.oauth2.service_account import Credentials
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# Injected by Cloud Run from GCP Secret Manager at runtime
BOT_TOKEN        = os.environ.get("TELEGRAM_BOT_TOKEN")
SHEETS_CREDS_RAW = os.environ.get("SHEETS_CREDENTIALS")
SHEETS_ID        = os.environ.get("SHEETS_ID")

# Singapore timezone — all reminder scheduling is done in SGT (UTC+8)
SGT = ZoneInfo("Asia/Singapore")

# Per-user in-memory store — lost on container restart, permanent data lives in Sheets
user_state    = {}  # Current conversation step
user_name     = {}  # Registered name
user_height   = {}  # Height in cm
user_weight   = {}  # Weight in kg
user_calories = {}  # Running calorie total for today
user_age      = {}  # Age
user_gender   = {}  # "male" or "female"

# Set of user IDs who have subscribed to reminders.
# Persisted to column F of row 1 in each user's sheet so it survives restarts.
subscribed_users = set()


# ── Google Sheets helpers ─────────────────────────────────────────

def get_sheets_client():
    # Authenticate using service account credentials and return a gspread client
    creds_dict = json.loads(SHEETS_CREDS_RAW)
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive"
    ]
    creds  = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    return gspread.authorize(creds)


def get_user_sheet(client, user_id):
    # Opens the master Google Sheet and navigates to the user's individual tab.
    # If the tab doesn't exist yet, creates it with headers for all profile fields.
    # Row 1: NAME | HEIGHT | AGE | GENDER | WEIGHT | SUBSCRIBED
    spreadsheet = client.open_by_key(SHEETS_ID)
    tab_name    = str(user_id)
    try:
        return spreadsheet.worksheet(tab_name)
    except gspread.exceptions.WorksheetNotFound:
        worksheet = spreadsheet.add_worksheet(title=tab_name, rows=1000, cols=6)
        worksheet.update("A1:F1", [["NAME", "HEIGHT", "AGE", "GENDER", "WEIGHT", "SUBSCRIBED"]])
        worksheet.update("A3:C3", [["DATE", "TYPE", "VALUE"]])
        return worksheet


def write_profile(worksheet, name, height, age, gender, weight, subscribed=False):
    # Writes all profile fields into row 1 (A1:F1).
    # Column F stores subscription status as TRUE/FALSE.
    worksheet.update("A1:F1", [[name, height, age, gender, weight, str(subscribed)]])


def write_subscription_status(user_id, subscribed):
    # Updates only column F of row 1 for the given user in their sheet tab.
    # Called whenever they subscribe or unsubscribe so the status persists
    # across container restarts without rewriting the entire profile row.
    try:
        client    = get_sheets_client()
        worksheet = get_user_sheet(client, user_id)
        worksheet.update("F1", [[str(subscribed)]])
    except Exception as e:
        logger.error(f"Sheet write error for subscription status {user_id}: {e}")


def load_subscriptions_from_sheets():
    # Called once on startup to reload all subscribed user IDs from Google Sheets.
    # Scans every tab in the master sheet, reads column F of row 1,
    # and adds the user ID to subscribed_users if the value is "True".
    # This ensures reminders continue working after a container restart.
    if not SHEETS_CREDS_RAW or not SHEETS_ID:
        return
    try:
        client      = get_sheets_client()
        spreadsheet = client.open_by_key(SHEETS_ID)
        sheets      = spreadsheet.worksheets()
        for sheet in sheets:
            # Skip the Master sheet — it's not a user tab
            if sheet.title.lower() == "master":
                continue
            try:
                row1 = sheet.row_values(1)
                # Column F (index 5) stores the subscription status
                if len(row1) >= 6 and row1[5].strip().lower() == "true":
                    subscribed_users.add(int(sheet.title))
                    logger.info(f"Reloaded subscription for user {sheet.title}")
            except Exception:
                continue
    except Exception as e:
        logger.error(f"Error loading subscriptions from sheets: {e}")


def append_data_row(worksheet, entry_type, value):
    # Adds a new dated row to the data log (below row 3).
    # Enforces a minimum of row 4 so we never overwrite the profile,
    # spacer, or column headers in rows 1-3.
    today      = datetime.now().strftime("%Y-%m-%d")
    all_values = worksheet.get_all_values()
    next_row   = max(len(all_values) + 1, 4)
    worksheet.update(f"A{next_row}:C{next_row}", [[today, entry_type, value]])


def read_data_rows(worksheet, entry_type):
    # Reads all rows of a given type ("calories" or "weight"), skips rows 1-3.
    # Returns a list of (date_string, float_value) tuples, oldest first.
    # Malformed rows are silently skipped.
    all_values = worksheet.get_all_values()
    rows       = []
    for row in all_values[3:]:
        if len(row) >= 3 and row[1].strip().lower() == entry_type:
            try:
                rows.append((row[0].strip(), float(row[2].strip())))
            except ValueError:
                continue
    return rows


# ── BMI + TDEE helpers ────────────────────────────────────────────

def calc_bmi(height_cm, weight_kg):
    # Returns (bmi rounded to 1dp, category string with emoji)
    bmi = weight_kg / ((height_cm / 100) ** 2)
    if bmi < 18.5:  cat = "🔵 Underweight"
    elif bmi < 25:  cat = "🟢 Normal weight"
    elif bmi < 30:  cat = "🟡 Overweight"
    else:           cat = "🔴 Obese"
    return round(bmi, 1), cat


def calc_tdee(height_cm, weight_kg, age, gender):
    # Mifflin-St Jeor BMR x 1.2 sedentary multiplier → daily calorie target
    if gender.lower() == "male":
        bmr = (10 * weight_kg) + (6.25 * height_cm) - (5 * age) + 5
    else:
        bmr = (10 * weight_kg) + (6.25 * height_cm) - (5 * age) - 161
    return round(bmr * 1.2)


def get_calorie_note(total, tdee):
    # Compares today's total calorie intake against the user's personal TDEE.
    # Uses a ratio so feedback scales correctly regardless of body size.
    ratio = total / tdee
    if ratio < 0.5:    return f"⚠️ Very low — under 50% of your daily target ({tdee} kcal)."
    elif ratio < 0.75: return f"🟡 Below target — aim for around {tdee} kcal today."
    elif ratio <= 1.0: return f"✅ On track — within your daily target of {tdee} kcal."
    elif ratio <= 1.2: return f"🟡 Slightly over your daily target of {tdee} kcal."
    else:              return f"🔴 Significantly over your daily target of {tdee} kcal."


# ── Graph builders ────────────────────────────────────────────────

def build_calorie_graph(calorie_rows):
    # Bar chart: avg daily calories per week for last 4 complete weeks + current week.
    # Complete weeks divide by 7; current (incomplete) week divides by days elapsed.
    # Weeks with no data show as empty gaps rather than zero bars.
    # Trend line (linear regression via numpy polyfit) only drawn across weeks with data.
    if not calorie_rows:
        return None

    today       = datetime.now().date()
    this_monday = today - timedelta(days=today.weekday())
    week_starts = [this_monday - timedelta(weeks=i) for i in range(4, -1, -1)]
    week_data   = {ws: [] for ws in week_starts}

    for date_str, calories in calorie_rows:
        try:
            entry_date = datetime.strptime(date_str, "%Y-%m-%d").date()
        except ValueError:
            continue
        for ws in week_starts:
            if ws <= entry_date <= ws + timedelta(days=6):
                week_data[ws].append(calories)
                break

    week_labels, week_avgs, has_data = [], [], []
    for i, ws in enumerate(week_starts):
        values = week_data[ws]
        week_labels.append(ws.strftime('%d %b'))
        if not values:
            week_avgs.append(None)
            has_data.append(False)
        else:
            divisor = (today.weekday() + 1) if i == len(week_starts) - 1 else 7
            week_avgs.append(round(sum(values) / divisor, 1))
            has_data.append(True)

    fig, ax = plt.subplots(figsize=(10, 6))
    fig.patch.set_facecolor("#1e1e2e")
    ax.set_facecolor("#2a2a3e")
    x_positions = list(range(len(week_labels)))

    for i, (avg, _) in enumerate(zip(week_avgs, week_labels)):
        if avg is not None:
            ax.bar(i, avg, color="#7c6af7", width=0.6, zorder=3)
            ax.text(i, avg + 20, f"{avg:.0f}", ha="center", va="bottom",
                    color="white", fontsize=10, fontweight="bold")

    # polyfit returns [slope, intercept]; poly1d makes it a callable p(x)
    trend_x = [i for i, h in enumerate(has_data) if h]
    trend_y = [a for a, h in zip(week_avgs, has_data) if h]
    if len(trend_x) >= 2:
        p = np.poly1d(np.polyfit(trend_x, trend_y, 1))
        ax.plot(np.linspace(trend_x[0], trend_x[-1], 100),
                p(np.linspace(trend_x[0], trend_x[-1], 100)),
                color="#f7a76a", linewidth=2, linestyle="--", label="Trend", zorder=4)
        ax.legend(facecolor="#2a2a3e", labelcolor="white", fontsize=10)

    ax.set_xticks(x_positions)
    ax.set_xticklabels(week_labels, color="white", fontsize=10)
    ax.set_ylabel("Avg Daily Calories (kcal)", color="white", fontsize=11)
    ax.set_xlabel("Week Starting", color="white", fontsize=11)
    ax.set_title("Average Daily Calories Per Week\n(Last 4 Weeks + Current)",
                 color="white", fontsize=13, fontweight="bold", pad=15)
    ax.tick_params(colors="white")
    ax.yaxis.label.set_color("white")
    for spine in ["top", "right"]: ax.spines[spine].set_visible(False)
    for spine in ["bottom", "left"]: ax.spines[spine].set_color("#555577")
    ax.grid(axis="y", color="#555577", linestyle="--", alpha=0.5, zorder=0)
    plt.tight_layout()

    buf = io.BytesIO()
    plt.savefig(buf, format="png", dpi=150, bbox_inches="tight")
    plt.close(fig)
    buf.seek(0)
    return buf


def build_weight_graph(weight_rows):
    # Line chart of last 5 weight entries with trend line.
    # Dates converted to floats for polyfit, then back to dates for plotting.
    # Trend line skipped if fewer than 2 data points exist.
    if not weight_rows:
        return None

    recent        = weight_rows[-5:]
    dates, weights = [], []
    for date_str, w in recent:
        try:
            dates.append(datetime.strptime(date_str, "%Y-%m-%d"))
            weights.append(w)
        except ValueError:
            continue

    if not dates:
        return None

    fig, ax = plt.subplots(figsize=(10, 6))
    fig.patch.set_facecolor("#1e1e2e")
    ax.set_facecolor("#2a2a3e")

    ax.plot(dates, weights, color="#7c6af7", linewidth=2.5, marker="o",
            markersize=8, markerfacecolor="#f7a76a",
            markeredgecolor="white", markeredgewidth=1.5, zorder=3, label="Weight")

    for d, w in zip(dates, weights):
        ax.text(d, w + 0.3, f"{w} kg", ha="center", va="bottom",
                color="white", fontsize=10, fontweight="bold")

    if len(dates) >= 2:
        x_num   = mdates.date2num(dates)
        p       = np.poly1d(np.polyfit(x_num, weights, 1))
        x_range = np.linspace(x_num[0], x_num[-1], 100)
        ax.plot(mdates.num2date(x_range), p(x_range),
                color="#f7a76a", linewidth=2, linestyle="--", label="Trend", zorder=4)

    ax.xaxis.set_major_formatter(mdates.DateFormatter("%d %b"))
    plt.xticks(rotation=30, ha="right", color="white", fontsize=10)
    ax.set_ylabel("Weight (kg)", color="white", fontsize=11)
    ax.set_xlabel("Date", color="white", fontsize=11)
    ax.set_title("Last 5 Weight Entries", color="white", fontsize=13, fontweight="bold", pad=15)
    ax.tick_params(colors="white")
    for spine in ["top", "right"]: ax.spines[spine].set_visible(False)
    for spine in ["bottom", "left"]: ax.spines[spine].set_color("#555577")
    ax.grid(axis="y", color="#555577", linestyle="--", alpha=0.5, zorder=0)
    ax.legend(facecolor="#2a2a3e", labelcolor="white", fontsize=10)
    plt.tight_layout()

    buf = io.BytesIO()
    plt.savefig(buf, format="png", dpi=150, bbox_inches="tight")
    plt.close(fig)
    buf.seek(0)
    return buf


# ════════════════════════════════════════════════════════════════════
#  REMINDER SYSTEM
# ════════════════════════════════════════════════════════════════════

subscribed_users = set()

def write_subscription_status(user_id, subscribed):
    # Persists subscription status to column F of row 1 in the user's sheet
    try:
        client    = get_sheets_client()
        worksheet = get_user_sheet(client, user_id)
        worksheet.update("F1", [[str(subscribed)]])
    except Exception as e:
        logger.error(f"Subscription write error {user_id}: {e}")


def load_subscriptions_from_sheets():
    # On startup, scans all user tabs and reloads subscribed user IDs from column F row 1
    if not SHEETS_CREDS_RAW or not SHEETS_ID:
        return
    try:
        client      = get_sheets_client()
        spreadsheet = client.open_by_key(SHEETS_ID)
        for sheet in spreadsheet.worksheets():
            if sheet.title.lower() == "master":
                continue
            try:
                row1 = sheet.row_values(1)
                if len(row1) >= 6 and row1[5].strip().lower() == "true":
                    subscribed_users.add(int(sheet.title))
            except Exception:
                continue
    except Exception as e:
        logger.error(f"Error loading subscriptions: {e}")


async def send_reminder(user_id, message):
    # Sends a reminder message to a single user
    try:
        application = Application.builder().token(BOT_TOKEN).build()
        await application.bot.send_message(chat_id=user_id, text=message, parse_mode="Markdown")
        logger.info(f"Reminder sent to {user_id}")
    except Exception as e:
        logger.error(f"Reminder failed for {user_id}: {e}")


def fire_reminder(message):
    # Sends the reminder to all subscribed users
    if not subscribed_users:
        return
    for user_id in list(subscribed_users):
        asyncio.run(send_reminder(user_id, message))


def job_midday():
    # 14:00 SGT (06:00 UTC) Mon–Fri after-lunch reminder
    now = datetime.now(SGT)
    if now.weekday() >= 5:
        return
    fire_reminder(
        "🍽️ *Afternoon Check-in!*\n\n"
        "Don't forget to log your lunch calories!\n"
        "Use /track to add them to today's total. 💪"
    )


def job_evening():
    # 20:30 SGT (12:30 UTC) Mon–Fri end-of-day reminder; Friday includes weigh-in
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
    # Background thread — checks every 60s for pending jobs
    # Times are in UTC (Cloud Run runs in UTC, SGT = UTC+8)
    # ── TESTING: both jobs at 07:35 UTC = 15:35 SGT ──────────────
    #schedule.every().day.at("07:35").do(job_midday)
    #schedule.every().day.at("07:35").do(job_evening)
    # ── PRODUCTION (uncomment these and remove above two): ─────────
    schedule.every().day.at("06:00").do(job_midday)   # 14:00 SGT
    schedule.every().day.at("12:30").do(job_evening)  # 20:30 SGT
    logger.info("Scheduler started")
    while True:
        schedule.run_pending()
        time.sleep(60)


def start_scheduler_thread():
    t = threading.Thread(target=run_scheduler, daemon=True)
    t.start()
    logger.info("Scheduler thread launched")


# ── Command handlers ──────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Welcome message; resets conversation state to idle
    user_state[update.effective_user.id] = "idle"
    await update.message.reply_text(
        "👋 *Welcome to the BMI & Calorie Tracker Bot!*\n\n"
        "Available commands:\n\n"
        "📋 /register — Register your Name, Height & Weight\n"
        "👤 /user — View your profile & BMI\n"
        "⚖️ /updateweight — Update your weight & recalculate BMI\n"
        "🍽️ /track — Log caloric intake (adds to daily total)\n"
        "🔄 /resettrack — Reset today's calorie total to zero\n"
        "📊 /caloriegraph — Weekly average calorie chart\n"
        "📉 /weightgraph — Last 5 weight entries chart\n"
        "🔔 /subscribe — Subscribe to daily calorie reminders\n"
        "🔕 /unsubscribe — Unsubscribe from reminders",
        parse_mode="Markdown"
    )


async def cmd_subscribe(update: Update, context: ContextTypes.DEFAULT_TYPE):
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


async def cmd_unsubscribe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in subscribed_users:
        await update.message.reply_text("🔕 You're not subscribed. Use /subscribe to turn on.")
        return
    subscribed_users.discard(user_id)
    threading.Thread(target=write_subscription_status, args=(user_id, False), daemon=True).start()
    await update.message.reply_text(
        "🔕 *Unsubscribed from reminders.*\n\nUse /subscribe to turn back on anytime.",
        parse_mode="Markdown"
    )


async def cmd_register(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Starts registration flow: name → height → weight → age → gender
    user_state[update.effective_user.id] = "awaiting_name"
    await update.message.reply_text(
        "📝 *Registration started!*\n\nPlease enter your rank & full name:",
        parse_mode="Markdown"
    )


async def cmd_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Shows profile, BMI and TDEE.
    # Falls back to reading all profile fields from row 1 of the sheet
    # if data isn't in memory (e.g. after a container restart).
    user_id = update.effective_user.id
    name   = user_name.get(user_id)
    height = user_height.get(user_id)
    weight = user_weight.get(user_id)
    age    = user_age.get(user_id)
    gender = user_gender.get(user_id)

    if not name or not height or not weight or not age or not gender:
        try:
            client    = get_sheets_client()
            worksheet = get_user_sheet(client, user_id)
            all_vals  = worksheet.get_all_values()
            if len(all_vals) > 0 and len(all_vals[0]) >= 5:
                row    = all_vals[0]
                name   = row[0]; user_name[user_id]   = name
                height = float(row[1]) if row[1] else None; user_height[user_id] = height
                age    = int(row[2])   if row[2] else None; user_age[user_id]    = age
                gender = row[3];       user_gender[user_id] = gender
            weight_rows = read_data_rows(worksheet, "weight")
            if weight_rows:
                weight = weight_rows[-1][1]
                user_weight[user_id] = weight
        except Exception as e:
            logger.error(f"Sheet read error for /user: {e}")

    if not name or not height or not weight or not age or not gender:
        await update.message.reply_text("⚠️ No profile found. Please use /register first.")
        return

    bmi, cat = calc_bmi(height, weight)
    tdee     = calc_tdee(height, weight, age, gender)
    await update.message.reply_text(
        f"👤 *Your Profile*\n\n"
        f"Name: {name}\nAge: {age}\nGender: {gender.capitalize()}\n"
        f"Height: {height} cm\nWeight: {weight} kg\n"
        f"BMI: {bmi} — {cat}\nDaily Calorie Target: {tdee} kcal",
        parse_mode="Markdown"
    )


async def cmd_updateweight(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Prompts for new weight; requires registration (needs height for BMI)
    user_id = update.effective_user.id
    if not user_name.get(user_id):
        await update.message.reply_text("⚠️ No profile found. Please use /register first.")
        return
    user_state[user_id] = "awaiting_update_weight"
    await update.message.reply_text(
        "⚖️ Enter your *new weight in kilograms* (e.g. 68):", parse_mode="Markdown"
    )


async def cmd_track(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Shows current daily total and prompts user to log more calories
    user_id = update.effective_user.id
    total   = user_calories.get(user_id, 0)
    user_state[user_id] = "awaiting_calories"
    await update.message.reply_text(
        f"🍽️ *Calorie Tracker*\n\nCurrent daily total: *{total} kcal*\n\n"
        f"Enter calories to add (e.g. 350):", parse_mode="Markdown"
    )


async def cmd_resettrack(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Resets in-memory daily total to zero; sheet history is preserved
    user_calories[update.effective_user.id] = 0
    await update.message.reply_text(
        "🔄 Calorie total reset to *0 kcal*. Use /track to start logging again.",
        parse_mode="Markdown"
    )


async def cmd_caloriegraph(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Reads calorie history from Sheets and sends a weekly average bar chart
    user_id = update.effective_user.id
    await update.message.reply_text("📊 Generating your calorie graph, please wait...")
    try:
        client       = get_sheets_client()
        worksheet    = get_user_sheet(client, user_id)
        calorie_rows = read_data_rows(worksheet, "calories")
        if not calorie_rows:
            await update.message.reply_text(
                "⚠️ No calorie data found. Use /track to start logging your calories."
            )
            return
        buf = build_calorie_graph(calorie_rows)
        if buf is None:
            await update.message.reply_text("⚠️ Could not generate graph. Please try again.")
            return
        await update.message.reply_photo(
            photo=buf,
            caption="📊 *Your Weekly Average Calorie Chart*\nDashed line shows your trend.",
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"Calorie graph error for user {user_id}: {e}")
        await update.message.reply_text("⚠️ Something went wrong generating the graph. Please try again.")


async def cmd_weightgraph(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Reads weight history from Sheets and sends a line chart of last 5 entries
    user_id = update.effective_user.id
    await update.message.reply_text("📉 Generating your weight graph, please wait...")
    try:
        client      = get_sheets_client()
        worksheet   = get_user_sheet(client, user_id)
        weight_rows = read_data_rows(worksheet, "weight")
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


# ── Conversation state handler ────────────────────────────────────

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text    = update.message.text.strip()
    state   = user_state.get(user_id, "idle")

    if state == "awaiting_name":
        # Step 1: save name, ask for height
        user_name[user_id]  = text
        user_state[user_id] = "awaiting_height"
        await update.message.reply_text(
            "📏 Enter your *height in centimetres* (e.g. 175):", parse_mode="Markdown"
        )

    elif state == "awaiting_height":
        # Step 2: validate height (50–300 cm), ask for weight
        try:
            h = float(text)
            if h < 50 or h > 300: raise ValueError
            user_height[user_id] = h
            user_state[user_id]  = "awaiting_weight"
            await update.message.reply_text(
                "⚖️ Enter your *weight in kilograms* (e.g. 70):", parse_mode="Markdown"
            )
        except ValueError:
            await update.message.reply_text(
                "⚠️ Invalid height. Enter a number between 50 and 300 cm (e.g. 175):"
            )

    elif state == "awaiting_weight":
        # Step 3: validate weight (10–500 kg), ask for age
        try:
            w = float(text)
            if w < 10 or w > 500: raise ValueError
            user_weight[user_id] = w
            user_state[user_id]  = "awaiting_age"
            await update.message.reply_text(
                "🎂 Enter your *age* (e.g. 25):", parse_mode="Markdown"
            )
        except ValueError:
            await update.message.reply_text(
                "⚠️ Invalid weight. Enter a number between 10 and 500 kg (e.g. 70):"
            )

    elif state == "awaiting_age":
        # Step 4: validate age (10–120), ask for gender
        try:
            age = int(text)
            if age < 10 or age > 120: raise ValueError
            user_age[user_id]   = age
            user_state[user_id] = "awaiting_gender"
            await update.message.reply_text(
                "⚧ Enter your *gender* — type *male* or *female*:", parse_mode="Markdown"
            )
        except ValueError:
            await update.message.reply_text(
                "⚠️ Invalid age. Enter a number between 10 and 120 (e.g. 25):"
            )

    elif state == "awaiting_gender":
        # Step 5: validate gender, complete registration, write full profile to Sheets
        gender_input = text.lower().strip()
        if gender_input not in ("male", "female"):
            await update.message.reply_text(
                "⚠️ Please type *male* or *female*:", parse_mode="Markdown"
            )
            return

        user_gender[user_id] = gender_input
        user_state[user_id]  = "idle"
        name   = user_name.get(user_id, "N/A")
        height = user_height.get(user_id)
        weight = user_weight.get(user_id)
        age    = user_age.get(user_id)
        bmi, cat = calc_bmi(height, weight)
        tdee     = calc_tdee(height, weight, age, gender_input)

        try:
            client    = get_sheets_client()
            worksheet = get_user_sheet(client, user_id)
            # Pass current subscription status so it isn't overwritten on re-registration
            is_subscribed = user_id in subscribed_users
            write_profile(worksheet, name, height, age, gender_input, weight, is_subscribed)
            append_data_row(worksheet, "weight", weight)
        except Exception as e:
            logger.error(f"Sheet write error during registration: {e}")

        await update.message.reply_text(
            f"✅ *Registration Complete!*\n\n"
            f"Name: {name}\nAge: {age}\nGender: {gender_input.capitalize()}\n"
            f"Height: {height} cm\nWeight: {weight} kg\n"
            f"BMI: {bmi} — {cat}\nDaily Calorie Target: {tdee} kcal",
            parse_mode="Markdown"
        )

    elif state == "awaiting_update_weight":
        # Validate new weight, update sheet, recalculate BMI and TDEE
        try:
            w = float(text)
            if w < 10 or w > 500: raise ValueError
            user_weight[user_id] = w
            user_state[user_id]  = "idle"
            name   = user_name.get(user_id, "N/A")
            height = user_height.get(user_id)
            age    = user_age.get(user_id)
            gender = user_gender.get(user_id)
            bmi, cat = calc_bmi(height, w)
            tdee     = calc_tdee(height, w, age, gender) if age and gender else None

            try:
                client    = get_sheets_client()
                worksheet = get_user_sheet(client, user_id)
                append_data_row(worksheet, "weight", w)
                worksheet.update("E1", [[w]])
            except Exception as e:
                logger.error(f"Sheet write error during weight update: {e}")

            tdee_line = f"\nDaily Calorie Target: {tdee} kcal" if tdee else ""
            await update.message.reply_text(
                f"✅ *Weight Updated!*\n\nName: {name}\nHeight: {height} cm\n"
                f"Weight: {w} kg\nBMI: {bmi} — {cat}{tdee_line}",
                parse_mode="Markdown"
            )
        except ValueError:
            await update.message.reply_text(
                "⚠️ Invalid weight. Enter a number between 10 and 500 kg (e.g. 70):"
            )

    elif state == "awaiting_calories":
        # Add calories to daily total, write to Sheets, give TDEE-based feedback
        try:
            cal = float(text)
            if cal < 0: raise ValueError
            total = user_calories.get(user_id, 0) + cal
            user_calories[user_id] = total
            user_state[user_id]    = "idle"

            try:
                client    = get_sheets_client()
                worksheet = get_user_sheet(client, user_id)
                append_data_row(worksheet, "calories", cal)
            except Exception as e:
                logger.error(f"Sheet write error during calorie log: {e}")

            height = user_height.get(user_id)
            weight = user_weight.get(user_id)
            age    = user_age.get(user_id)
            gender = user_gender.get(user_id)

            if height and weight and age and gender:
                tdee        = calc_tdee(height, weight, age, gender)
                note        = get_calorie_note(total, tdee)
                target_line = f"Your daily target: *{tdee} kcal*\n"
            else:
                note        = "Use /register to get personalised calorie targets."
                target_line = ""

            await update.message.reply_text(
                f"🍽️ *Calorie Log Updated*\n\nTotal today: *{total} kcal*\n"
                f"{target_line}{note}\n\n"
                f"Use /track to add more or /resettrack to reset.",
                parse_mode="Markdown"
            )
        except ValueError:
            await update.message.reply_text(
                "⚠️ Invalid entry. Please enter a positive number for calories (e.g. 350):"
            )

    else:
        await update.message.reply_text(
            "❓ Unknown command. Use /start to see all available commands."
        )


# ── Flask routes ──────────────────────────────────────────────────

async def cmd_unknown(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Catches any command that doesn't match a registered handler
    # and reminds the user of the available commands
    await update.message.reply_text(
        "❓ *Unknown command.*\n\n"
        "Here are the available commands:\n\n"
        "📋 /register — Register your Name, Height & Weight\n"
        "👤 /user — View your profile & BMI\n"
        "⚖️ /updateweight — Update your weight & recalculate BMI\n"
        "🍽️ /track — Log caloric intake (adds to daily total)\n"
        "🔄 /resettrack — Reset today's calorie total to zero\n"
        "📊 /caloriegraph — Weekly average calorie chart\n"
        "📉 /weightgraph — Last 5 weight entries chart\n"
        "🔔 /subscribe — Subscribe to daily calorie reminders\n"
        "🔕 /unsubscribe — Unsubscribe from reminders",
        parse_mode="Markdown"
    )

@app.route("/webhook", methods=["POST"])
def webhook():
    # Receives all Telegram updates; builds app, registers handlers, processes update
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
        # Must be last — catches all unrecognised commands
        application.add_handler(MessageHandler(filters.COMMAND, cmd_unknown))
        await application.initialize()
        update = Update.de_json(data, application.bot)
        await application.process_update(update)

    asyncio.run(process())
    return "ok", 200


@app.route("/", methods=["GET"])
def index():
    # Health check — shows token, sheets and subscription status
    token_status  = "Token found"              if BOT_TOKEN        else "Token MISSING"
    sheets_status = "Sheets credentials found" if SHEETS_CREDS_RAW else "Sheets credentials MISSING"
    sheets_id     = f"Sheet ID: {SHEETS_ID}"   if SHEETS_ID        else "Sheet ID MISSING"
    sub_count     = f"{len(subscribed_users)} subscribed users"
    return f"Bot is running! {token_status} | {sheets_status} | {sheets_id} | {sub_count}", 200


# ── Startup ───────────────────────────────────────────────────────
# Load subscriptions from Sheets and start the reminder scheduler.
# Both run before Flask starts serving requests so reminders are
# active from the moment the container comes up.
load_subscriptions_from_sheets()
start_scheduler_thread()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
    load_subscriptions_from_sheets()
    start_scheduler_thread()