import logging
from telegram import Update
from telegram.ext import ContextTypes
from state import user_state, user_name, user_height, user_weight, user_calories, user_age, user_gender
from sheets import (get_sheets_client, get_user_sheet, write_profile,
                    append_data_row, read_data_rows,
                    get_todays_calories, delete_todays_calories)
from helpers import calc_bmi, calc_tdee, get_calorie_note
from graphs import build_calorie_graph, build_weight_graph
from reminders import subscribed_users

logger = logging.getLogger(__name__)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
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


async def cmd_register(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_state[update.effective_user.id] = "awaiting_name"
    await update.message.reply_text(
        "📝 *Registration started!*\n\nPlease enter your rank & full name:",
        parse_mode="Markdown"
    )


async def cmd_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
    user_id = update.effective_user.id
    if not user_name.get(user_id):
        await update.message.reply_text("⚠️ No profile found. Please use /register first.")
        return
    user_state[user_id] = "awaiting_update_weight"
    await update.message.reply_text(
        "⚖️ Enter your *new weight in kilograms* (e.g. 68):", parse_mode="Markdown"
    )


async def cmd_track(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    # If no in-memory total, reload today's sum from sheet
    if user_id not in user_calories:
        try:
            client    = get_sheets_client()
            worksheet = get_user_sheet(client, user_id)
            user_calories[user_id] = get_todays_calories(worksheet)
        except Exception as e:
            logger.error(f"Error reloading calories from sheet: {e}")
            user_calories[user_id] = 0

    total = user_calories.get(user_id, 0)
    user_state[user_id] = "awaiting_calories"
    await update.message.reply_text(
        f"🍽️ *Calorie Tracker*\n\nCurrent daily total: *{int(total)} kcal*\n\n"
        f"Enter calories to add (e.g. 350):",
        parse_mode="Markdown"
    )


async def cmd_resettrack(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_calories[user_id] = 0
    try:
        client    = get_sheets_client()
        worksheet = get_user_sheet(client, user_id)
        delete_todays_calories(worksheet)
    except Exception as e:
        logger.error(f"Sheet delete error during reset: {e}")
    await update.message.reply_text(
        "🔄 Calorie total reset to *0 kcal*.\nUse /track to start logging again.",
        parse_mode="Markdown"
    )


async def cmd_caloriegraph(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    await update.message.reply_text("📊 Generating your calorie graph, please wait...")
    try:
        client       = get_sheets_client()
        worksheet    = get_user_sheet(client, user_id)
        calorie_rows = read_data_rows(worksheet, "calories")
        if not calorie_rows:
            await update.message.reply_text("⚠️ No calorie data found. Use /track to start logging.")
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
        await update.message.reply_text("⚠️ Something went wrong. Please try again.")


async def cmd_weightgraph(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    await update.message.reply_text("📉 Generating your weight graph, please wait...")
    try:
        client      = get_sheets_client()
        worksheet   = get_user_sheet(client, user_id)
        weight_rows = read_data_rows(worksheet, "weight")
        if not weight_rows:
            await update.message.reply_text("⚠️ No weight data found. Use /updateweight to start logging.")
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
        await update.message.reply_text("⚠️ Something went wrong. Please try again.")


async def cmd_unknown(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "❓ *Unknown command.*\n\n"
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


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text    = update.message.text.strip()
    state   = user_state.get(user_id, "idle")

    if state == "awaiting_name":
        user_name[user_id]  = text
        user_state[user_id] = "awaiting_height"
        await update.message.reply_text(
            "📏 Enter your *height in centimetres* (e.g. 175):", parse_mode="Markdown"
        )

    elif state == "awaiting_height":
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
                f"🍽️ *Calorie Log Updated*\n\nTotal today: *{int(total)} kcal*\n"
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