import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from book_storage import (
    BookRecord,
    PendingScan,
    apply_batch,
    create_location,
    load_books,
    load_locations,
    save_books,
)


class BookStorageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary_directory.name)
        self.locations_path = self.path / "locations.json"
        self.locations_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "locations": [
                        {"id": "books", "name": "Books", "parent_id": None},
                        {"id": "unknown", "name": "Unknown", "parent_id": "books"},
                    ],
                }
            ),
            encoding="utf-8",
        )
        self.locations = load_locations(self.locations_path)

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_valid_scan_replaces_unknown_timestamp_and_location(self) -> None:
        books = {"9788476724651": BookRecord(None, "unknown")}

        result = apply_batch(
            books,
            {
                "9788476724651": PendingScan(
                    datetime(2026, 10, 17, 11, 26, 29),
                    None,
                )
            },
            "books",
        )

        self.assertEqual(len(result.updated), 1)
        self.assertEqual(
            books["9788476724651"],
            BookRecord(datetime(2026, 10, 17, 11, 26, 29), "books"),
        )

    def test_unknown_incoming_timestamp_does_not_replace_existing_book(self) -> None:
        original = BookRecord(datetime(2026, 10, 17, 11, 26, 29), "books")
        books = {"9788476724651": original}

        result = apply_batch(
            books,
            {"9788476724651": PendingScan(None, "missing")},
            "unknown",
        )

        self.assertEqual(len(result.unchanged), 1)
        self.assertEqual(books["9788476724651"], original)

    def test_location_updates_follow_timestamp_order(self) -> None:
        original = BookRecord(datetime(2026, 10, 17, 11, 26, 29), "books")
        for timestamp, location_id, should_update in (
            (datetime(2026, 10, 17, 11, 26, 28), "unknown", False),
            (original.scanned_at, "unknown", True),
            (datetime(2026, 10, 17, 11, 26, 30), "unknown", True),
            (original.scanned_at, "books", False),
            (datetime(2026, 10, 17, 11, 26, 30), "books", True),
        ):
            with self.subTest(timestamp=timestamp, location_id=location_id):
                books = {"9788476724651": original}
                incoming = BookRecord(timestamp, location_id)

                result = apply_batch(
                    books,
                    {"9788476724651": PendingScan(timestamp, None)},
                    location_id,
                )

                self.assertEqual(
                    books["9788476724651"],
                    incoming if should_update else original,
                )
                self.assertEqual(
                    result.updated,
                    [("9788476724651", incoming, original)] if should_update else [],
                )
                self.assertEqual(
                    result.unchanged,
                    [] if should_update else [("9788476724651", incoming, original)],
                )

    def test_new_unknown_timestamp_is_saved_with_selected_location(self) -> None:
        books: dict[str, BookRecord] = {}

        result = apply_batch(
            books,
            {"9788476724651": PendingScan(None, "invalid")},
            "books",
        )

        self.assertEqual(len(result.added), 1)
        self.assertEqual(books["9788476724651"], BookRecord(None, "books"))

    def test_creates_location_and_round_trips_books(self) -> None:
        location = create_location(
            self.locations_path,
            self.locations,
            "Caja 1",
            "books",
        )
        books_path = self.path / "isbn_list.json"
        books = {"9788476724651": BookRecord(None, location.id)}

        save_books(books_path, books)

        self.assertEqual(load_books(books_path, self.locations), books)
