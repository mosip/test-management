import os
import csv
import gspread
from google.oauth2.service_account import Credentials as GoogleCredentials
from googleapiclient.discovery import build
import time
import json

# --- CONFIGURATION ---
SOURCE_DIR = "minio-report-tracker/csv"
SPREADSHEET_ID = "1gpV5T5Ol45VqmS8nI6Xk2MXWEeJMiXU1yoFUDMODi6g"
CREDENTIALS_FILE = "creds.json"

# --- CONSTANTS ---
START_ROW_INDEX = 2
START_COL = 9 # This is column J
NUM_DATA_COLS = 6
BLOCK_WIDTH = 9 # The number of columns each block spans

# --- FORMATTING STYLES ---
COLORS = {
    "red": {"red": 1.0, "green": 0.0, "blue": 0.0},
    "green": {"red": 0.0, "green": 0.6, "blue": 0.1},
    "light_grey_fill": {"red": 0.85, "green": 0.85, "blue": 0.85}
}
BORDER = {"style": "SOLID", "width": 1}

def col_to_a1(col_idx):
    """Converts a 0-indexed column number to A1 notation."""
    a1 = ""
    col_idx += 1
    while col_idx > 0:
        col_idx, remainder = divmod(col_idx - 1, 26)
        a1 = chr(65 + remainder) + a1
    return a1

def convert_cell(val):
    """Safely converts a string value to a number or returns it as a string."""
    if val is None or str(val).strip() == '':
        return None
    try:
        f = float(val)
        return int(f) if f.is_integer() else f
    except (ValueError, TypeError):
        return str(val).strip()

def create_bake_and_delete_requests(service, sheet_id, sheet_gid, sheet_title, target_col, max_rows):
    """
    Analyzes the sheet and returns two separate lists of requests:
    1. A list of 'repeatCell' requests to bake colors.
    2. A list of 'deleteConditionalFormatRule' requests.
    """
    print(f"\n[DEBUG] --- Creating Bake & Delete Requests ---")
    print(f"[DEBUG] Sheet: '{sheet_title}', Target Column: {col_to_a1(target_col)}")
    
    bake_requests = []
    delete_requests = []
    
    data_end_col = target_col + NUM_DATA_COLS
    bake_range_a1 = f"'{sheet_title}'!{col_to_a1(target_col)}{START_ROW_INDEX + 1}:{col_to_a1(data_end_col - 1)}{START_ROW_INDEX + max_rows}"

    try:
        sheet_data = service.spreadsheets().get(
            spreadsheetId=sheet_id,
            ranges=[bake_range_a1],
            fields='sheets(data(rowData(values(effectiveFormat))),conditionalFormats)'
        ).execute()
    except Exception as e:
        print(f"[DEBUG] ❌ ERROR: Could not retrieve sheet data for baking. Error: {e}")
        return [], []

    sheet_info = sheet_data.get('sheets', [{}])[0]
    rows_data = sheet_info.get('data', [{}])[0].get('rowData', [])
    
    if not rows_data:
        print(f"[DEBUG] ⚠️ WARNING: No rowData returned. Cannot bake formats.")
        return [], []
    
    for r_idx, row in enumerate(rows_data):
        for c_idx, cell in enumerate(row.get('values', [])):
            effective_format = cell.get('effectiveFormat', {})
            text_format = effective_format.get('textFormat', {})
            if 'foregroundColor' in text_format:
                color_api = text_format['foregroundColor']
                r, g, b = round(color_api.get('red', 0), 1), round(color_api.get('green', 0), 1), round(color_api.get('blue', 0), 1)
                matched_color_name = "red" if r == 1.0 and g == 0.0 else ("green" if g == 0.6 else None)
                if matched_color_name:
                    bake_requests.append({"repeatCell": {"range": {"sheetId": sheet_gid, "startRowIndex": START_ROW_INDEX + r_idx, "endRowIndex": START_ROW_INDEX + r_idx + 1, "startColumnIndex": target_col + c_idx, "endColumnIndex": target_col + c_idx + 1}, "cell": {"userEnteredFormat": {"textFormat": {"foregroundColor": COLORS[matched_color_name]}}}, "fields": "userEnteredFormat.textFormat.foregroundColor"}})
    
    indices_to_delete = []
    all_rules = sheet_info.get('conditionalFormats', [])
    for rule_index, rule in enumerate(all_rules):
        for r in rule.get('ranges', []):
            if r.get('startColumnIndex') >= target_col and r.get('endColumnIndex') <= data_end_col:
                if rule_index not in indices_to_delete:
                    indices_to_delete.append(rule_index)
    
    indices_to_delete.sort(reverse=True)
    for rule_index in indices_to_delete:
        delete_requests.append({"deleteConditionalFormatRule": {"sheetId": sheet_gid, "index": rule_index}})

    print(f"[DEBUG] Created {len(bake_requests)} bake requests and {len(delete_requests)} delete requests.")
    return bake_requests, delete_requests

