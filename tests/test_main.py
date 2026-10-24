import asyncio
from datetime import datetime
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import main
from book_storage import BookRecord, load_books, save_books
from main import _parse_csv, _parse_timestamp


class ErrorPageTests(unittest.TestCase):
    def test_preserves_the_requested_status_code(self) -> None:
        self.assertEqual(main._error_page(404, "Not found").status_code, 404)


class ParseTimestampTests(unittest.TestCase):
    def test_parses_qr2csv_month_first_timestamp(self) -> None:
        self.assertEqual(
            _parse_timestamp("10/17/2026 11:26:29"),
            datetime(2026, 10, 17, 11, 26, 29),
        )

    def test_parses_stored_iso_timestamp(self) -> None:
        self.assertEqual(
            _parse_timestamp("2026-10-17T11:26:29"),
            datetime(2026, 10, 17, 11, 26, 29),
        )


class ParseCsvTests(unittest.TestCase):
    def test_tolerates_missing_and_invalid_timestamps(self) -> None:
        batch = _parse_csv(
            b"""Data,Timestamp
9788476724651,10/17/2026 11:26:29
9788408031215,
9780747580690,not-a-date
,10/17/2026 11:26:29
"""
        )

        self.assertEqual(batch.row_count, 4)
        self.assertEqual(batch.skipped_rows, [5])
        self.assertEqual(
            batch.scans["9788476724651"].scanned_at,
            datetime(2026, 10, 17, 11, 26, 29),
        )
        self.assertEqual(batch.scans["9788408031215"].timestamp_issue, "missing")
        self.assertEqual(batch.scans["9780747580690"].timestamp_issue, "invalid")


class CommitScanBatchTests(unittest.TestCase):
    def setUp(self) -> None:
        main.PENDING_BATCH = None

    def tearDown(self) -> None:
        main.PENDING_BATCH = None

    def test_failed_save_keeps_pending_batch_for_retry(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with (
                patch.object(main, "ISBN_LIST_PATH", root / "isbn_list.json"),
                patch.object(
                    main, "save_books", side_effect=ValueError("Could not save isbn_list.json")
                ),
            ):
                batch = _parse_csv(b"Data,Timestamp\n9788476724651,\n")
                main.PENDING_BATCH = batch

                with self.assertRaises(main.HTTPException) as error:
                    asyncio.run(main.commit_scan_batch("trastero-caja-1", "", ""))

                self.assertEqual(error.exception.detail, "Could not save isbn_list.json")
                self.assertIs(main.PENDING_BATCH, batch)
                self.assertFalse(main.ISBN_LIST_PATH.exists())

    def test_same_timestamp_location_correction_is_persisted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with (
                patch.object(main, "ISBN_LIST_PATH", root / "isbn_list.json"),
            ):
                timestamp = datetime(2026, 10, 17, 11, 26, 29)
                save_books(
                    main.ISBN_LIST_PATH,
                    {"9788476724651": BookRecord(timestamp, "unknown")},
                )
                batch = _parse_csv(
                    b"Data,Timestamp\n9788476724651,10/17/2026 11:26:29\n"
                )
                main.PENDING_BATCH = batch

                response = asyncio.run(
                    main.commit_scan_batch("trastero-caja-1", "", "")
                )

                self.assertEqual(response.status_code, 200)
                self.assertIn(b"ISBNs updated (1)", response.body)
                self.assertEqual(
                    load_books(
                        main.ISBN_LIST_PATH,
                        main.load_locations(main.LOCATIONS_PATH),
                    )["9788476724651"],
                    BookRecord(timestamp, "trastero-caja-1"),
                )
                self.assertIsNone(main.PENDING_BATCH)

    def test_new_upload_replaces_the_waiting_batch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(main, "ISBN_LIST_PATH", root / "isbn_list.json"):
                main.PENDING_BATCH = _parse_csv(
                    b"Data,Timestamp\n9788476724651,10/17/2026 11:26:29\n"
                )
                main.PENDING_BATCH = _parse_csv(
                    b"Data,Timestamp\n9788408031215,10/17/2026 11:26:29\n"
                )

                asyncio.run(main.commit_scan_batch("trastero-caja-1", "", ""))

                books = load_books(
                    main.ISBN_LIST_PATH,
                    main.load_locations(main.LOCATIONS_PATH),
                )
                self.assertEqual(set(books), {"9788408031215"})
                self.assertIsNone(main.PENDING_BATCH)

    def test_commits_selected_location_to_new_books(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original_isbn_path = main.ISBN_LIST_PATH
            original_locations_path = main.LOCATIONS_PATH
            try:
                main.ISBN_LIST_PATH = root / "isbn_list.json"
                main.LOCATIONS_PATH = original_locations_path
                batch = _parse_csv(b"Data,Timestamp\n9788476724651,\n")
                main.PENDING_BATCH = batch

                response = asyncio.run(
                    main.commit_scan_batch(
                        "trastero-caja-1",
                        "",
                        "",
                    )
                )

                self.assertEqual(response.status_code, 200)
                books = load_books(
                    main.ISBN_LIST_PATH,
                    main.load_locations(main.LOCATIONS_PATH),
                )
                self.assertIsNone(books["9788476724651"].scanned_at)
                self.assertEqual(
                    books["9788476724651"].location_id,
                    "trastero-caja-1",
                )
                self.assertIsNone(main.PENDING_BATCH)
            finally:
                main.ISBN_LIST_PATH = original_isbn_path
                main.LOCATIONS_PATH = original_locations_path

    def test_prefers_valid_duplicate_timestamp(self) -> None:
        batch = _parse_csv(
            b"""Data,Timestamp
9788476724651,
9788476724651,10/17/2026 11:26:29
"""
        )

        self.assertEqual(
            batch.scans["9788476724651"].scanned_at,
            datetime(2026, 10, 17, 11, 26, 29),
        )
