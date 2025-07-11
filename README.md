# mosip-labs - MinIO Report Tracker

This repository automates the extraction, transformation, and visualization of report metadata from MinIO buckets. It generates `.csv` and `.xlsx` reports with charts and commits them to the repository daily.

## What It Does

1. Fetches metadata from MinIO buckets using aliases such as `cellbox21`, `dev-int`, `qa-core`, etc.  
   Note: These aliases represent different environments.

2. Parses filenames to extract key values:
   - T (Total), P (Passed), S (Skipped), F (Failed), I (Ignored), KI (Known Issues)

3. Generates:
   - A `.csv` file containing the last 5 days of data per module.
   - A `.xlsx` report with two sheets:
     - Module Data: Tabular data
     - Module Graphs: Line charts for Total, Passed, and Failed

4. Pushes updated reports to the `reporting` branch.

## GitHub Actions Workflow Overview

A scheduled GitHub Actions workflow:

- Runs every weekday at 9:45 AM IST
- Connects securely to MinIO over WireGuard
- Dynamically configures aliases using repository secrets
- Generates `.csv` and `.xlsx` reports
- Pushes updates to the `reporting` branch

## Required GitHub Secrets

Set the following secrets in your repository:

| Secret Name                    | Description                         |
|-------------------------------|-------------------------------------|
| WIREGUARD_CONF                 | Complete `wg0.conf` contents        |
| MINIO_<ENV>_NAME              | Alias name for environment          |
| MINIO_<ENV>_URL               | MinIO URL (e.g., https://host:9000) |
| MINIO_<ENV>_USER              | MinIO root user                     |
| MINIO_<ENV>_PASSWORD          | MinIO root password                 |

Example:
- MINIO_CELLBOX21_NAME = cellbox21  
- MINIO_CELLBOX21_URL = https://minio.cellbox21.com:9000  
- MINIO_CELLBOX21_USER = minioadmin  
- MINIO_CELLBOX21_PASSWORD = password  

Add secrets for each environment you want to track.

## Python Scripts

### scripts/update_csv.py

- Connects to defined MinIO aliases  
- Uses regex to parse filenames  
- Extracts metadata for the latest 5 days  
- Saves `.csv` in `csv/` folder  

### scripts/generate_xlsx.py

- Reads all `.csv` from `csv/`  
- Creates `.xlsx` with:  
  - Sheet 1: Table  
  - Sheet 2: Line chart for T, P, F  
- Saves `.xlsx` in `xlxs/`  

## Maintenance Instructions

### Add New Environment

1. Add relevant GitHub secrets:
   - MINIO_<ENV>_NAME  
   - MINIO_<ENV>_URL  
   - MINIO_<ENV>_USER  
   - MINIO_<ENV>_PASSWORD  

2. Update `MINIO_ALIASES` in `scripts/update_csv.py`:

```python
MINIO_ALIASES = [
    "cellbox21", "collab", "dev-int", "dev3",
    "released", "dev1", "qa-base", "qa-core", "qa-country"
]
```

### Remove Environment

1. Remove corresponding GitHub secrets  
2. Remove alias name from `MINIO_ALIASES` in `update_csv.py`
