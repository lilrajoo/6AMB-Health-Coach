# 🏋️ 6AMB Health Coach Bot


## What It Does

6AMB Health Coach is a Telegram bot designed to help unit members maintain awareness of their health metrics throughout the week. Each member registers once and the bot handles the rest, tracking calories across the day, logging weight over time, generating visual progress charts, and sending daily reminders at meal times.

All data is stored per user in Google Sheets, meaning nothing is lost between sessions and an admin can review any member's progress at any time from a single master dashboard.


## Features

### 👤 Personal Profile

Each member registers with their rank, full name, height, weight, age and gender. The bot calculates their BMI and a personalised daily calorie target using the Mifflin-St Jeor equation, displayed every time they check their profile.

### 🍽️ Calorie Tracking

Members log their meals throughout the day using `/track`. The bot accumulates a running daily total and compares it against their personal calorie target, giving contextual feedback on whether they are under, on track, or over their target for the day.

### ⚖️ Weight Logging

Members update their weight using `/updateweight`. Every update is stored with a timestamp, building a historical record that feeds directly into progress graphs.

### 📊 Progress Graphs

Two chart commands generate and send charts directly in Telegram:

- **`/caloriegraph`** – Bar chart showing average daily calorie intake per week for the last four weeks plus the current week, with a trend line.
- **`/weightgraph`** – Line chart of the last five weight entries with a trend line showing the direction of progress.

### 🔔 Daily Reminders

Members can subscribe to automated reminders using `/subscribe`. Reminders are sent Monday to Friday at:

- **14:00 SGT** – After-lunch calorie check-in.
- **20:30 SGT** – After-dinner calorie check-in.
- **Fridays at 20:30 SGT** – Dinner check-in and weekly weigh-in reminder.

### 📋 Admin Dashboard (Google Sheets)

A master sheet provides a real-time overview of any member's data. The admin selects a name from a dropdown and the sheet instantly updates to show:

- Full profile summary.
- Weight progress over the last four months.
- Weekly average calorie intake for the past month.
- Embedded charts with trend lines.


## Commands

| Command | Description |
|---------|-------------|
| `/start` | Welcome message and command list |
| `/register` | Register your profile (name, height, weight, age and gender) |
| `/user` | View your profile, BMI and daily calorie target |
| `/updateweight` | Log a new weight entry |
| `/track` | Log calorie intake (adds to today's total) |
| `/resettrack` | Reset today's calorie total to zero |
| `/caloriegraph` | Generate weekly average calorie chart |
| `/weightgraph` | Generate last five weight entries chart |
| `/subscribe` | Subscribe to daily calorie reminders |
| `/unsubscribe` | Unsubscribe from reminders |

---

*Built for internal use by the 6AMB unit.*