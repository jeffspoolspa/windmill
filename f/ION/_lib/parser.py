# requirements:
# beautifulsoup4

"""ION Pool Care HTML report parser.

Parses .xls files (HTML tables disguised as Excel) from ION Pool Care
and outputs structured JSON with ION's original field names.

Handles two report types:
  - service_log: Service Log Details (multi-profile, variable columns)
  - recurring_tasks: Recurring Tasks Detail (flat table, fixed columns)

Usage:
    python parser.py <file_path> <report_type>

Only dependency: beautifulsoup4
"""

import hashlib
import json
import sys
from html import unescape
from pathlib import Path

from bs4 import BeautifulSoup

CORE_FIELD_COUNT = 18
CORE_FIELDS = [
    "Office", "Technician", "Customer", "Address1", "Address2",
    "City", "ST", "Postal", "Service Type", "Service Body",
    "Price", "Invoice Type", "Date", "Start", "End",
    "Est. Min", "Actual", "Comments",
]


def file_hash(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return f"sha256:{h.hexdigest()}"


def clean_text(text: str) -> str | None:
    """Decode HTML entities, normalize whitespace, return None for empty."""
    if text is None:
        return None
    decoded = unescape(text)
    decoded = decoded.replace("\r\n", "\n").replace("\r", "\n")
    stripped = decoded.strip()
    if not stripped or stripped == "\xa0":
        return None
    return stripped


def extract_cell_text(cell) -> str | None:
    """Extract text from a BeautifulSoup td element."""
    return clean_text(cell.get_text())


def is_column_header_row(row) -> bool:
    """Check if a row is a column header row (gray background cells)."""
    cells = row.find_all("td")
    if not cells:
        return False
    return "background-color:#CCCCCC" in cells[0].get("style", "")


def is_section_header_3(cells) -> bool:
    """3-cell section header: profile name + Readings + Consumables."""
    if len(cells) != 3:
        return False
    return (
        cells[1].get_text(strip=True) == "Readings"
        and cells[2].get_text(strip=True) == "Consumables"
    )


def is_section_header_2(cells) -> bool:
    """2-cell section header: profile name + Readings (no consumables sold in date range).

    ION drops the consumables cell entirely when zero consumables were used
    across all visits in a profile section. This is a full profile, not a
    sub-section — treat it the same as a 3-cell header with consumables_colspan=0.
    """
    if len(cells) != 2:
        return False
    return cells[1].get_text(strip=True) == "Readings"


def is_blank_separator(cells) -> bool:
    """Check if a row is a blank separator (1 cell with colspan, empty text)."""
    if len(cells) != 1:
        return False
    colspan = cells[0].get("colspan")
    text = cells[0].get_text(strip=True)
    return colspan is not None and (not text or text == "\xa0")


def deduplicate_headers(headers: list[str]) -> list[str]:
    """Handle duplicate column names by appending _2, _3, etc."""
    seen: dict[str, int] = {}
    result = []
    for h in headers:
        if h in seen:
            seen[h] += 1
            result.append(f"{h}_{seen[h]}")
        else:
            seen[h] = 1
            result.append(h)
    return result


def classify_readings_vs_tasks(
    data_rows: list[list[str | None]],
    readings_start: int,
    readings_end: int,
) -> set[int]:
    """Scan column values to determine which are tasks (Yes/No only).

    Returns set of absolute column indices that are tasks.
    """
    task_indices = set()
    for col_idx in range(readings_start, readings_end):
        values = []
        for row in data_rows:
            if col_idx < len(row):
                val = row[col_idx]
                if val is not None:
                    values.append(val)
        if values and all(v in ("Yes", "No") for v in values):
            task_indices.add(col_idx)
    return task_indices


def _build_row_dicts(
    data_rows: list[list[str | None]],
    headers: list[str],
    readings_colspan: int,
    profile_name: str,
) -> list[dict]:
    """Classify columns and build output dicts for one subsection batch."""
    readings_start = CORE_FIELD_COUNT
    readings_end = CORE_FIELD_COUNT + readings_colspan
    consumables_start = readings_end
    total_cols = len(headers)

    task_indices = classify_readings_vs_tasks(data_rows, readings_start, readings_end)

    reading_headers = []
    task_headers = []
    for idx in range(readings_start, readings_end):
        if idx < total_cols:
            if idx in task_indices:
                task_headers.append((idx, headers[idx]))
            else:
                reading_headers.append((idx, headers[idx]))

    consumable_headers = [
        (idx, headers[idx])
        for idx in range(consumables_start, total_cols)
        if idx < total_cols
    ]

    output = []
    for cell_values in data_rows:
        row_dict: dict = {}
        for i, field_name in enumerate(CORE_FIELDS):
            row_dict[field_name] = cell_values[i] if i < len(cell_values) else None

        row_dict["Service Profile"] = profile_name

        row_dict["_readings"] = {
            hdr: cell_values[idx]
            for idx, hdr in reading_headers
            if idx < len(cell_values) and cell_values[idx] is not None
        }
        row_dict["_tasks"] = {
            hdr: cell_values[idx]
            for idx, hdr in task_headers
            if idx < len(cell_values) and cell_values[idx] is not None
        }
        row_dict["_consumables"] = {
            hdr: cell_values[idx]
            for idx, hdr in consumable_headers
            if idx < len(cell_values) and cell_values[idx] is not None
        }
        output.append(row_dict)
    return output


def parse_service_log(soup: BeautifulSoup) -> dict:
    """Parse a Service Log Details report."""
    tables = soup.find_all("table")
    if len(tables) < 2:
        raise ValueError(
            f"Expected at least 2 <table> elements, found {len(tables)}. "
            "Is this a Service Log Details report?"
        )

    data_table = tables[1]
    all_rows = data_table.find_all("tr")

    profiles_found = []
    profile_row_counts: dict[str, int] = {}
    all_output_rows = []
    parse_errors = []

    current_profile = None
    current_headers: list[str] = []
    current_readings_colspan = 0
    pending_data: list[list[str | None]] = []

    def flush():
        nonlocal pending_data
        if not current_profile or not pending_data:
            pending_data = []
            return
        rows_out = _build_row_dicts(
            pending_data, current_headers, current_readings_colspan, current_profile
        )
        all_output_rows.extend(rows_out)
        profile_row_counts[current_profile] = (
            profile_row_counts.get(current_profile, 0) + len(rows_out)
        )
        pending_data = []

    for row in all_rows:
        cells = row.find_all("td")

        if is_blank_separator(cells):
            continue

        # Full profile section header (3 cells)
        if is_section_header_3(cells):
            flush()
            current_profile = clean_text(cells[0].get_text())
            current_readings_colspan = int(cells[1].get("colspan", 0))
            profiles_found.append(current_profile)
            pending_data = []
            continue

        # 2-cell section header (no consumables sold in date range)
        if is_section_header_2(cells):
            flush()
            current_profile = clean_text(cells[0].get_text())
            current_readings_colspan = int(cells[1].get("colspan", 0))
            # No consumables cell — zero consumables sold in this profile
            profiles_found.append(current_profile)
            pending_data = []
            continue

        # Column header row — new column layout, flush pending data first
        if is_column_header_row(row):
            flush()
            raw_headers = [extract_cell_text(c) or "" for c in cells]
            current_headers = deduplicate_headers(raw_headers)
            continue

        # Data row
        if current_profile and current_headers and len(cells) == len(current_headers):
            pending_data.append([extract_cell_text(c) for c in cells])
            continue

    flush()

    return {
        "report_type": "service_log",
        "source_system": "ion",
        "extraction_metadata": {
            "file_hash": None,
            "row_count": len(all_output_rows),
            "profiles_found": profiles_found,
            "profile_row_counts": profile_row_counts,
            "parse_errors": parse_errors,
        },
        "rows": all_output_rows,
    }


def parse_recurring_tasks(soup: BeautifulSoup) -> dict:
    """Parse a Recurring Tasks Detail report."""
    tables = soup.find_all("table")
    if not tables:
        raise ValueError("No <table> elements found. Is this a Recurring Tasks report?")

    table = tables[0]
    all_rows = table.find_all("tr")

    header_row_idx = None
    for i, row in enumerate(all_rows):
        if is_column_header_row(row):
            header_row_idx = i
            break

    if header_row_idx is None:
        raise ValueError("Could not find column header row in recurring tasks report.")

    header_cells = all_rows[header_row_idx].find_all("td")
    headers = [extract_cell_text(c) or "" for c in header_cells]

    output_rows = []
    parse_errors = []

    for row in all_rows[header_row_idx + 1:]:
        cells = row.find_all("td")
        if len(cells) != len(headers):
            continue
        cell_values = [extract_cell_text(c) for c in cells]
        output_rows.append(
            {header: val for header, val in zip(headers, cell_values)}
        )

    return {
        "report_type": "recurring_tasks",
        "source_system": "ion",
        "extraction_metadata": {
            "file_hash": None,
            "row_count": len(output_rows),
            "parse_errors": parse_errors,
        },
        "rows": output_rows,
    }


def parse(file_path: str, report_type: str) -> dict:
    """Main entry point: parse an ION report file."""
    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError(f"File not found: {file_path}")

    with open(path, "r", encoding="ascii", errors="replace") as f:
        content = f.read()

    soup = BeautifulSoup(content, "html.parser")

    if report_type == "service_log":
        result = parse_service_log(soup)
    elif report_type == "recurring_tasks":
        result = parse_recurring_tasks(soup)
    else:
        raise ValueError(
            f"Unknown report_type: {report_type!r}. "
            "Expected 'service_log' or 'recurring_tasks'."
        )

    result["extraction_metadata"]["file_hash"] = file_hash(file_path)
    return result


def main():
    if len(sys.argv) != 3:
        print(
            f"Usage: {sys.argv[0]} <file_path> <report_type>",
            file=sys.stderr,
        )
        print("  report_type: service_log | recurring_tasks", file=sys.stderr)
        sys.exit(1)

    file_path = sys.argv[1]
    report_type = sys.argv[2]

    try:
        result = parse(file_path, report_type)
        json.dump(result, sys.stdout, indent=2, ensure_ascii=False)
        print()
        sys.exit(0)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
