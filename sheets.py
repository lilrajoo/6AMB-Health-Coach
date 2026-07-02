import os
import json
import logging
from datetime import datetime
import gspread
from google.oauth2.service_account import Credentials
from state import subscribed_users

logger = logging.getLogger(__name__)

SHEETS_CREDS_RAW = os.environ.get("SHEETS_CREDENTIALS")
SHEETS_ID        = os.environ.get("SHEETS_ID")


def get_sheets_client():
    creds_dict = json.loads(SHEETS_CREDS_RAW)
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive"
    ]
    creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    return gspread.authorize(creds)


def get_user_sheet(client, user_id):
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
    worksheet.update("A1:F1", [[name, height, age, gender, weight, str(subscribed)]])


def write_subscription_status(user_id, subscribed):
    try:
        client    = get_sheets_client()
        worksheet = get_user_sheet(client, user_id)
        worksheet.update("F1", [[str(subscribed)]])
    except Exception as e:
        logger.error(f"Subscription write error {user_id}: {e}")


def append_data_row(worksheet, entry_type, value):
    today      = datetime.now().strftime("%Y-%m-%d")
    all_values = worksheet.get_all_values()
    next_row   = max(len(all_values) + 1, 4)
    worksheet.update(f"A{next_row}:C{next_row}", [[today, entry_type, value]])


def read_data_rows(worksheet, entry_type):
    all_values = worksheet.get_all_values()
    rows       = []
    for row in all_values[3:]:
        if len(row) >= 3 and row[1].strip().lower() == entry_type:
            try:
                rows.append((row[0].strip(), float(row[2].strip())))
            except ValueError:
                continue
    return rows


def load_subscriptions_from_sheets():
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