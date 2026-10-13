import os
import json
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

failed_aliases     = []
successful_aliases = []
failure_reasons    = {}


def log(alias, message):
    print(f"[{alias}] {message}", flush=True)


def format_date_str(dt):
    return dt.strftime("%d-%B-%Y")


def date_key_from_minio_ts(ts: str) -> str:
    ts_norm = ts.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(ts_norm)
    except ValueError:
        dt = datetime.strptime(ts_norm[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
    return format_date_str(dt.astimezone(IST))


# 15s was too short for a real bucket listing and dropped rows. No timeout at
# all lets a dead WireGuard peer hang the job until someone cancels it, which
# skips that env's sheet tab. 120s covers a slow listing and still fails a
# peer that will never answer.
LISTING_TIMEOUT_SECONDS = 120
PROBE_TIMEOUT_SECONDS = 30
last_mc_error = ""
latest_modified = {}


def run_mc_json_lines(alias, target, timeout=LISTING_TIMEOUT_SECONDS):
    """List one MinIO prefix. Returns [] on timeout or mc failure."""
    global last_mc_error
    last_mc_error = ""
    try:
        result = subprocess.run(
            ["mc", "ls", "--json", target],
            capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        last_mc_error = f"mc ls timed out after {timeout}s: {target}"
        log(alias, last_mc_error)
        return []

    entries = []
    for line in result.stdout.splitlines():
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            if line.strip():
                log(alias, f"Skipping non-JSON mc output: {line[:200]}")

    if result.returncode != 0 and not entries:
        last_mc_error = (result.stderr or result.stdout or f"mc ls failed for {target}").strip()[:500]
        log(alias, last_mc_error)
    return entries


def is_error_report(filename):
    return "error-report" in filename or "-error-" in filename


def append_row(all_data_by_date, date_key, module, row, modified=""):
    """Keep the newest object for a module on a given day.

    mc ls order is not time order. The first object used to win, so an older
    run or an error digest could overwrite the full-run numbers, including DSL.
    """
    rows = all_data_by_date.setdefault(date_key, [])
    key = (date_key, module)
    previous = latest_modified.get(key)
    if previous is None:
        latest_modified[key] = modified
        rows.append(row)
        return
    if modified < previous:
        return
    latest_modified[key] = modified
    for index, existing in enumerate(rows):
        if existing[1] == module:
            rows[index] = row
            return
    rows.append(row)


for alias in MINIO_ALIASES:
    try:
        log(alias, "Starting MinIO scan")
        latest_modified.clear()
        csv_filename   = f"{alias}.csv"
        all_data_by_date = {}
        matched_rows   = 0
        bucket_stats   = {bucket: 0 for bucket in MINIO_BUCKETS}

        run_mc_json_lines(alias, f"{alias}/", timeout=PROBE_TIMEOUT_SECONDS)
        if last_mc_error:
            reason = f"MinIO is unreachable for '{alias}'. {last_mc_error}"
            log(alias, reason)
            failure_reasons[alias] = {"reason": reason}
            failed_aliases.append(alias)
            continue

        for bucket in MINIO_BUCKETS:
            folders = []

            if bucket == "dslreports":
                entries = run_mc_json_lines(alias, f"{alias}/dslreports/full/")
                log(alias, f"dslreports/full entries scanned: {len(entries)}")

                for info in entries:
                    try:
                        fn       = info["key"]
                        if fn.startswith("ExtentReport-") or is_error_report(fn):
                            continue
                        date_key = date_key_from_minio_ts(info["lastModified"])
                    except (KeyError, ValueError):
                        continue

                    if not re.search(r"report_T-\d+_P-\d+(?:_KI-\d+)?(?:_I-\d+)?_S-\d+_F-\d+", fn):
                        continue
                    m = re.search(r"report_T-(\d+)_P-(\d+)(?:_KI-(\d+))?(?:_I-(\d+))?_S-(\d+)_F-(\d+)", fn)
                    if not m:
                        continue

                    t_val, p_val, ki_val, i_val, s_val, f_val = m.groups()
                    append_row(all_data_by_date, date_key, "dsl",
                               [date_key, "dsl", t_val, p_val, s_val, f_val,
                                i_val or "0", ki_val or "0"],
                               info.get("lastModified", ""))
                    matched_rows += 1
                    bucket_stats[bucket] += 1
                continue

            if bucket == "uitestrig":
                entries = run_mc_json_lines(alias, f"{alias}/{bucket}/")
                log(alias, f"uitestrig root entries scanned: {len(entries)}")

                for info in entries:
                    try:
                        fn       = info["key"]
                        date_key = date_key_from_minio_ts(info["lastModified"])
                    except (KeyError, ValueError):
                        continue

                    if not re.search(r"-report_T-\d+_P-\d+_S-\d+_F-\d+\.html$", fn):
                        continue
                    mod_match = re.match(r"(ADMINUI|RESIDENT)-api-", fn)
                    if not mod_match:
                        continue

                    mod_raw = mod_match.group(1).lower()
                    module  = "residentui" if mod_raw == "resident" else mod_raw
                    m = re.search(r"report_T-(\d+)_P-(\d+)_S-(\d+)_F-(\d+)", fn)
                    if not m:
                        continue
                    t_val, p_val, s_val, f_val = m.groups()
                    append_row(all_data_by_date, date_key, module,
                               [date_key, module, t_val, p_val, s_val, f_val, "0", "0"],
                               info.get("lastModified", ""))
                    matched_rows += 1
                    bucket_stats[bucket] += 1

            folder_entries = run_mc_json_lines(alias, f"{alias}/{bucket}/")
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
                    for info in run_mc_json_lines(alias, f"{alias}/{bucket}/{folder}/"):
                        try:
                            fn       = info["key"]
                            date_key = date_key_from_minio_ts(info["lastModified"])
                        except (KeyError, ValueError):
                            continue
                        if not re.search(r"PMPUI-.*-report_T-\d+_P-\d+_S-\d+_F-\d+", fn):
                            continue
                        m = re.search(r"report_T-(\d+)_P-(\d+)_S-(\d+)_F-(\d+)", fn)
                        if not m:
                            continue
                        t_val, p_val, s_val, f_val = m.groups()
                        append_row(all_data_by_date, date_key, "pmpui",
                                   [date_key, "pmpui", t_val, p_val, s_val, f_val, "0", "0"],
                                   info.get("lastModified", ""))
                        matched_rows += 1
                        bucket_stats[bucket] += 1
                    continue

                for info in run_mc_json_lines(alias, f"{alias}/{bucket}/{folder}/"):
                    try:
                        fn       = info["key"]
                        date_key = date_key_from_minio_ts(info["lastModified"])
                    except (KeyError, ValueError):
                        continue
                    if "error-report" in fn:
                        continue
                    if not re.search(r"(full-)?report_T-\d+_P-\d+_S-\d+_F-", fn):
                        continue
                    m = re.search(r"report_T-(\d+)_P-(\d+)_S-(\d+)_F-(\d+)(?:_I-(\d+))?(?:_KI-(\d+))?", fn)
                    if not m:
                        continue
                    t_val, p_val, s_val, f_val, i_val, ki_val = m.groups()

                    if folder == "masterdata":
                        lang   = re.search(r"masterdata-([a-z]{3})", fn)
                        module = f"{folder}-{lang.group(1)}" if lang else folder
                    else:
                        module = folder

                    append_row(all_data_by_date, date_key, module,
                               [date_key, module, t_val, p_val, s_val, f_val,
                                i_val or "0", ki_val or "0"],
                               info.get("lastModified", ""))
                    matched_rows += 1
                    bucket_stats[bucket] += 1

        sorted_dates  = sorted(all_data_by_date.keys(),
                               key=lambda v: datetime.strptime(v, "%d-%B-%Y"), reverse=True)
        weekdays_only = []
        for date_str in sorted_dates:
            if datetime.strptime(date_str, "%d-%B-%Y").weekday() < 5:
                weekdays_only.append(date_str)
            if len(weekdays_only) == 5:
                break

        log(alias, f"Matched rows before dedupe/write: {matched_rows}")
        log(alias, f"Bucket stats: {bucket_stats}")
        log(alias, f"Available date buckets (IST): {sorted_dates[:10]}")
        log(alias, f"Selected latest 5 working days: {weekdays_only}")

        dfs = [pd.DataFrame(all_data_by_date[d], columns=columns) for d in weekdays_only]

        if not dfs:
            reason = "No usable report data found after parsing MinIO objects and applying the latest 5 working days filter."
            log(alias, reason)
            failure_reasons[alias] = {"reason": reason,
                                      "available_dates": sorted_dates[:10],
                                      "bucket_stats": bucket_stats}
            failed_aliases.append(alias)
            continue

        max_len = max(len(df) for df in dfs)
        for i in range(len(dfs)):
            dfs[i] = dfs[i].reindex(range(max_len))
            if i < len(dfs) - 1:
                dfs[i][""]  = ""
                dfs[i][" "] = ""

        os.makedirs("../csv", exist_ok=True)
        pd.concat(dfs, axis=1).to_csv(f"../csv/{csv_filename}", index=False)
        successful_aliases.append(alias)
        log(alias, f"CSV written to ../csv/{csv_filename}")

    except Exception as exc:
        log(alias, f"Failed with exception: {exc}")
        failed_aliases.append(alias)
        failure_reasons[alias] = {"reason": str(exc)}
        continue


os.makedirs("../status", exist_ok=True)

alias_name = env_name if env_name else "all"
with open(f"../status/status_{alias_name}.json", "w", encoding="utf-8") as handle:
    json.dump({"success": successful_aliases,
               "failed":  failed_aliases,
               "details": failure_reasons}, handle, indent=2)