def apply_new_conditional_formatting(service, sheet_id, sheet_gid, target_col, max_rows):
    """Builds and executes all formatting requests for a NEW data block."""
    print("  -> Building and applying new formatting requests...")
    requests = []
    data_end_col = target_col + NUM_DATA_COLS

    # Standard Formatting
    requests.append({"updateDimensionProperties": {"range": {"sheetId": sheet_gid, "dimension": "COLUMNS", "startIndex": target_col, "endIndex": data_end_col}, "properties": {"pixelSize": 50}, "fields": "pixelSize"}})
    requests.append({"mergeCells": {"range": {"sheetId": sheet_gid, "startRowIndex": 0, "endRowIndex": 1, "startColumnIndex": target_col, "endColumnIndex": data_end_col}, "mergeType": "MERGE_ALL"}})
    
    # --- FIX IS HERE ---
    # The key 'endIndex' has been corrected to 'endColumnIndex'.
    requests.append({"repeatCell": {"range": {"sheetId": sheet_gid, "startRowIndex": 1, "endRowIndex": 2, "startColumnIndex": target_col, "endColumnIndex": data_end_col}, "cell": {"userEnteredFormat": {"backgroundColor": COLORS["light_grey_fill"], "textFormat": {"bold": True}}}, "fields": "userEnteredFormat(backgroundColor,textFormat)"}})
    
    border_range = {"sheetId": sheet_gid, "startRowIndex": 0, "endRowIndex": START_ROW_INDEX + max_rows, "startColumnIndex": target_col, "endColumnIndex": data_end_col}
    requests.append({"updateBorders": {"range": border_range, "top": BORDER, "bottom": BORDER, "left": BORDER, "right": BORDER}})
    requests.append({"updateBorders": {"range": border_range, "innerHorizontal": BORDER, "innerVertical": BORDER}})

    # Conditional Formatting
    for i, col_header in enumerate(["T", "P", "S", "F", "I", "KI"]):
        current_col_idx = target_col + i
        ref_col_idx = 1 + i 
        
        rule_range = {"sheetId": sheet_gid, "startRowIndex": START_ROW_INDEX, "endRowIndex": START_ROW_INDEX + max_rows, "startColumnIndex": current_col_idx, "endColumnIndex": current_col_idx + 1}
        current_cell_a1 = f"{col_to_a1(current_col_idx)}{START_ROW_INDEX + 1}"
        ref_cell_a1 = f"{col_to_a1(ref_col_idx)}{START_ROW_INDEX + 1}"
        
        conditions = {
            "T":  (f"{current_cell_a1}={ref_cell_a1}", f"{current_cell_a1}<>{ref_cell_a1}"),
            "P":  (f"{current_cell_a1}>={ref_cell_a1}", f"{current_cell_a1}<{ref_cell_a1}"),
            "S":  (f"{current_cell_a1}<={ref_cell_a1}", f"{current_cell_a1}>{ref_cell_a1}"),
            "F":  (f"{current_cell_a1}<={ref_cell_a1}", f"{current_cell_a1}>{ref_cell_a1}"),
            "I":  (f"{current_cell_a1}<={ref_cell_a1}", f"{current_cell_a1}>{ref_cell_a1}"),
            "KI": (f"{current_cell_a1}<={ref_cell_a1}", f"{current_cell_a1}>{ref_cell_a1}")
        }
        green_cond, red_cond = conditions[col_header]

        green_formula = f"=AND(NOT(ISBLANK({current_cell_a1})), NOT(ISBLANK({ref_cell_a1})), {green_cond})"
        red_formula_comp = f"=AND(NOT(ISBLANK({current_cell_a1})), NOT(ISBLANK({ref_cell_a1})), {red_cond})"
        red_formula_no_ref = f"=AND(NOT(ISBLANK({current_cell_a1})), ISBLANK({ref_cell_a1}))"

        requests.append({"addConditionalFormatRule": {"rule": {"ranges": [rule_range], "booleanRule": {"condition": {"type": "CUSTOM_FORMULA", "values": [{"userEnteredValue": green_formula}]}, "format": {"textFormat": {"foregroundColor": COLORS["green"]}}}}, "index": 0}})
        requests.append({"addConditionalFormatRule": {"rule": {"ranges": [rule_range], "booleanRule": {"condition": {"type": "CUSTOM_FORMULA", "values": [{"userEnteredValue": red_formula_comp}]}, "format": {"textFormat": {"foregroundColor": COLORS["red"]}}}}, "index": 1}})
        requests.append({"addConditionalFormatRule": {"rule": {"ranges": [rule_range], "booleanRule": {"condition": {"type": "CUSTOM_FORMULA", "values": [{"userEnteredValue": red_formula_no_ref}]}, "format": {"textFormat": {"foregroundColor": COLORS["red"]}}}}, "index": 2}})

    if requests:
        service.spreadsheets().batchUpdate(spreadsheetId=sheet_id, body={"requests": requests}).execute()

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
        print(f"⚠️ WARNING: CSV '{csv_path}' seems malformed. Skipping.")
        return

    date_label = first_block_rows[1][0].strip()
    csv_headers = [h.strip() for h in first_block_rows[0][2:2 + NUM_DATA_COLS]]
    csv_data_map = {row[1].strip(): [convert_cell(c) for c in row[2:2 + NUM_DATA_COLS]] for row in first_block_rows[1:] if len(row) > 1 and row[1].strip()}
    print(f"  -> Found date '{date_label}' with {len(csv_data_map)} modules in CSV.")

    existing_data = sheet.get_all_values()
    master_module_list = [row[0] for row in existing_data[START_ROW_INDEX:] if row and row[0]] if len(existing_data) > START_ROW_INDEX else []
    
    new_modules = sorted([m for m in csv_data_map if m not in master_module_list])
    if new_modules:
        print(f"  -> New modules found: {', '.join(new_modules)}. Appending.")
        sheet.append_rows(values=[[m] for m in new_modules], value_input_option='USER_ENTERED', table_range=f"A{len(master_module_list) + START_ROW_INDEX + 1}")
        master_module_list.extend(new_modules)
    
    aligned_data_block = [csv_data_map.get(m, [None] * NUM_DATA_COLS) for m in master_module_list]

    sheet_headers = existing_data[0] if existing_data else []
    target_col = sheet_headers.index(date_label) if date_label in sheet_headers else -1

    if target_col == -1:
        print(f"  -> Date '{date_label}' not found. Beginning column insertion process.")
        target_col = START_COL
        
        if len(sheet_headers) > START_COL and sheet_headers[START_COL]:
            bake_reqs, delete_reqs = create_bake_and_delete_requests(service, SPREADSHEET_ID, sheet.id, sheet.title, START_COL, len(master_module_list))

            if bake_reqs:
                print(f"[DEBUG] STAGE 1: Sending {len(bake_reqs)} requests to bake colors.")
                service.spreadsheets().batchUpdate(spreadsheetId=SPREADSHEET_ID, body={"requests": bake_reqs}).execute()
                print("[DEBUG] Bake request sent. Pausing for 3 seconds...")
                time.sleep(3)
            
            requests_for_stage_2 = delete_reqs
            print(f"[DEBUG] STAGE 2: Preparing to delete {len(delete_reqs)} rules and insert columns.")
            
            insert_req = {"insertDimension": {"range": {"sheetId": sheet.id, "dimension": "COLUMNS", "startIndex": START_COL, "endIndex": START_COL + BLOCK_WIDTH}, "inheritFromBefore": False}}
            requests_for_stage_2.append(insert_req)

            print(f"[DEBUG] Sending {len(requests_for_stage_2)} requests for deletion and insertion.")
            service.spreadsheets().batchUpdate(spreadsheetId=SPREADSHEET_ID, body={"requests": requests_for_stage_2}).execute()
            print("[DEBUG] Deletion and insertion complete.")
        
        else:
            print("  -> No existing data at insertion point. Inserting new columns directly.")
            insert_req = {"insertDimension": {"range": {"sheetId": sheet.id, "dimension": "COLUMNS", "startIndex": START_COL, "endIndex": START_COL + BLOCK_WIDTH}, "inheritFromBefore": False}}
            service.spreadsheets().batchUpdate(spreadsheetId=SPREADSHEET_ID, body={"requests": [insert_req]}).execute()

    else:
        print(f"  -> Date '{date_label}' found. Updating columns in place.")

    update_body = {"valueInputOption": "USER_ENTERED", "data": [{"range": f"'{sheet.title}'!{col_to_a1(target_col)}1", "values": [[date_label]]}, {"range": f"'{sheet.title}'!{col_to_a1(target_col)}2", "values": [csv_headers]}, {"range": f"'{sheet.title}'!{col_to_a1(target_col)}{START_ROW_INDEX + 1}", "values": aligned_data_block}]}
    print(f"  -> Writing data to sheet '{sheet.title}' starting at column {col_to_a1(target_col)}.")
    service.spreadsheets().values().batchUpdate(spreadsheetId=SPREADSHEET_ID, body=update_body).execute()
    
    apply_new_conditional_formatting(service, SPREADSHEET_ID, sheet.id, target_col, len(master_module_list))
    
    print(f"✅ Sheet '{sheet_name}' updated successfully.")

