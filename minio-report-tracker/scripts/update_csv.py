import os
import re
import json
import subprocess
import pandas as pd
from datetime import datetime, timezone, timedelta
try:
    from zoneinfo import ZoneInfo 
    IST = ZoneInfo("Asia/Kolkata")
except Exception:
    # Fallback if zoneinfo/tzdata is unavailable
    IST = timezone(timedelta(hours=5, minutes=30))

MINIO_ALIASES = ["cellbox21", "collab", "dev-int", "dev3", "released", "dev1", "qa-base", "qa-country", "qa1-java21", "qatest1"]
MINIO_BUCKETS = ["apitestrig", "automation", "dslreports", "uitestrig"]
columns = ["Date", "Module", "T", "P", "S", "F", "I", "KI"]

def format_date_str(dt):
    return dt.strftime("%d-%B-%Y")

def date_key_from_minio_ts(ts: str) -> str:
    """
    Convert MinIO 'lastModified' (UTC, e.g. '2025-08-13T21:25:10.000Z')
    to an IST date key like '14-August-2025'.
    """
    # Normalize 'Z' to '+00:00' so fromisoformat can parse
    ts_norm = ts.replace('Z', '+00:00')
    try:
        dt = datetime.fromisoformat(ts_norm)
    except ValueError:
        # Fallback: parse without fractional seconds
        dt = datetime.strptime(ts_norm[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
    dt_ist = dt.astimezone(IST)
    return format_date_str(dt_ist)

for alias in MINIO_ALIASES:
    csv_filename = f"{alias}.csv"
    all_data_by_date = {}

    for bucket in MINIO_BUCKETS:
        folders = []

        # --- Special handling for dslreports ---
        if bucket == "dslreports":
            cmd = f"mc ls --json {alias}/dslreports/full/"
            output = subprocess.getoutput(cmd)
            for line in output.splitlines():
                try:
                    info = json.loads(line)
                    fn = info["key"]
                    if fn.startswith("ExtentReport-"):
                        continue
                    ts = info["lastModified"]
                    date_key = date_key_from_minio_ts(ts)
                except (json.JSONDecodeError, KeyError, ValueError):
                    continue

                if not re.search(r"report_T-\d+_P-\d+(?:_KI-\d+)?(?:_I-\d+)?_S-\d+_F-\d+", fn):
                    continue

                m = re.search(r"report_T-(\d+)_P-(\d+)(?:_KI-(\d+))?(?:_I-(\d+))?_S-(\d+)_F-(\d+)", fn)
                if not m:
                    continue

                T, P, KI, I, S, F = m.groups()
                I = I or "0"
                KI = KI or "0"

                if date_key not in all_data_by_date:
                    all_data_by_date[date_key] = []
                if not any(row[1] == "dsl" for row in all_data_by_date[date_key]):
                    all_data_by_date[date_key].append([date_key, "dsl", T, P, S, F, I, KI])
            continue

        # --- Special handling for uitestrig - ADMINUI, RESIDENT directly ---
        if bucket == "uitestrig":
            cmd = f"mc ls --json {alias}/{bucket}/"
            lines = subprocess.getoutput(cmd).splitlines()
            for line in lines:
                try:
                    info = json.loads(line)
                    fn = info["key"]
                    ts = info["lastModified"]
                    date_key = date_key_from_minio_ts(ts)
                except (json.JSONDecodeError, KeyError, ValueError):
                    continue

                if not re.search(r"-report_T-\d+_P-\d+_S-\d+_F-\d+\.html$", fn):
                    continue

                mod_match = re.match(r"(ADMINUI|RESIDENT)-api-", fn)
                if mod_match:
                    mod_raw = mod_match.group(1).lower()
                    mod = "residentui" if mod_raw == "resident" else mod_raw
                    m = re.search(r"report_T-(\d+)_P-(\d+)_S-(\d+)_F-(\d+)", fn)
                    if not m:
                        continue
                    T, P, S, F = m.groups()
                    I, KI = "0", "0"

                    if date_key not in all_data_by_date:
                        all_data_by_date[date_key] = []
                    if not any(row[1] == mod for row in all_data_by_date[date_key]):
                        all_data_by_date[date_key].append([date_key, mod, T, P, S, F, I, KI])

            # Do not 'continue' here — allow folder logic to run for PMPUI

        # --- List folders ---
        cmd = f"mc ls --json {alias}/{bucket}/"
        output = subprocess.getoutput(cmd)
        for line in output.splitlines():
            try:
                folders.append(json.loads(line)["key"].strip("/"))
            except (json.JSONDecodeError, KeyError):
                continue

        if not folders:
            continue

        for folder in folders:
            # --- Special case: PMPUI ---
            if bucket == "uitestrig" and folder.lower() == "pmpui":
                folder_path = f"{bucket}/{folder}"
                cmd = f"mc ls --json {alias}/{folder_path}/"
                lines = subprocess.getoutput(cmd).splitlines()
                for line in lines:
                    try:
                        info = json.loads(line)
                        fn = info["key"]
                        ts = info["lastModified"]
                        date_key = date_key_from_minio_ts(ts)
                    except (json.JSONDecodeError, KeyError, ValueError):
                        continue

                    if not re.search(r"PMPUI-.*-report_T-\d+_P-\d+_S-\d+_F-\d+", fn):
                        continue
                    m = re.search(r"report_T-(\d+)_P-(\d+)_S-(\d+)_F-(\d+)", fn)
                    if not m:
                        continue
                    T, P, S, F = m.groups()
                    I, KI = "0", "0"
                    mod = "pmpui"

                    if date_key not in all_data_by_date:
                        all_data_by_date[date_key] = []
                    if not any(row[1] == mod for row in all_data_by_date[date_key]):
                        all_data_by_date[date_key].append([date_key, mod, T, P, S, F, I, KI])
                continue

            # --- Default logic for other folders ---
            folder_path = f"{bucket}/{folder}"
            cmd = f"mc ls --json {alias}/{folder_path}/"
            lines = subprocess.getoutput(cmd).splitlines()
            for line in lines:
                try:
                    info = json.loads(line)
                    fn = info["key"]
                    ts = info["lastModified"]
                    date_key = date_key_from_minio_ts(ts)
                except (json.JSONDecodeError, KeyError, ValueError):
                    continue

                if "error-report" in fn:
                    continue
                if not re.search(r"(full-)?report_T-\d+_P-\d+_S-\d+_F-", fn):
                    continue
                m = re.search(r"report_T-(\d+)_P-(\d+)_S-(\d+)_F-(\d+)(?:_I-(\d+))?(?:_KI-(\d+))?", fn)
                if not m:
                    continue
                T, P, S, F, I, KI = m.groups()
                I = I or "0"
                KI = KI or "0"

                if folder == "masterdata":
                    lang = re.search(r"masterdata-([a-z]{3})", fn)
                    mod = f"{folder}-{lang.group(1)}" if lang else folder
                else:
                    mod = folder

                if date_key not in all_data_by_date:
                    all_data_by_date[date_key] = []
                if not any(row[1] == mod for row in all_data_by_date[date_key]):
                    all_data_by_date[date_key].append([date_key, mod, T, P, S, F, I, KI])

    # === Only include latest 5 working days (Mon–Fri) ===
    sorted_dates = sorted(
        all_data_by_date.keys(),
        key=lambda x: datetime.strptime(x, "%d-%B-%Y"),
        reverse=True
    )
    weekdays_only = []
    for date_str in sorted_dates:
        dt = datetime.strptime(date_str, "%d-%B-%Y")
        if dt.weekday() < 5:  # Monday–Friday
            weekdays_only.append(date_str)
        if len(weekdays_only) == 5:
            break

    latest_dates = weekdays_only
    dfs = []
    for date in latest_dates:
        df = pd.DataFrame(all_data_by_date[date], columns=columns)
        dfs.append(df)

    if not dfs:
        continue

    max_len = max(len(df) for df in dfs)
    for i in range(len(dfs)):
        dfs[i] = dfs[i].reindex(range(max_len))
        if i < len(dfs) - 1:
            dfs[i][""] = ""
            dfs[i][" "] = ""

    os.makedirs("../csv", exist_ok=True)
    final_df = pd.concat(dfs, axis=1)
    final_df.to_csv(f"../csv/{csv_filename}", index=False)
