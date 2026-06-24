import os
import io
import json
import logging
import asyncio
from datetime import datetime, timedelta
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

app = Flask(__name__)  # Web server that receives Telegram webhook requests

# Injected by Cloud Run from GCP Secret Manager at runtime
BOT_TOKEN        = os.environ.get("TELEGRAM_BOT_TOKEN")
SHEETS_CREDS_RAW = os.environ.get("SHEETS_CREDENTIALS")  # Service account JSON string
SHEETS_ID        = os.environ.get("SHEETS_ID")           # Google Sheet file ID

# Per-user in-memory store — lost on container restart, permanent data lives in Sheets
user_state    = {}  # Current conversation step
user_name     = {}  # Registered name
user_height   = {}  # Height in cm
user_weight   = {}  # Weight in kg
user_calories = {}  # Running calorie total for today
user_age      = {}  # Age
user_gender   = {}  # "male" or "female"


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
    spreadsheet = client.open_by_key(SHEETS_ID)
    tab_name    = str(user_id)
    try:
        return spreadsheet.worksheet(tab_name)
    except gspread.exceptions.WorksheetNotFound:
        worksheet = spreadsheet.add_worksheet(title=tab_name, rows=1000, cols=5)
        # Row 1: full profile header — all fields stored across columns A-F
        worksheet.update("A1:F1", [["NAME", "HEIGHT", "AGE", "GENDER", "WEIGHT", ""]])
        # Row 3: data log column headers
        worksheet.update("A3:C3", [["DATE", "TYPE", "VALUE"]])
        return worksheet


def write_profile(worksheet, name, height, age, gender, weight):
    # Writes all profile fields into row 1 (A1:F1) of the user's tab.
    # Called on registration and whenever weight/profile is updated.
    worksheet.update("A1:F1", [[name, height, age, gender, weight, ""]])


def append_data_row(worksheet, entry_type, value):
    # Adds a new row to the bottom of the user's data log with today's date.
    # First reads all existing rows to find the next empty row index.
    # Enforces a minimum of row 4 so we never accidentally overwrite
    # the profile header (row 1), spacer (row 2), or column headers (row 3).
    today      = datetime.now().strftime("%Y-%m-%d")
    all_values = worksheet.get_all_values()
    next_row   = max(len(all_values) + 1, 4)
    worksheet.update(f"A{next_row}:C{next_row}", [[today, entry_type, value]])


def read_data_rows(worksheet, entry_type):
    # Reads all rows from the data log and filters by entry type ("calories" or "weight").
    # Skips the first 3 rows (profile + spacer + headers) using all_values[3:].
    # Each valid row is returned as a (date_string, float_value) tuple.
    # Rows with missing columns or non-numeric values are silently skipped
    # to handle any accidental manual edits in the sheet.
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
    # Uses a ratio (total / tdee) instead of fixed thresholds so the feedback
    # scales correctly regardless of the user's body size or calorie target.
    # e.g. ratio of 0.4 means they've only eaten 40% of what they need today.
    ratio = total / tdee
    if ratio < 0.5:    return f"⚠️ Very low — under 50% of your daily target ({tdee} kcal)."
    elif ratio < 0.75: return f"🟡 Below target — aim for around {tdee} kcal today."
    elif ratio <= 1.0: return f"✅ On track — within your daily target of {tdee} kcal."
    elif ratio <= 1.2: return f"🟡 Slightly over your daily target of {tdee} kcal."
    else:              return f"🔴 Significantly over your daily target of {tdee} kcal."


# ── Graph builders ────────────────────────────────────────────────

