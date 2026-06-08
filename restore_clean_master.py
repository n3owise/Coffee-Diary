from __future__ import annotations

import csv
from pathlib import Path

from coffee_diary_automation import (
    CHANGE_LOG_COLUMNS,
    CHANGE_LOG_SHEET,
    ERROR_COLUMNS,
    ERRORS_SHEET,
    GOOGLE_WRITE_CHUNK_SIZE,
    MASTER_COLUMNS,
    MASTER_SHEET,
    RUN_LOG_COLUMNS,
    RUN_LOG_SHEET,
    SHEET_ID_DEFAULT,
    build_sheets_service,
    column_letter,
    execute_google_request,
    now_ist,
    sheet_range,
    timestamp,
)


BASELINE_PATH = Path("data/master_sheet_baseline.csv")


def update_rows(service, spreadsheet_id: str, sheet_name: str, rows: list[list[str]]) -> None:
    width = max(len(row) for row in rows)
    end_col = column_letter(width)
    for start in range(0, len(rows), GOOGLE_WRITE_CHUNK_SIZE):
        chunk = rows[start : start + GOOGLE_WRITE_CHUNK_SIZE]
        start_row = start + 1
        end_row = start + len(chunk)
        request = service.spreadsheets().values().update(
            spreadsheetId=spreadsheet_id,
            range=sheet_range(sheet_name, f"A{start_row}:{end_col}{end_row}"),
            valueInputOption="RAW",
            body={"values": chunk},
        )
        execute_google_request(request, f"restore {sheet_name}")


def clear_range(service, spreadsheet_id: str, sheet_name: str, a1: str) -> None:
    request = service.spreadsheets().values().clear(
        spreadsheetId=spreadsheet_id,
        range=sheet_range(sheet_name, a1),
        body={},
    )
    execute_google_request(request, f"clear {sheet_name}")


def main() -> int:
    if not BASELINE_PATH.exists():
        raise FileNotFoundError(BASELINE_PATH)

    with BASELINE_PATH.open(newline="", encoding="utf-8") as handle:
        baseline = list(csv.reader(handle))

    if not baseline or baseline[0] != MASTER_COLUMNS:
        raise RuntimeError("Baseline Master Sheet header does not match expected columns")

    spreadsheet_id = SHEET_ID_DEFAULT
    service = build_sheets_service()

    clear_range(service, spreadsheet_id, MASTER_SHEET, "A:J")
    update_rows(service, spreadsheet_id, MASTER_SHEET, baseline)

    clear_range(service, spreadsheet_id, CHANGE_LOG_SHEET, "A:J")
    update_rows(service, spreadsheet_id, CHANGE_LOG_SHEET, [CHANGE_LOG_COLUMNS])

    clear_range(service, spreadsheet_id, ERRORS_SHEET, "A:H")
    update_rows(service, spreadsheet_id, ERRORS_SHEET, [ERROR_COLUMNS])

    clear_range(service, spreadsheet_id, RUN_LOG_SHEET, "A:L")
    run = "manual-restore-" + now_ist().strftime("%Y%m%d-%H%M%S")
    update_rows(
        service,
        spreadsheet_id,
        RUN_LOG_SHEET,
        [
            RUN_LOG_COLUMNS,
            [
                timestamp(),
                run,
                timestamp(),
                timestamp(),
                "Restored",
                "0",
                "0",
                "0",
                "0",
                "0",
                "FALSE",
                "Restored clean Master Sheet baseline and cleared bad automation logs.",
            ],
        ],
    )

    print(f"Restored {len(baseline) - 1} Master Sheet rows from clean baseline")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
