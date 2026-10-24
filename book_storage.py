import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


ROOT_LOCATION_ID = "books"
UNKNOWN_LOCATION_ID = "unknown"


@dataclass(frozen=True)
class Location:
    id: str
    name: str
    parent_id: str | None


@dataclass(frozen=True)
class BookRecord:
    scanned_at: datetime | None
    location_id: str


@dataclass(frozen=True)
class PendingScan:
    scanned_at: datetime | None
    timestamp_issue: str | None


@dataclass(frozen=True)
class PendingBatch:
    row_count: int
    scans: dict[str, PendingScan]
    skipped_rows: list[int]


@dataclass(frozen=True)
class BatchResult:
    added: list[tuple[str, BookRecord]]
    updated: list[tuple[str, BookRecord, BookRecord]]
    unchanged: list[tuple[str, BookRecord, BookRecord]]


def _write_json(path: Path, contents: object) -> None:
    temporary_path = path.with_suffix(".tmp")
    try:
        temporary_path.write_text(
            json.dumps(contents, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        temporary_path.replace(path)
    except OSError as exc:
        raise ValueError(f"Could not save {path.name}") from exc


def _read_json(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Could not read {path.name}") from exc


def load_locations(path: Path) -> dict[str, Location]:
    data = _read_json(path)
    if not isinstance(data, dict) or data.get("schema_version") != 1:
        raise ValueError("locations.json has an unsupported format")
    nodes = data.get("locations")
    if not isinstance(nodes, list):
        raise ValueError("locations.json must contain a locations list")

    locations: dict[str, Location] = {}
    for node in nodes:
        if not isinstance(node, dict):
            raise ValueError("locations.json contains an invalid location")
        location_id = node.get("id")
        name = node.get("name")
        parent_id = node.get("parent_id")
        if (
            not isinstance(location_id, str)
            or not location_id
            or not isinstance(name, str)
            or not name.strip()
            or (parent_id is not None and not isinstance(parent_id, str))
            or location_id in locations
        ):
            raise ValueError("locations.json contains an invalid location")
        locations[location_id] = Location(location_id, name.strip(), parent_id)

    if ROOT_LOCATION_ID not in locations or UNKNOWN_LOCATION_ID not in locations:
        raise ValueError("locations.json must include Books and Unknown")
    if locations[ROOT_LOCATION_ID].parent_id is not None:
        raise ValueError("Books must be the root location")
    if locations[UNKNOWN_LOCATION_ID].parent_id not in locations:
        raise ValueError("Unknown has an invalid parent location")
    for location in locations.values():
        if location.parent_id is not None and location.parent_id not in locations:
            raise ValueError(f"Location {location.name} has an invalid parent")
        seen: set[str] = set()
        current = location
        while current.parent_id is not None:
            if current.id in seen:
                raise ValueError("locations.json contains a location cycle")
            seen.add(current.id)
            current = locations[current.parent_id]
    return locations


def save_locations(path: Path, locations: dict[str, Location]) -> None:
    _write_json(
        path,
        {
            "schema_version": 1,
            "locations": [
                {
                    "id": location.id,
                    "name": location.name,
                    "parent_id": location.parent_id,
                }
                for location in sorted(locations.values(), key=lambda item: item.id)
            ],
        },
    )


def location_path(location_id: str, locations: dict[str, Location]) -> str:
    try:
        location = locations[location_id]
    except KeyError as exc:
        raise ValueError("Book references an unknown location") from exc
    names = [location.name]
    while location.parent_id is not None:
        location = locations[location.parent_id]
        names.append(location.name)
    return " / ".join(reversed(names))


def create_location(
    locations_path: Path,
    locations: dict[str, Location],
    name: str,
    parent_id: str,
) -> Location:
    name = name.strip()
    if not name:
        raise ValueError("New location name is required")
    if parent_id not in locations:
        raise ValueError("New location parent is invalid")

    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    if not slug:
        raise ValueError("New location name must contain letters or numbers")
    location_id = f"{parent_id}-{slug}"
    suffix = 2
    while location_id in locations:
        location_id = f"{parent_id}-{slug}-{suffix}"
        suffix += 1

    location = Location(location_id, name, parent_id)
    locations[location_id] = location
    save_locations(locations_path, locations)
    return location


def load_books(path: Path, locations: dict[str, Location]) -> dict[str, BookRecord]:
    if not path.exists():
        return {}
    data = _read_json(path)
    if not isinstance(data, dict) or data.get("schema_version") != 1:
        raise ValueError(
            "isbn_list.json uses the previous format; remove it and re-upload your CSV files"
        )
    books_data = data.get("books")
    if not isinstance(books_data, dict):
        raise ValueError("isbn_list.json must contain a books object")

    books: dict[str, BookRecord] = {}
    for isbn, record in books_data.items():
        if not isinstance(isbn, str) or not isbn or not isinstance(record, dict):
            raise ValueError("isbn_list.json contains an invalid book record")
        timestamp = record.get("scanned_at")
        location_id = record.get("location_id")
        if timestamp is None:
            scanned_at = None
        elif isinstance(timestamp, str):
            try:
                scanned_at = datetime.fromisoformat(timestamp)
            except ValueError as exc:
                raise ValueError("isbn_list.json contains an invalid timestamp") from exc
        else:
            raise ValueError("isbn_list.json contains an invalid timestamp")
        if not isinstance(location_id, str) or location_id not in locations:
            raise ValueError("isbn_list.json references an unknown location")
        books[isbn] = BookRecord(scanned_at, location_id)
    return books


def save_books(path: Path, books: dict[str, BookRecord]) -> None:
    _write_json(
        path,
        {
            "schema_version": 1,
            "books": {
                isbn: {
                    "scanned_at": (
                        record.scanned_at.isoformat(timespec="seconds")
                        if record.scanned_at
                        else None
                    ),
                    "location_id": record.location_id,
                }
                for isbn, record in sorted(books.items())
            },
        },
    )


def apply_batch(
    books: dict[str, BookRecord],
    scans: dict[str, PendingScan],
    location_id: str,
) -> BatchResult:
    added: list[tuple[str, BookRecord]] = []
    updated: list[tuple[str, BookRecord, BookRecord]] = []
    unchanged: list[tuple[str, BookRecord, BookRecord]] = []
    for isbn, scan in scans.items():
        incoming = BookRecord(scan.scanned_at, location_id)
        previous = books.get(isbn)
        if previous is None:
            books[isbn] = incoming
            added.append((isbn, incoming))
        elif (
            incoming.scanned_at is not None
            and (
                previous.scanned_at is None
                or incoming.scanned_at > previous.scanned_at
                or (
                    incoming.scanned_at == previous.scanned_at
                    and incoming.location_id != previous.location_id
                )
            )
        ):
            books[isbn] = incoming
            updated.append((isbn, incoming, previous))
        else:
            unchanged.append((isbn, incoming, previous))
    return BatchResult(added, updated, unchanged)
