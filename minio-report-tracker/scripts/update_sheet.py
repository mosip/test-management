import os
import csv
import json
import random
import gspread
from datetime import datetime, timezone, timedelta
from google.oauth2.service_account import Credentials as GoogleCredentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import HttpRequest
import time

try:
    from zoneinfo import ZoneInfo
    IST = ZoneInfo("Asia/Kolkata")
except Exception:
    IST = timezone(timedelta(hours=5, minutes=30))

# Each env's job writes its own tab, and Summary writes whatever an env job could not.
SOURCE_DIR      = os.getenv("SHEET_CSV_DIR", "minio-report-tracker/csv")
STATUS_DIR      = os.getenv("SHEET_STATUS_DIR", "status")
# Where to record what was written and whether Google's quota was hit (optional).
REPORT_FILE     = os.getenv("SHEET_REPORT")
SPREADSHEET_ID  = "1UqmeHphhNNW8AfrQtT-Bl260xYRGN9elmJJa5NvM8KI"
CREDENTIALS_FILE = "creds.json"

START_ROW_INDEX = 2
START_COL       = 9
NUM_DATA_COLS   = 6
BLOCK_WIDTH     = 8

COLORS = {
    "red":              {"red": 1.0, "green": 0.0, "blue": 0.0},
    "green":            {"red": 0.0, "green": 0.6, "blue": 0.1},
    "light_grey_fill":  {"red": 0.85, "green": 0.85, "blue": 0.85}
}
BORDER = {"style": "SOLID", "width": 1}

# Google allows about 60 Sheets writes a minute per service account, and every
# env's job writes through the same one. A request refused with 429 is retried
# with backoff, and every refusal is counted so the run summary can report it.
QUOTA_ATTEMPTS = 6
quota = {"hits": 0, "waited": 0.0}


def is_quota_error(exc):
    if isinstance(exc, HttpError):
        return exc.resp.status == 429
    if isinstance(exc, gspread.exceptions.APIError):
        response = getattr(exc, "response", None)
        return getattr(exc, "code", None) == 429 or getattr(response, "status_code", None) == 429
    return False


def with_quota_retry(call):
    for attempt in range(1, QUOTA_ATTEMPTS + 1):
        try:
            return call()
        except Exception as exc:
            if not is_quota_error(exc):
                raise
            quota["hits"] += 1
            if attempt == QUOTA_ATTEMPTS:
                raise
            wait = min(2 ** attempt, 64) + random.uniform(0, 1)
            quota["waited"] += wait
            print(f"  Google Sheets quota hit, retrying in {wait:.0f}s (attempt {attempt}/{QUOTA_ATTEMPTS})")
            time.sleep(wait)


# Every Sheets API call goes through one of these two methods, so wrapping them
# covers every call site below without touching each one.
_execute = HttpRequest.execute
HttpRequest.execute = lambda self, *a, **kw: with_quota_retry(lambda: _execute(self, *a, **kw))

if hasattr(gspread, "http_client"):    # gspread 6
    _gspread_owner = gspread.http_client.HTTPClient
else:                                  # gspread 5
    _gspread_owner = gspread.Client
_gspread_request = _gspread_owner.request
_gspread_owner.request = lambda self, *a, **kw: with_quota_retry(lambda: _gspread_request(self, *a, **kw))


def find_date_col(headers, date_label):
    """
    Column of the block for date_label, or -1. The date is written as
    "02-November-2026" but Sheets stores it as a date and shows
    "2-November-2026", so compare dates, not text. Matching as text missed
    every block on days 1-9, and each run added a duplicate block.
    """
    def as_date(value):
        try:
            return datetime.strptime(str(value).strip(), "%d-%B-%Y").date()
        except ValueError:
            return None

    wanted = as_date(date_label)
    for idx, header in enumerate(headers):
        if wanted and as_date(header) == wanted:
            return idx
    return -1


def col_to_a1(col_idx):
    a1 = ""
    col_idx += 1
    while col_idx > 0:
        col_idx, remainder = divmod(col_idx - 1, 26)
        a1 = chr(65 + remainder) + a1
    return a1


def convert_cell(val):
    if val is None or str(val).strip() == '':
        return None
    try:
        f = float(val)
        return int(f) if f.is_integer() else f
    except (ValueError, TypeError):
        return str(val).strip()


