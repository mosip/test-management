#!/usr/bin/env bash
# Start the daily testrig sheet update in time for 09:30 IST.
#
# GitHub delivers the workflow's own schedule hours late (runs have been
# starting around 16:00 IST). A host whose clock is correct has to dispatch
# the workflow instead. MINIO_ENVS must already list every environment tab;
# this script does not change that list.
#
#   crontab -e
#   CRON_TZ=Asia/Kolkata
#   0 9 * * 1-5 GH_TOKEN=... /absolute/path/trigger_morning_sheet.sh
#
# GH_TOKEN needs permission to dispatch Actions on mosip/test-management.
# The token already stored as ACTION_PAT can be used.
set -euo pipefail

: "${GH_TOKEN:?Set GH_TOKEN to a token that can dispatch Actions on mosip/test-management}"

REPO="${REPO:-mosip/test-management}"
REF="${REF:-master}"

curl --fail -sS -X POST \
  -H "Authorization: Bearer ${GH_TOKEN}" \
  -H "Accept: application/vnd.github+json" \
  "https://api.github.com/repos/${REPO}/actions/workflows/update-reports.yml/dispatches" \
  -d "{\"ref\":\"${REF}\"}"

echo "Dispatched update-reports.yml on ${REPO}@${REF}"
