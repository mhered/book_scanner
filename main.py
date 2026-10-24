import csv
import html
import io
import logging
import os
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, PlainTextResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from book_storage import (
    BatchResult,
    BookRecord,
    PendingBatch,
    PendingScan,
    apply_batch,
    create_location,
    load_books,
    load_locations,
    location_path,
    save_books,
)


app = FastAPI(title="Book Scanner")
logger = logging.getLogger(__name__)
APP_PATH = Path(__file__).parent
ISBN_LIST_PATH = APP_PATH / "isbn_list.json"
LOCATIONS_PATH = APP_PATH / "locations.json"
PENDING_BATCH: PendingBatch | None = None
CSV_COLUMNS = {"data", "timestamp"}
TIMESTAMP_FORMATS = (
    "%m/%d/%Y %H:%M:%S",
    "%m/%d/%Y %H:%M",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S",
)


def _page(title: str, body: str, status_code: int = 200) -> HTMLResponse:
    return HTMLResponse(
        f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(title)}</title>
  <style>
    body {{ font-family: system-ui, sans-serif; margin: 2rem auto; max-width: 48rem; padding: 0 1rem; }}
    section {{ margin-top: 2rem; }}
    table {{ border-collapse: collapse; width: 100%; }}
    th, td {{ border: 1px solid #ccc; padding: 0.5rem; text-align: left; }}
    th {{ background: #f4f4f4; }}
    label {{ display: block; font-weight: 600; margin-top: 1rem; }}
    input, select, button {{ font: inherit; margin-top: 0.35rem; padding: 0.4rem; }}
    select, input {{ max-width: 100%; width: 100%; box-sizing: border-box; }}
    button {{ cursor: pointer; }}
    .problem {{ background: #fff3f3; border: 1px solid #d33; border-radius: 0.25rem; padding: 1rem; }}
    .notice {{ background: #fffbe6; border: 1px solid #dbb800; border-radius: 0.25rem; padding: 1rem; }}
  </style>
</head>
<body>
  {body}
</body>
</html>""",
        status_code=status_code,
    )


def _error_page(status_code: int, message: str) -> HTMLResponse:
    return _page(
        "Book Scanner Upload Problem",
        f"""<h1>Upload could not be processed</h1>
<div class="problem"><p>{html.escape(message)}</p></div>""",
        status_code,
    )


@app.exception_handler(StarletteHTTPException)
async def http_exception_page(
    request: Request,
    exc: StarletteHTTPException,
) -> HTMLResponse:
    return _error_page(exc.status_code, str(exc.detail))


@app.exception_handler(RequestValidationError)
async def validation_exception_page(
    request: Request,
    exc: RequestValidationError,
) -> HTMLResponse:
    return _error_page(422, "The request is incomplete or malformed.")


@app.exception_handler(Exception)
async def unexpected_exception_page(request: Request, exc: Exception) -> HTMLResponse:
    logger.exception("Unexpected error while processing a book scanner request")
    return _error_page(
        500,
        "An unexpected server error occurred. Please try again or check the service logs.",
    )


def _parse_timestamp(value: str) -> datetime:
    for timestamp_format in TIMESTAMP_FORMATS:
        try:
            return datetime.strptime(value.strip(), timestamp_format)
        except ValueError:
            pass
    raise ValueError("use MM/DD/YYYY HH:MM:SS")


def _parse_csv(content: bytes) -> PendingBatch:
    try:
        text = content.decode("utf-8-sig")
        reader = csv.DictReader(io.StringIO(text))
    except (UnicodeDecodeError, csv.Error) as exc:
        raise HTTPException(status_code=400, detail="Invalid UTF-8 CSV") from exc

    if not reader.fieldnames:
        raise HTTPException(status_code=400, detail="CSV is empty or has no header")
    names = {name.strip().lower(): name for name in reader.fieldnames if name}
    if not CSV_COLUMNS.issubset(names):
        raise HTTPException(
            status_code=400,
            detail="Expected CSV columns named Data and Timestamp",
        )

    scans: dict[str, PendingScan] = {}
    skipped_rows: list[int] = []
    row_count = 0
    try:
        for row_number, row in enumerate(reader, start=2):
            isbn = "".join(
                character
                for character in (row.get(names["data"]) or "").strip()
                if character not in " -\t\r\n"
            )
            timestamp_value = (row.get(names["timestamp"]) or "").strip()
            if not isbn and not timestamp_value:
                continue
            row_count += 1
            if not isbn:
                skipped_rows.append(row_number)
                continue
            if not timestamp_value:
                scan = PendingScan(None, "missing")
            else:
                try:
                    scan = PendingScan(_parse_timestamp(timestamp_value), None)
                except ValueError:
                    scan = PendingScan(None, "invalid")
            existing = scans.get(isbn)
            if existing is None or (
                scan.scanned_at is not None
                and (
                    existing.scanned_at is None
                    or scan.scanned_at > existing.scanned_at
                )
            ):
                scans[isbn] = scan
    except csv.Error as exc:
        raise HTTPException(status_code=400, detail="Malformed CSV data") from exc

    if not scans:
        raise HTTPException(status_code=400, detail="CSV contains no ISBN rows")
    return PendingBatch(row_count, scans, skipped_rows)


def _load_locations_or_error() -> dict:
    try:
        return load_locations(LOCATIONS_PATH)
    except ValueError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


def _timestamp_label(timestamp: datetime | None) -> str:
    return timestamp.isoformat(timespec="seconds") if timestamp else "Unknown"


def _location_options(locations: dict, selected_id: str | None = None) -> str:
    options: list[tuple[str, str, int]] = []
    for location in locations.values():
        depth = 0
        current = location
        while current.parent_id is not None:
            depth += 1
            current = locations[current.parent_id]
        options.append((location.id, location_path(location.id, locations), depth))
    return "".join(
        "<option value=\"{}\"{}>{}{}</option>".format(
            html.escape(location_id, quote=True),
            " selected" if location_id == selected_id else "",
            "&nbsp;&nbsp;" * depth,
            html.escape(path),
        )
        for location_id, path, depth in sorted(options, key=lambda item: item[1])
    )


def _assignment_url(request: Request) -> str:
    public_url = os.environ.get("BOOK_SCANNER_PUBLIC_URL", "").rstrip("/")
    if public_url and "<" not in public_url:
        return f"{public_url}/assign-location"
    return str(request.url_for("view_scan_batch"))


def _batch_notices(batch: PendingBatch, saved: bool = False) -> str:
    notices = ""
    timestamp_issues = sum(
        scan.timestamp_issue is not None for scan in batch.scans.values()
    )
    if timestamp_issues:
        description = (
            "were saved with an Unknown timestamp."
            if saved
            else (
                "have a missing or invalid timestamp. They will be saved with an "
                "Unknown timestamp and can be replaced by any later valid scan."
            )
        )
        notices += (
            f"<div class=\"notice\"><p>{timestamp_issues} ISBN(s) {description}</p></div>"
        )
    if batch.skipped_rows:
        rows = ", ".join(str(row) for row in batch.skipped_rows)
        notices += (
            "<div class=\"notice\"><p>Rows without an ISBN were skipped: "
            f"{html.escape(rows)}.</p></div>"
        )
    return notices


def _result_table(
    title: str,
    rows: list[tuple[str, BookRecord]]
    | list[tuple[str, BookRecord, BookRecord]],
    locations: dict,
) -> str:
    if not rows:
        return f"<section><h2>{html.escape(title)}</h2><p>None.</p></section>"
    has_previous = len(rows[0]) == 3
    header = "<th>ISBN</th><th>Scan timestamp</th><th>Location</th>"
    if has_previous:
        header += "<th>Previous record</th>"
    body = ""
    for row in rows:
        isbn, record = row[0], row[1]
        cells = (
            f"<td>{html.escape(isbn)}</td>"
            f"<td>{html.escape(_timestamp_label(record.scanned_at))}</td>"
            f"<td>{html.escape(location_path(record.location_id, locations))}</td>"
        )
        if has_previous:
            previous = row[2]
            cells += (
                "<td>{}; {}</td>".format(
                    html.escape(_timestamp_label(previous.scanned_at)),
                    html.escape(location_path(previous.location_id, locations)),
                )
            )
        body += f"<tr>{cells}</tr>"
    return (
        f"<section><h2>{html.escape(title)} ({len(rows)})</h2>"
        f"<table><thead><tr>{header}</tr></thead><tbody>{body}</tbody></table></section>"
    )


@app.post("/upload", response_class=PlainTextResponse)
async def upload_csv(request: Request) -> str:
    """Stage a QR2CSV export and return the location-assignment page URL."""
    global PENDING_BATCH
    content_type = request.headers.get("content-type", "").split(";", 1)[0].lower()
    if content_type != "text/csv":
        raise HTTPException(status_code=415, detail="Use Content-Type: text/csv")
    content = await request.body()
    if not content:
        raise HTTPException(status_code=400, detail="CSV body is empty")
    PENDING_BATCH = _parse_csv(content)
    return _assignment_url(request)


@app.get("/assign-location", response_class=HTMLResponse)
async def view_scan_batch() -> HTMLResponse:
    if PENDING_BATCH is None:
        raise HTTPException(status_code=404, detail="No uploaded scan batch is waiting")
    batch = PENDING_BATCH
    locations = _load_locations_or_error()
    return _page(
        "Assign book location",
        f"""<h1>Assign a location</h1>
<p>{batch.row_count} CSV rows read; {len(batch.scans)} unique ISBNs are ready to save.</p>
{_batch_notices(batch)}
<form method="post" action="/assign-location">
  <label for="location_id">Storage location</label>
  <select id="location_id" name="location_id" required>
    {_location_options(locations)}
  </select>
  <details>
    <summary>Add New Location</summary>
    <label for="new_location_name">New location name</label>
    <input id="new_location_name" name="new_location_name">
    <label for="new_location_parent_id">Parent location</label>
    <select id="new_location_parent_id" name="new_location_parent_id">
      {_location_options(locations)}
    </select>
    <p>If a new name is entered, it is created under this parent and used for this batch.</p>
  </details>
  <button type="submit">Assign location and save</button>
</form>""",
    )


@app.post("/assign-location", response_class=HTMLResponse)
async def commit_scan_batch(
    location_id: str = Form(...),
    new_location_name: str = Form(""),
    new_location_parent_id: str = Form(""),
) -> HTMLResponse:
    global PENDING_BATCH
    if PENDING_BATCH is None:
        raise HTTPException(status_code=404, detail="No uploaded scan batch is waiting")
    batch = PENDING_BATCH
    locations = _load_locations_or_error()
    try:
        books = load_books(ISBN_LIST_PATH, locations)
        if new_location_name.strip():
            location = create_location(
                LOCATIONS_PATH,
                locations,
                new_location_name,
                new_location_parent_id,
            )
            location_id = location.id
        if location_id not in locations:
            raise ValueError("Select a valid storage location")
        result = apply_batch(books, batch.scans, location_id)
        save_books(ISBN_LIST_PATH, books)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    PENDING_BATCH = None

    return _page(
        "ISBN list updated",
        f"""<h1>ISBN list updated</h1>
<p>{batch.row_count} CSV rows read; {len(batch.scans)} unique ISBNs processed.</p>
{_batch_notices(batch, saved=True)}
{_result_table("New ISBNs added", result.added, locations)}
{_result_table("ISBNs updated", result.updated, locations)}
{_result_table("Already up to date", result.unchanged, locations)}""",
    )
