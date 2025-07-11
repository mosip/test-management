import os
import pandas as pd
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from openpyxl import Workbook
from openpyxl.drawing.image import Image
from openpyxl.utils.dataframe import dataframe_to_rows
from openpyxl.utils import get_column_letter

def load_and_normalize_data(input_file):
    rows = []
    with open(input_file, 'r') as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("###"):
                continue
            parts = [p.strip() for p in line.split(',') if p.strip()]
            for i in range(0, len(parts), 8):
                if i + 7 < len(parts):
                    row = parts[i:i + 8]
                    rows.append(row)

    df = pd.DataFrame(rows, columns=["Date", "Module", "T", "P", "S", "F", "I", "KI"])
    df = df[df["Date"].str.contains(r'\d{1,2}-[A-Za-z]+-\d{4}', na=False)]
    df["Date"] = pd.to_datetime(df["Date"], format="%d-%B-%Y")

    for col in ["T", "P", "S", "F", "I", "KI"]:
        df[col] = pd.to_numeric(df[col], errors='coerce')

    return df

def generate_graphs(df, output_dir):
    modules = df["Module"].unique()
    graph_files = []

    for module in modules:
        module_df = df[df["Module"] == module]
        module_df = module_df.groupby(["Date", "Module"]).sum().reset_index()
        module_df = module_df.sort_values("Date")

        if module_df.empty:
            continue

        plt.figure(figsize=(10, 5))
        plt.plot(module_df["Date"], module_df["T"], label="Total", linewidth=2, color='blue', marker='o')
        plt.plot(module_df["Date"], module_df["P"], label="Passed", linewidth=2.5, color='green', marker='o')
        plt.plot(module_df["Date"], module_df["F"], label="Failed", linewidth=2.5, color='red', marker='o')

        plt.title(f"Trend for Module: {module}", fontsize=14)
        plt.xlabel("Date", fontsize=12)
        plt.ylabel("Count", fontsize=12)
        plt.grid(True, linestyle='--', alpha=0.5)
        plt.legend()

        plt.gca().xaxis.set_major_formatter(mdates.DateFormatter('%d-%b-%Y'))
        plt.gca().xaxis.set_major_locator(mdates.DayLocator())

        plt.tight_layout()

        file_path = os.path.join(output_dir, f"{module}_trend.png")
        plt.savefig(file_path)
        graph_files.append((module, file_path))
        plt.close()

    return graph_files

def export_to_excel(df, graph_files, xlsx_path):
    wb = Workbook()

    ws_data = wb.active
    ws_data.title = "Module Data"

    for row in dataframe_to_rows(df, index=False, header=True):
        ws_data.append(row)

    # Format date cells in column A and auto-adjust column widths
    for cell in ws_data["A"][1:]:  # Skip header
        cell.number_format = "DD-MMM-YYYY"

    for column_cells in ws_data.columns:
        length = max(len(str(cell.value)) if cell.value is not None else 0 for cell in column_cells)
        col_letter = get_column_letter(column_cells[0].column)
        ws_data.column_dimensions[col_letter].width = length + 2

    ws_charts = wb.create_sheet(title="Module Graphs")
    row_pos = 1
    for module, image_path in graph_files:
        if not os.path.exists(image_path):
            continue
        img = Image(image_path)
        img.width = 800
        img.height = 400
        ws_charts.add_image(img, f"A{row_pos}")
        row_pos += 22

    wb.save(xlsx_path)

# === MAIN EXECUTION ===

csv_dir = "minio-report-tracker/csv"
output_base = "minio-report-tracker/xlxs"
os.makedirs(output_base, exist_ok=True)

for file in os.listdir(csv_dir):
    if not file.endswith(".csv"):
        continue

    alias = file.replace(".csv", "")
    csv_path = os.path.join(csv_dir, file)
    df = load_and_normalize_data(csv_path)

    output_dir = os.path.join(output_base, f"{alias}_images")
    os.makedirs(output_dir, exist_ok=True)

    graph_files = generate_graphs(df, output_dir)
    xlsx_path = os.path.join(output_base, f"{alias}.xlsx")
    export_to_excel(df, graph_files, xlsx_path)
