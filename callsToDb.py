#!/usr/bin/env python3

import argparse
import re
import sqlite3
import sys
import xml.etree.ElementTree as ET
from collections import Counter
from datetime import datetime
from pathlib import Path

TABLE_NAME = "calls"

CREATE_TABLE_SQL = f"""
CREATE TABLE IF NOT EXISTS {TABLE_NAME} (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    number TEXT NOT NULL,
    date TEXT NOT NULL,
    date_ms INTEGER NOT NULL,
    duration INTEGER NOT NULL,
    type TEXT NOT NULL,
    presentation TEXT,
    subscription_id TEXT,
    post_dial_digits TEXT,
    subscription_component_name TEXT,
    readable_date TEXT,
    contact_name TEXT,
    UNIQUE(number, date_ms, type, duration, presentation, subscription_id, post_dial_digits)
)
"""

INSERT_SQL = f"""
INSERT OR IGNORE INTO {TABLE_NAME} (
    number,
    date,
    date_ms,
    duration,
    type,
    presentation,
    subscription_id,
    post_dial_digits,
    subscription_component_name,
    readable_date,
    contact_name
)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

SELECT_EXISTING_SQL = f"""
SELECT contact_name, readable_date, subscription_component_name
FROM {TABLE_NAME}
WHERE number = ?
  AND date_ms = ?
  AND type = ?
  AND duration = ?
  AND presentation = ?
  AND subscription_id = ?
  AND post_dial_digits = ?
"""

UPDATE_METADATA_SQL = f"""
UPDATE {TABLE_NAME}
SET contact_name = ?,
    readable_date = ?,
    subscription_component_name = ?
WHERE number = ?
  AND date_ms = ?
  AND type = ?
  AND duration = ?
  AND presentation = ?
  AND subscription_id = ?
  AND post_dial_digits = ?
"""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Import SMS Backup & Restore call-log XML into SQLite. "
            "Duplicate calls are ignored based on stable call identity."
        )
    )
    parser.add_argument(
        "xml_paths",
        nargs="+",
        type=Path,
        help="One or more call XML files exported by SMS Backup & Restore.",
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=Path("call-log.db"),
        help="SQLite database path (default: ./call-log.db)",
    )
    return parser.parse_args()


def normalize_phone_number(value: str | None) -> str:
    if not value:
        return ""

    cleaned = re.sub(r"[\s().-]", "", value.strip())

    if cleaned.lower() == "insert-address-token":
        return ""

    if re.fullmatch(r"\+1\d{10}", cleaned):
        return cleaned[2:]

    return cleaned


def clean_text(value: str | None) -> str:
    return value.strip() if value else ""


def convert_date(unix_epoch_ms: str | None) -> tuple[int, str]:
    if not unix_epoch_ms:
        raise ValueError("Missing date value")

    date_ms = int(unix_epoch_ms)
    date_text = datetime.fromtimestamp(date_ms / 1000).strftime("%Y-%m-%d %H:%M:%S")
    return date_ms, date_text


def ensure_database(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute(CREATE_TABLE_SQL)


def build_call_row(elem: ET.Element) -> tuple[str, str, int, int, str, str, str, str, str, str, str]:
    date_ms, date_text = convert_date(elem.get("date"))
    return (
        normalize_phone_number(elem.get("number")),
        date_text,
        date_ms,
        int(elem.get("duration") or 0),
        clean_text(elem.get("type")),
        clean_text(elem.get("presentation")),
        clean_text(elem.get("subscription_id")),
        clean_text(elem.get("post_dial_digits")),
        clean_text(elem.get("subscription_component_name")),
        clean_text(elem.get("readable_date")),
        clean_text(elem.get("contact_name")),
    )


def is_meaningful_contact_name(value: str) -> bool:
    lowered = value.strip().lower()
    return bool(lowered) and lowered not in {"unknown", "(unknown)"}


def first_non_empty(*values: str) -> str:
    for value in values:
        if value.strip():
            return value
    return ""


def maybe_refresh_metadata(conn: sqlite3.Connection, row: tuple[str, str, int, int, str, str, str, str, str, str, str]) -> bool:
    key = (row[0], row[2], row[4], row[3], row[5], row[6], row[7])
    existing = conn.execute(SELECT_EXISTING_SQL, key).fetchone()
    if existing is None:
        return False

    existing_contact, existing_readable_date, existing_component = existing
    updated_contact = existing_contact
    if not is_meaningful_contact_name(existing_contact or "") and is_meaningful_contact_name(row[10]):
        updated_contact = row[10]

    updated_readable_date = first_non_empty(existing_readable_date or "", row[9])
    updated_component = first_non_empty(existing_component or "", row[8])

    if (
        updated_contact == (existing_contact or "")
        and updated_readable_date == (existing_readable_date or "")
        and updated_component == (existing_component or "")
    ):
        return False

    conn.execute(
        UPDATE_METADATA_SQL,
        (
            updated_contact,
            updated_readable_date,
            updated_component,
            *key,
        ),
    )
    return True


def import_xml_file(xml_path: Path, conn: sqlite3.Connection) -> Counter:
    stats: Counter = Counter()
    cursor = conn.cursor()

    context = ET.iterparse(xml_path, events=("start", "end"))
    _, root = next(context)

    for event, elem in context:
        if event != "end" or elem.tag != "call":
            continue

        stats["calls_seen"] += 1
        row = build_call_row(elem)
        cursor.execute(INSERT_SQL, row)
        if cursor.rowcount == 1:
            stats["inserted"] += 1
        else:
            stats["skipped_existing"] += 1
            if maybe_refresh_metadata(conn, row):
                stats["metadata_refreshed"] += 1
        root.clear()

    return stats


def main() -> int:
    args = parse_args()

    missing = [path for path in args.xml_paths if not path.is_file()]
    if missing:
        for path in missing:
            print(f"XML file not found: {path}", file=sys.stderr)
        return 1

    args.db.parent.mkdir(parents=True, exist_ok=True)

    overall: Counter = Counter()
    with sqlite3.connect(args.db, timeout=30) as conn:
        ensure_database(conn)
        for xml_path in args.xml_paths:
            stats = import_xml_file(xml_path, conn)
            overall.update(stats)
            print(f"Imported from {xml_path}")
            print(f"Calls seen: {stats['calls_seen']}")
            print(f"Inserted: {stats['inserted']}")
            print(f"Skipped existing: {stats['skipped_existing']}")
            print(f"Metadata refreshed: {stats['metadata_refreshed']}")

    print(f"Database: {args.db}")
    print(f"Total calls seen: {overall['calls_seen']}")
    print(f"Total inserted: {overall['inserted']}")
    print(f"Total skipped existing: {overall['skipped_existing']}")
    print(f"Total metadata refreshed: {overall['metadata_refreshed']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