def build_calorie_graph(calorie_rows):
    # Builds a bar chart showing average daily calories per week.
    # Works backwards from today to find the start of the current week (Monday),
    # then generates 5 week buckets: 4 complete past weeks + the current week.
    # Each calorie entry is sorted into its correct week bucket by date.
    # Average calculation differs between complete and incomplete weeks:
    #   - Complete weeks: total calories / 7 (full 7-day average)
    #   - Current week: total calories / days elapsed so far (e.g. if Wednesday, divide by 3)
    # Weeks with no data produce a None value which renders as an empty gap in the chart,
    # making it visually clear there's missing data rather than showing a misleading zero bar.
    # The trend line is calculated using numpy polyfit (linear regression) and is only
    # drawn across weeks that actually have data — empty weeks are excluded from the fit.
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

    # Linear regression trend line — only computed on weeks that have data.
    # polyfit returns coefficients [slope, intercept] of the best-fit line.
    # poly1d converts those into a callable function p(x) we can plot.
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
    ax.set_title("Average Daily Calories Per Week\n(Last 4 Weeks + Current)", color="white", fontsize=13, fontweight="bold", pad=15)
    ax.tick_params(colors="white")
    ax.yaxis.label.set_color("white")
    for spine in ["top", "right"]: ax.spines[spine].set_visible(False)
    for spine in ["bottom", "left"]: ax.spines[spine].set_color("#555577")
    ax.grid(axis="y", color="#555577", linestyle="--", alpha=0.5, zorder=0)
    plt.tight_layout()

    buf = io.BytesIO()
    plt.savefig(buf, format="png", dpi=150, bbox_inches="tight")
    plt.close(fig)  # Free memory — important in a long-running server
    buf.seek(0)     # Rewind buffer to start before passing to Telegram
    return buf


def build_weight_graph(weight_rows):
    # Builds a line chart of the last 5 weight entries.
    # Dates are parsed from strings into datetime objects so matplotlib can
    # space the X axis correctly based on actual time gaps between entries
    # (e.g. entries 2 weeks apart will appear further apart than daily entries).
    # The trend line uses date2num to convert dates into numeric values that
    # polyfit can process, then num2date converts the result back for plotting.
    # Trend line is skipped entirely if fewer than 2 data points exist.
    if not weight_rows:
        return None

    recent  = weight_rows[-5:]
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

    # Convert dates to floats for polyfit, then back to dates for plotting
    if len(dates) >= 2:
        x_num = mdates.date2num(dates)
        p     = np.poly1d(np.polyfit(x_num, weights, 1))
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
        "📉 /weightgraph — Last 5 weight entries chart",
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
    # If data isn't in memory (e.g. after container restart), reads all
    # profile fields from row 1 of the user's sheet tab.
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

            # Row 1 stores: NAME | HEIGHT | AGE | GENDER | WEIGHT
            if len(all_vals) > 0 and len(all_vals[0]) >= 5:
                row = all_vals[0]
                name   = row[0]; user_name[user_id]   = name
                height = float(row[1]) if row[1] else None; user_height[user_id] = height
                age    = int(row[2])   if row[2] else None; user_age[user_id]    = age
                gender = row[3];       user_gender[user_id] = gender

            # Most recent weight entry from data log
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
# Handles all plain text messages by checking the user's current state

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
        # Step 5: validate gender, complete registration, write to Sheets
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
            write_profile(worksheet, name, height, age, gender_input, weight)
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
        # Validate new weight, update Sheets, recalculate BMI and TDEE
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
                # Update weight column in profile row so /user always shows latest
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

@app.route("/webhook", methods=["POST"])
def webhook():
    # Receives all Telegram updates; builds app, registers handlers, processes update
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
        application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
        await application.initialize()
        update = Update.de_json(data, application.bot)
        await application.process_update(update)

    asyncio.run(process())
    return "ok", 200


@app.route("/", methods=["GET"])
def index():
    # Health check — visit Cloud Run URL in browser to confirm bot is alive
    token_status  = "Token found"               if BOT_TOKEN        else "Token MISSING"
    sheets_status = "Sheets credentials found"  if SHEETS_CREDS_RAW else "Sheets credentials MISSING"
    sheets_id     = f"Sheet ID: {SHEETS_ID}"    if SHEETS_ID        else "Sheet ID MISSING"
    return f"Bot is running! {token_status} | {sheets_status} | {sheets_id}", 200


# Runs only when executing `python bot.py` directly; production uses gunicorn (see Dockerfile)
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))