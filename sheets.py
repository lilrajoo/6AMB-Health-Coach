import os
import json
import logging
from datetime import datetime
import gspread
from google.oauth2.service_account import Credentials


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
    # subscribed column removed — reminders now fire to all registered users
    worksheet.update("A1:E1", [[name, height, age, gender, weight]])




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



def get_todays_calories(worksheet):
    # Reads all calorie entries from today and returns the sum
    today      = datetime.now().strftime("%Y-%m-%d")
    all_values = worksheet.get_all_values()
    total      = 0
    for row in all_values[3:]:
        if len(row) >= 3 and row[1].strip().lower() == "calories" and row[0].strip() == today:
            try:
                total += float(row[2].strip())
            except ValueError:
                continue
    return total


def delete_todays_calories(worksheet):
    # Deletes all calorie rows logged today from the sheet
    # Iterates in reverse so row indices don't shift during deletion
    today      = datetime.now().strftime("%Y-%m-%d")
    all_values = worksheet.get_all_values()
    rows_to_delete = []

    for i, row in enumerate(all_values[3:], start=4):  # start=4 because rows 1-3 are headers
        if len(row) >= 3 and row[1].strip().lower() == "calories" and row[0].strip() == today:
            rows_to_delete.append(i)

    # Delete in reverse order so indices remain valid
    for row_index in reversed(rows_to_delete):
        worksheet.delete_rows(row_index)