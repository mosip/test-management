import json
import os
import re
import subprocess
from datetime import datetime, timedelta, timezone

import pandas as pd

try:
    from zoneinfo import ZoneInfo

    IST = ZoneInfo("Asia/Kolkata")
except Exception:
    IST = timezone(timedelta(hours=5, minutes=30))


env_name = os.getenv("MINIO_ENV") or os.getenv("MINIO_ALIAS")
env_list = os.getenv("MINIO_ENVS")

if env_name:
    MINIO_ALIASES = [env_name]
elif env_list:
    MINIO_ALIASES = json.loads(env_list)
else:
    MINIO_ALIASES = []


MINIO_BUCKETS = ["apitestrig", "automation", "dslreports", "uitestrig"]
columns = ["Date", "Module", "T", "P", "S", "F", "I", "KI"]

failed_aliases = []
successful_aliases = []
failure_reasons = {}


def log(alias, message):
    print(f"[{alias}] {message}")


def format_date_str(dt):
    return dt.strftime("%d-%B-%Y")


def date_key_from_minio_ts(ts: str) -> str:
    ts_norm = ts.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(ts_norm)
    except ValueError:
        dt = datetime.strptime(ts_norm[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
    dt_ist = dt.astimezone(IST)
    return format_date_str(dt_ist)


def run_mc_json_lines(alias, command):
    output = subprocess.getoutput(command)
    lines = output.splitlines()
    entries = []

    for line in lines:
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            if line.strip():
                log(alias, f"Skipping non-JSON mc output: {line[:200]}")

    return entries


def append_row(all_data_by_date, date_key, module, row):
    all_data_by_date.setdefault(date_key, [])
    if not any(existing[1] == module for existing in all_data_by_date[date_key]):
        all_data_by_date[date_key].append(row)


for alias in MINIO_ALIASES:
    try:
        log(alias, "Starting MinIO scan")
        csv_filename = f"{alias}.csv"
        all_data_by_date = {}
        matched_rows = 0
        bucket_stats = {bucket: 0 for bucket in MINIO_BUCKETS}

        for bucket in MINIO_BUCKETS:
            folders = []

            if bucket == "dslreports":
                entries = run_mc_json_lines(alias, f"mc ls --json {alias}/dslreports/full/")
                log(alias, f"dslreports/full entries scanned: {len(entries)}")

                for info in entries:
                    try:
                        fn = info["key"]
                        if fn.startswith("ExtentReport-"):
                            continue
                        ts = info["lastModified"]
                        date_key = date_key_from_minio_ts(ts)
                    except (KeyError, ValueError):
                        continue

                    if not re.search(r"report_T-\d+_P-\d+(?:_KI-\d+)?(?:_I-\d+)?_S-\d+_F-\d+", fn):
                        continue

                    match = re.search(r"report_T-(\d+)_P-(\d+)(?:_KI-(\d+))?(?:_I-(\d+))?_S-(\d+)_F-(\d+)", fn)
                    if not match:
                        continue

                    t_val, p_val, ki_val, i_val, s_val, f_val = match.groups()
                    i_val = i_val or "0"
                    ki_val = ki_val or "0"

                    append_row(
                        all_data_by_date,
                        date_key,
                        "dsl",
                        [date_key, "dsl", t_val, p_val, s_val, f_val, i_val, ki_val],
                    )
                    matched_rows += 1
                    bucket_stats[bucket] += 1

                continue

            if bucket == "uitestrig":
                root_entries = run_mc_json_lines(alias, f"mc ls --json {alias}/{bucket}/")
                log(alias, f"uitestrig root entries scanned: {len(root_entries)}")

                for info in root_entries:
                    try:
                        fn = info["key"]
                        ts = info["lastModified"]
                        date_key = date_key_from_minio_ts(ts)
                    except (KeyError, ValueError):
                        continue

                    if not re.search(r"-report_T-\d+_P-\d+_S-\d+_F-\d+\.html$", fn):
                        continue

                    mod_match = re.match(r"(ADMINUI|RESIDENT)-api-", fn)
                    if not mod_match:
                        continue

                    mod_raw = mod_match.group(1).lower()
                    module = "residentui" if mod_raw == "resident" else mod_raw

                    match = re.search(r"report_T-(\d+)_P-(\d+)_S-(\d+)_F-(\d+)", fn)
                    if not match:
                        continue

                    t_val, p_val, s_val, f_val = match.groups()
                    append_row(
                        all_data_by_date,
                        date_key,
                        module,
                        [date_key, module, t_val, p_val, s_val, f_val, "0", "0"],
                    )
                    matched_rows += 1
                    bucket_stats[bucket] += 1

            folder_entries = run_mc_json_lines(alias, f"mc ls --json {alias}/{bucket}/")
            for info in folder_entries:
                try:
                    folders.append(info["key"].strip("/"))
                except KeyError:
                    continue

            log(alias, f"{bucket} folders discovered: {len(folders)}")
            if not folders:
                continue

            for folder in folders:
                if bucket == "uitestrig" and folder.lower() == "pmpui":
                    folder_entries = run_mc_json_lines(alias, f"mc ls --json {alias}/{bucket}/{folder}/")
                    for info in folder_entries:
                        try:
                            fn = info["key"]
                            ts = info["lastModified"]
                            date_key = date_key_from_minio_ts(ts)
                        except (KeyError, ValueError):
                            continue

                        if not re.search(r"PMPUI-.*-report_T-\d+_P-\d+_S-\d+_F-\d+", fn):
                            continue

                        match = re.search(r"report_T-(\d+)_P-(\d+)_S-(\d+)_F-(\d+)", fn)
                        if not match:
                            continue

                        t_val, p_val, s_val, f_val = match.groups()
                        append_row(
                            all_data_by_date,
                            date_key,
                            "pmpui",
                            [date_key, "pmpui", t_val, p_val, s_val, f_val, "0", "0"],
                        )
                        matched_rows += 1
                        bucket_stats[bucket] += 1
                    continue

                folder_entries = run_mc_json_lines(alias, f"mc ls --json {alias}/{bucket}/{folder}/")
                for info in folder_entries:
                    try:
                        fn = info["key"]
                        ts = info["lastModified"]
                        date_key = date_key_from_minio_ts(ts)
                    except (KeyError, ValueError):
                        continue

                    if "error-report" in fn:
                        continue
                    if not re.search(r"(full-)?report_T-\d+_P-\d+_S-\d+_F-", fn):
                        continue

                    match = re.search(r"report_T-(\d+)_P-(\d+)_S-(\d+)_F-(\d+)(?:_I-(\d+))?(?:_KI-(\d+))?", fn)
                    if not match:
                        continue

                    t_val, p_val, s_val, f_val, i_val, ki_val = match.groups()
                    i_val = i_val or "0"
                    ki_val = ki_val or "0"

                    if folder == "masterdata":
                        lang = re.search(r"masterdata-([a-z]{3})", fn)
                        module = f"{folder}-{lang.group(1)}" if lang else folder
                    else:
                        module = folder

                    append_row(
                        all_data_by_date,
                        date_key,
                        module,
                        [date_key, module, t_val, p_val, s_val, f_val, i_val, ki_val],
                    )
                    matched_rows += 1
                    bucket_stats[bucket] += 1

        sorted_dates = sorted(
            all_data_by_date.keys(),
            key=lambda value: datetime.strptime(value, "%d-%B-%Y"),
            reverse=True,
        )
        weekdays_only = []
        for date_str in sorted_dates:
            dt = datetime.strptime(date_str, "%d-%B-%Y")
            if dt.weekday() < 5:
                weekdays_only.append(date_str)
            if len(weekdays_only) == 5:
                break

        log(alias, f"Matched rows before dedupe/write: {matched_rows}")
        log(alias, f"Bucket stats: {bucket_stats}")
        log(alias, f"Available date buckets (IST): {sorted_dates[:10]}")
        log(alias, f"Selected latest 5 working days: {weekdays_only}")

        dfs = []
        for date_key in weekdays_only:
            dfs.append(pd.DataFrame(all_data_by_date[date_key], columns=columns))

        if not dfs:
            reason = (
                "No usable report data found after parsing MinIO objects and applying the latest 5 working days filter."
            )
            log(alias, reason)
            failure_reasons[alias] = {
                "reason": reason,
                "available_dates": sorted_dates[:10],
                "bucket_stats": bucket_stats,
            }
            failed_aliases.append(alias)
            continue

        max_len = max(len(df) for df in dfs)
        for index in range(len(dfs)):
            dfs[index] = dfs[index].reindex(range(max_len))
            if index < len(dfs) - 1:
                dfs[index][""] = ""
                dfs[index][" "] = ""

        os.makedirs("../csv", exist_ok=True)
        final_df = pd.concat(dfs, axis=1)
        final_df.to_csv(f"../csv/{csv_filename}", index=False)
        successful_aliases.append(alias)
        log(alias, f"CSV written to ../csv/{csv_filename}")

    except Exception as exc:
        log(alias, f"Failed with exception: {exc}")
        failed_aliases.append(alias)
        failure_reasons[alias] = {"reason": str(exc)}
        continue


os.makedirs("../status", exist_ok=True)

status = {
    "success": successful_aliases,
    "failed": failed_aliases,
    "details": failure_reasons,
}

alias_name = env_name if env_name else "all"
with open(f"../status/status_{alias_name}.json", "w", encoding="utf-8") as handle:
    json.dump(status, handle, indent=2)