def main():
    try:
        scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive", "https://www.googleapis.com/auth/spreadsheets"]
        creds = GoogleCredentials.from_service_account_file(CREDENTIALS_FILE, scopes=scope)
        service = build("sheets", "v4", credentials=creds)
        
        from oauth2client.service_account import ServiceAccountCredentials as GSpreadCredentials
        gspread_creds = GSpreadCredentials.from_json_keyfile_name(CREDENTIALS_FILE, scope)
        client = gspread.authorize(gspread_creds)
        spreadsheet = client.open_by_key(SPREADSHEET_ID)

        if not os.path.isdir(SOURCE_DIR):
            print(f"❌ ERROR: Source directory '{SOURCE_DIR}' not found.")
            return

        csv_files = sorted([f for f in os.listdir(SOURCE_DIR) if f.endswith(".csv")])
        if not csv_files:
            print("No CSV files found in 'source' directory. Nothing to do.")
            return

        for filename in csv_files:
            sheet_name = os.path.splitext(filename)[0]
            csv_path = os.path.join(SOURCE_DIR, filename)
            update_sheet(service, spreadsheet, sheet_name, csv_path)

    except FileNotFoundError:
        print(f"❌ CRITICAL ERROR: Credentials file '{CREDENTIALS_FILE}' not found.")
    except Exception as e:
        print(f"❌ A critical error occurred in main(): {e}")
        raise

if __name__ == "__main__":
    main()
