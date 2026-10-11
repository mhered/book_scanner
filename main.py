import csv
import html
import io
import json
import logging
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

app = FastAPI(title="Book Scanner")
logger = logging.getLogger(__name__)
ISBN_LIST_PATH = Path(__file__).with_name("isbn_list.json")
CSV_COLUMNS = {"data", "timestamp"}
TIMESTAMP_FORMATS = (
    "%d/%m/%Y %H:%M:%S",
    "%d/%m/%Y %H:%M",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S",
)


def _error_page(status_code: int, message: str) -> HTMLResponse:
    return HTMLResponse(
        status_code=status_code,
        content=f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Book Scanner Upload Problem</title>
  <style>
    body {{ font-family: system-ui, sans-serif; margin: 2rem auto; max-width: 48rem; padding: 0 1rem; }}
    .problem {{ background: #fff3f3; border: 1px solid #d33; border-radius: 0.25rem; padding: 1rem; }}
  </style>
</head>
<body>
  <h1>Upload could not be processed</h1>
  <div class="problem">
    <p>{html.escape(message)}</p>
  </div>
</body>
</html>""",
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
    return _error_page(422, "The upload request is incomplete or malformed.")


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
    raise ValueError("use DD/MM/YYYY HH:MM:SS")


def _load_isbn_list() -> dict[str, datetime]:
    if not ISBN_LIST_PATH.exists():
        return {}
    try:
        data = json.loads(ISBN_LIST_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HTTPException(
            status_code=500,
            detail="Could not read the local ISBN list",
        ) from exc
    if not isinstance(data, dict):
        raise HTTPException(status_code=500, detail="The local ISBN list is invalid")

    isbn_list: dict[str, datetime] = {}
    for isbn, timestamp in data.items():
        if not isinstance(isbn, str) or not isinstance(timestamp, str):
            raise HTTPException(status_code=500, detail="The local ISBN list is invalid")
        try:
            isbn_list[isbn] = _parse_timestamp(timestamp)
        except ValueError as exc:
            raise HTTPException(
                status_code=500,
                detail="The local ISBN list contains an invalid timestamp",
            ) from exc
    return isbn_list


def _save_isbn_list(isbn_list: dict[str, datetime]) -> None:
    contents = {
        isbn: timestamp.isoformat(timespec="seconds")
        for isbn, timestamp in sorted(isbn_list.items())
    }
    temporary_path = ISBN_LIST_PATH.with_suffix(".tmp")
    try:
        temporary_path.write_text(
            json.dumps(contents, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        temporary_path.replace(ISBN_LIST_PATH)
    except OSError as exc:
        raise HTTPException(
            status_code=500,
            detail="Could not save the local ISBN list",
        ) from exc


def _parse_csv(content: bytes) -> tuple[int, dict[str, datetime]]:
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

    scans: dict[str, datetime] = {}
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
            if not isbn:
                raise HTTPException(status_code=400, detail=f"Row {row_number}: ISBN is empty")
            try:
                scanned_at = _parse_timestamp(timestamp_value)
            except ValueError as exc:
                raise HTTPException(
                    status_code=400,
                    detail=f"Row {row_number}: invalid timestamp; {exc}",
                ) from exc
            row_count += 1
            if scanned_at > scans.get(isbn, datetime.min):
                scans[isbn] = scanned_at
    except csv.Error as exc:
        raise HTTPException(status_code=400, detail="Malformed CSV data") from exc

    if not scans:
        raise HTTPException(status_code=400, detail="CSV contains no scan rows")
    return row_count, scans


def _table(title: str, rows: list[tuple[str, datetime, datetime | None]]) -> str:
    if not rows:
        return f"<section><h2>{title}</h2><p>None.</p></section>"
    header = "<th>ISBN</th><th>Uploaded timestamp</th>"
    if any(previous for _, _, previous in rows):
        header += "<th>Previous timestamp</th>"
    body = "".join(
        "<tr><td>{}</td><td>{}</td>{}</tr>".format(
            html.escape(isbn),
            html.escape(timestamp.isoformat(timespec="seconds")),
            (
                f"<td>{html.escape(previous.isoformat(timespec='seconds'))}</td>"
                if previous
                else ""
            ),
        )
        for isbn, timestamp, previous in rows
    )
    return (
        f"<section><h2>{title} ({len(rows)})</h2>"
        f"<table><thead><tr>{header}</tr></thead><tbody>{body}</tbody></table></section>"
    )


@app.post("/upload")
async def upload_csv(request: Request) -> HTMLResponse:
    """Store the newest scan timestamp for each ISBN in a QR2CSV export."""
    content_type = request.headers.get("content-type", "").split(";", 1)[0].lower()
    if content_type != "text/csv":
        raise HTTPException(status_code=415, detail="Use Content-Type: text/csv")

    content = await request.body()
    if not content:
        raise HTTPException(status_code=400, detail="CSV body is empty")

    row_count, scans = _parse_csv(content)
    isbn_list = _load_isbn_list()
    added: list[tuple[str, datetime, datetime | None]] = []
    updated: list[tuple[str, datetime, datetime | None]] = []
    up_to_date: list[tuple[str, datetime, datetime | None]] = []

    for isbn, scanned_at in scans.items():
        previous = isbn_list.get(isbn)
        if previous is None:
            isbn_list[isbn] = scanned_at
            added.append((isbn, scanned_at, None))
        elif scanned_at > previous:
            isbn_list[isbn] = scanned_at
            updated.append((isbn, scanned_at, previous))
        else:
            up_to_date.append((isbn, scanned_at, previous))

    _save_isbn_list(isbn_list)
    return HTMLResponse(
        f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Book Scanner Upload</title>
  <style>
    body {{ font-family: system-ui, sans-serif; margin: 2rem auto; max-width: 48rem; padding: 0 1rem; }}
    section {{ margin-top: 2rem; }}
    table {{ border-collapse: collapse; width: 100%; }}
    th, td {{ border: 1px solid #ccc; padding: 0.5rem; text-align: left; }}
    th {{ background: #f4f4f4; }}
  </style>
</head>
<body>
  <h1>ISBN list updated</h1>
  <p>{row_count} CSV rows read; {len(scans)} unique ISBNs processed.</p>
  {_table("New ISBNs added", added)}
  {_table("ISBNs updated", updated)}
  {_table("Already up to date", up_to_date)}
</body>
</html>"""
    )