def create_bake_and_delete_requests(service, sheet_id, sheet_gid, sheet_title, target_col, max_rows):
    bake_requests   = []
    delete_requests = []

    data_end_col  = target_col + NUM_DATA_COLS
    bake_range_a1 = f"'{sheet_title}'!{col_to_a1(target_col)}{START_ROW_INDEX + 1}:{col_to_a1(data_end_col - 1)}{START_ROW_INDEX + max_rows}"

    try:
        sheet_data = service.spreadsheets().get(
            spreadsheetId=sheet_id,
            ranges=[bake_range_a1],
            fields='sheets(data(rowData(values(effectiveFormat))),conditionalFormats)'
        ).execute()
    except Exception as e:
        print(f"Could not retrieve sheet data for baking: {e}")
        return [], []

    sheet_info = sheet_data.get('sheets', [{}])[0]
    rows_data  = sheet_info.get('data', [{}])[0].get('rowData', [])

    for r_idx, row in enumerate(rows_data):
        for c_idx, cell in enumerate(row.get('values', [])):
            text_format = cell.get('effectiveFormat', {}).get('textFormat', {})
            if 'foregroundColor' in text_format:
                color_api = text_format['foregroundColor']
                r = round(color_api.get('red', 0), 1)
                g = round(color_api.get('green', 0), 1)
                matched = "red" if r == 1.0 and g == 0.0 else ("green" if g == 0.6 else None)
                if matched:
                    bake_requests.append({"repeatCell": {
                        "range": {"sheetId": sheet_gid,
                                  "startRowIndex": START_ROW_INDEX + r_idx,
                                  "endRowIndex":   START_ROW_INDEX + r_idx + 1,
                                  "startColumnIndex": target_col + c_idx,
                                  "endColumnIndex":   target_col + c_idx + 1},
                        "cell":   {"userEnteredFormat": {"textFormat": {"foregroundColor": COLORS[matched]}}},
                        "fields": "userEnteredFormat.textFormat.foregroundColor"
                    }})

    all_rules = sheet_info.get('conditionalFormats', [])
    indices_to_delete = []
    for rule_index, rule in enumerate(all_rules):
        for r in rule.get('ranges', []):
            if r.get('startColumnIndex') >= target_col and r.get('endColumnIndex') <= data_end_col:
                if rule_index not in indices_to_delete:
                    indices_to_delete.append(rule_index)

    for rule_index in sorted(indices_to_delete, reverse=True):
        delete_requests.append({"deleteConditionalFormatRule": {"sheetId": sheet_gid, "index": rule_index}})

    return bake_requests, delete_requests


def apply_new_conditional_formatting(service, sheet_id, sheet_gid, target_col, max_rows):
    requests     = []
    data_end_col = target_col + NUM_DATA_COLS

    requests.append({"updateDimensionProperties": {
        "range":      {"sheetId": sheet_gid, "dimension": "COLUMNS",
                       "startIndex": target_col, "endIndex": data_end_col},
        "properties": {"pixelSize": 50}, "fields": "pixelSize"
    }})
    requests.append({"mergeCells": {
        "range":     {"sheetId": sheet_gid, "startRowIndex": 0, "endRowIndex": 1,
                      "startColumnIndex": target_col, "endColumnIndex": data_end_col},
        "mergeType": "MERGE_ALL"
    }})
    requests.append({"repeatCell": {
        "range":  {"sheetId": sheet_gid, "startRowIndex": 1, "endRowIndex": 2,
                   "startColumnIndex": target_col, "endColumnIndex": data_end_col},
        "cell":   {"userEnteredFormat": {"backgroundColor": COLORS["light_grey_fill"],
                                         "textFormat": {"bold": True}}},
        "fields": "userEnteredFormat(backgroundColor,textFormat)"
    }})

    border_range = {"sheetId": sheet_gid,
                    "startRowIndex": 0, "endRowIndex": START_ROW_INDEX + max_rows,
                    "startColumnIndex": target_col, "endColumnIndex": data_end_col}
    requests.append({"updateBorders": {"range": border_range,
                                       "top": BORDER, "bottom": BORDER,
                                       "left": BORDER, "right": BORDER}})
    requests.append({"updateBorders": {"range": border_range,
                                       "innerHorizontal": BORDER, "innerVertical": BORDER}})

    for i, col_header in enumerate(["T", "P", "S", "F", "I", "KI"]):
        current_col_idx = target_col + i
        ref_col_idx     = 1 + i
        rule_range      = {"sheetId": sheet_gid,
                           "startRowIndex": START_ROW_INDEX, "endRowIndex": START_ROW_INDEX + max_rows,
                           "startColumnIndex": current_col_idx, "endColumnIndex": current_col_idx + 1}
        cur = f"{col_to_a1(current_col_idx)}{START_ROW_INDEX + 1}"
        ref = f"{col_to_a1(ref_col_idx)}{START_ROW_INDEX + 1}"

        conditions = {
            "T":  (f"{cur}={ref}",  f"{cur}<>{ref}"),
            "P":  (f"{cur}>={ref}", f"{cur}<{ref}"),
            "S":  (f"{cur}<={ref}", f"{cur}>{ref}"),
            "F":  (f"{cur}<={ref}", f"{cur}>{ref}"),
            "I":  (f"{cur}<={ref}", f"{cur}>{ref}"),
            "KI": (f"{cur}<={ref}", f"{cur}>{ref}")
        }
        green_cond, red_cond = conditions[col_header]

        for idx, formula, color in [
            (0, f"=AND(NOT(ISBLANK({cur})),NOT(ISBLANK({ref})),{green_cond})", "green"),
            (1, f"=AND(NOT(ISBLANK({cur})),NOT(ISBLANK({ref})),{red_cond})",   "red"),
            (2, f"=AND(NOT(ISBLANK({cur})),ISBLANK({ref}))",                   "red"),
        ]:
            requests.append({"addConditionalFormatRule": {
                "rule": {"ranges": [rule_range], "booleanRule": {
                    "condition": {"type": "CUSTOM_FORMULA", "values": [{"userEnteredValue": formula}]},
                    "format":    {"textFormat": {"foregroundColor": COLORS[color]}}
                }},
                "index": idx
            }})

    if requests:
        service.spreadsheets().batchUpdate(spreadsheetId=sheet_id, body={"requests": requests}).execute()


def add_failure_note(service, spreadsheet, sheet_name, reason):
    """
    For an env that failed entirely, inserts a full date block (same structure
    as a successful update) with empty data rows and the failure reason as a
    hover note on the date header cell.
    """
    today = datetime.now(tz=IST).strftime("%d-%B-%Y")
    print(f"\n--- Adding failure block to sheet '{sheet_name}' for {today} ---")

    try:
        sheet = spreadsheet.worksheet(sheet_name)
    except gspread.exceptions.WorksheetNotFound:
        print(f"  Sheet '{sheet_name}' not found. Creating it.")
        sheet = spreadsheet.add_worksheet(title=sheet_name, rows="100", cols="50")

    existing_data      = sheet.get_all_values()
    master_module_list = [row[0] for row in existing_data[START_ROW_INDEX:] if row and row[0]] \
                         if len(existing_data) > START_ROW_INDEX else []
    max_rows   = max(len(master_module_list), 1)
    sheet_headers = existing_data[0] if existing_data else []

    # An earlier run already wrote today's block; its data stands, so do not
    # mark it FAILED.
    if find_date_col(sheet_headers, today) != -1:
        print(f"  '{today}' already has a block on '{sheet_name}' from an earlier run; leaving it as is.")
        return

    # Insert the date column
    target_col = -1

    if target_col == -1:
        target_col = START_COL
        if len(sheet_headers) > START_COL and sheet_headers[START_COL]:
            bake_reqs, delete_reqs = create_bake_and_delete_requests(
                service, SPREADSHEET_ID, sheet.id, sheet.title, START_COL, max_rows
            )
            if bake_reqs:
                service.spreadsheets().batchUpdate(
                    spreadsheetId=SPREADSHEET_ID, body={"requests": bake_reqs}
                ).execute()
                time.sleep(3)
            insert_req = {"insertDimension": {
                "range": {"sheetId": sheet.id, "dimension": "COLUMNS",
                          "startIndex": START_COL, "endIndex": START_COL + BLOCK_WIDTH},
                "inheritFromBefore": False
            }}
            service.spreadsheets().batchUpdate(
                spreadsheetId=SPREADSHEET_ID,
                body={"requests": delete_reqs + [insert_req]}
            ).execute()
        else:
            insert_req = {"insertDimension": {
                "range": {"sheetId": sheet.id, "dimension": "COLUMNS",
                          "startIndex": START_COL, "endIndex": START_COL + BLOCK_WIDTH},
                "inheritFromBefore": False
            }}
            service.spreadsheets().batchUpdate(
                spreadsheetId=SPREADSHEET_ID, body={"requests": [insert_req]}
            ).execute()

    # Write date header, column headers, empty data rows
    empty_rows = [[None] * NUM_DATA_COLS + [None] for _ in range(max_rows)]
    update_body = {
        "valueInputOption": "USER_ENTERED",
        "data": [
            {"range": f"'{sheet.title}'!{col_to_a1(target_col)}1",
             "values": [[today]]},
            {"range": f"'{sheet.title}'!{col_to_a1(target_col)}2",
             "values": [["T", "P", "S", "F", "I", "KI", "PO"]]},
            {"range": f"'{sheet.title}'!{col_to_a1(target_col)}{START_ROW_INDEX + 1}",
             "values": empty_rows}
        ]
    }
    service.spreadsheets().values().batchUpdate(
        spreadsheetId=SPREADSHEET_ID, body=update_body
    ).execute()

    # Apply the same borders, merge, and grey header as a normal update
    apply_new_conditional_formatting(service, SPREADSHEET_ID, sheet.id, target_col, max_rows)

    # Add failure reason as hover note on the date header cell
    note_text = f"Run: {today}\nStatus: FAILED\n\nReason:\n{reason}"
    service.spreadsheets().batchUpdate(
        spreadsheetId=SPREADSHEET_ID,
        body={"requests": [{
            "updateCells": {
                "range": {"sheetId": sheet.id,
                          "startRowIndex": 0, "endRowIndex": 1,
                          "startColumnIndex": target_col, "endColumnIndex": target_col + 1},
                "rows":   [{"values": [{"note": note_text}]}],
                "fields": "note"
            }
        }]}
    ).execute()
    print(f"  Failure block added to '{sheet_name}' at column {col_to_a1(target_col)}.")


def update_sheet(service, spreadsheet, sheet_name, csv_path):
    try:
        print(f"\n--- Processing: {sheet_name} from {csv_path} ---")
        sheet = spreadsheet.worksheet(sheet_name)
    except gspread.exceptions.WorksheetNotFound:
        print(f"Worksheet '{sheet_name}' not found. Creating it.")
        sheet = spreadsheet.add_worksheet(title=sheet_name, rows="100", cols="50")

    first_block_rows = []
    with open(csv_path, 'r', encoding='utf-8') as f:
        reader = csv.reader(f)
        for row in reader:
            if any(cell.strip() for cell in row[:10]):
                first_block_rows.append(row[:10])

    if len(first_block_rows) < 2:
        print(f"WARNING: CSV '{csv_path}' seems malformed. Skipping.")
        return

    date_label   = first_block_rows[1][0].strip()
    csv_headers  = [h.strip() for h in first_block_rows[0][2:2 + NUM_DATA_COLS]]
    csv_data_map = {
        row[1].strip(): [convert_cell(c) for c in row[2:2 + NUM_DATA_COLS]]
        for row in first_block_rows[1:]
        if len(row) > 1 and row[1].strip()
    }
    print(f"  Found date '{date_label}' with {len(csv_data_map)} modules in CSV.")

    existing_data      = sheet.get_all_values()
    master_module_list = [row[0] for row in existing_data[START_ROW_INDEX:] if row and row[0]] \
                         if len(existing_data) > START_ROW_INDEX else []

    new_modules = sorted([m for m in csv_data_map if m not in master_module_list])
    if new_modules:
        print(f"  New modules found: {', '.join(new_modules)}. Appending.")
        sheet.append_rows(
            values=[[m] for m in new_modules],
            value_input_option='USER_ENTERED',
            table_range=f"A{len(master_module_list) + START_ROW_INDEX + 1}"
        )
        master_module_list.extend(new_modules)

    aligned_data_block = [csv_data_map.get(m, [None] * NUM_DATA_COLS) for m in master_module_list]

    sheet_headers = existing_data[0] if existing_data else []
    target_col    = find_date_col(sheet_headers, date_label)

    if target_col == -1:
        print(f"  Date '{date_label}' not found. Beginning column insertion process.")
        target_col = START_COL

        if len(sheet_headers) > START_COL and sheet_headers[START_COL]:
            bake_reqs, delete_reqs = create_bake_and_delete_requests(
                service, SPREADSHEET_ID, sheet.id, sheet.title, START_COL, len(master_module_list)
            )
            if bake_reqs:
                service.spreadsheets().batchUpdate(
                    spreadsheetId=SPREADSHEET_ID, body={"requests": bake_reqs}
                ).execute()
                time.sleep(3)

            insert_req = {"insertDimension": {
                "range": {"sheetId": sheet.id, "dimension": "COLUMNS",
                          "startIndex": START_COL, "endIndex": START_COL + BLOCK_WIDTH},
                "inheritFromBefore": False
            }}
            service.spreadsheets().batchUpdate(
                spreadsheetId=SPREADSHEET_ID,
                body={"requests": delete_reqs + [insert_req]}
            ).execute()
        else:
            insert_req = {"insertDimension": {
                "range": {"sheetId": sheet.id, "dimension": "COLUMNS",
                          "startIndex": START_COL, "endIndex": START_COL + BLOCK_WIDTH},
                "inheritFromBefore": False
            }}
            service.spreadsheets().batchUpdate(
                spreadsheetId=SPREADSHEET_ID, body={"requests": [insert_req]}
            ).execute()
    else:
        print(f"  Date '{date_label}' found at column {col_to_a1(target_col)}. Updating in place.")

    update_body = {
        "valueInputOption": "USER_ENTERED",
        "data": [
            {"range": f"'{sheet.title}'!{col_to_a1(target_col)}1",  "values": [[date_label]]},
            {"range": f"'{sheet.title}'!{col_to_a1(target_col)}2",  "values": [csv_headers + ["PO"]]},
            {"range": f"'{sheet.title}'!{col_to_a1(target_col)}{START_ROW_INDEX + 1}",
             "values": [row + [None] for row in aligned_data_block]}
        ]
    }
    service.spreadsheets().values().batchUpdate(
        spreadsheetId=SPREADSHEET_ID, body=update_body
    ).execute()

    apply_new_conditional_formatting(service, SPREADSHEET_ID, sheet.id, target_col, len(master_module_list))
    print(f"Sheet '{sheet_name}' updated successfully.")


def main():
    try:
        scope = [
            "https://spreadsheets.google.com/feeds",
            "https://www.googleapis.com/auth/drive",
            "https://www.googleapis.com/auth/spreadsheets"
        ]
        creds   = GoogleCredentials.from_service_account_file(CREDENTIALS_FILE, scopes=scope)
        service = build("sheets", "v4", credentials=creds)

        from oauth2client.service_account import ServiceAccountCredentials as GSpreadCredentials
        gspread_creds = GSpreadCredentials.from_json_keyfile_name(CREDENTIALS_FILE, scope)
        client        = gspread.authorize(gspread_creds)
        spreadsheet   = client.open_by_key(SPREADSHEET_ID)

        # One tab failing (e.g. quota still exhausted after every retry) must not
        # stop the others; it is recorded and the run summary reports it.
        written, not_written = [], {}

        # Update sheets for envs that produced a CSV
        if os.path.isdir(SOURCE_DIR):
            for filename in sorted(f for f in os.listdir(SOURCE_DIR) if f.endswith(".csv")):
                sheet_name = os.path.splitext(filename)[0]
                try:
                    update_sheet(service, spreadsheet, sheet_name, os.path.join(SOURCE_DIR, filename))
                    written.append(sheet_name)
                except Exception as e:
                    print(f"Could not update '{sheet_name}': {e}")
                    not_written[sheet_name] = str(e)

        # Add failure notes for envs that failed entirely (nothing could be fetched)
        if os.path.isdir(STATUS_DIR):
            for sf in sorted(f for f in os.listdir(STATUS_DIR)
                             if f.startswith("status_") and f.endswith(".json")):
                alias = sf.removeprefix("status_").removesuffix(".json")
                try:
                    with open(os.path.join(STATUS_DIR, sf)) as f:
                        data = json.load(f)
                    reason = data.get("reason", "").strip()
                    if alias in data.get("failed", []) and reason:
                        add_failure_note(service, spreadsheet, alias, reason)
                        written.append(alias)
                except (json.JSONDecodeError, IOError) as e:
                    print(f"Could not read status for '{alias}': {e}")
                except Exception as e:
                    print(f"Could not add the failure block to '{alias}': {e}")
                    not_written[alias] = str(e)

        print(f"Google Sheets quota hits: {quota['hits']}, waited {quota['waited']:.0f}s")
        if REPORT_FILE:
            with open(REPORT_FILE, "w", encoding="utf-8") as handle:
                json.dump({"written": written, "not_written": not_written,
                           "quota_hits": quota["hits"], "quota_waited_s": round(quota["waited"])},
                          handle, indent=2)
        if not_written:
            raise SystemExit(f"Not written: {', '.join(not_written)}")

    except FileNotFoundError:
        print(f"CRITICAL ERROR: Credentials file '{CREDENTIALS_FILE}' not found.")
        raise
    except Exception as e:
        print(f"A critical error occurred in main(): {e}")
        raise


if __name__ == "__main__":
    main()
