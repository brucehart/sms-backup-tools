#!/usr/bin/env python3

import argparse
import re
import sqlite3
import sys
import xml.etree.ElementTree as ET
from collections import Counter
from datetime import datetime
from pathlib import Path

TABLE_NAME = "messages"

CREATE_TABLE_SQL = f"""
CREATE TABLE IF NOT EXISTS {TABLE_NAME} (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    address TEXT,
    date TEXT,
    type TEXT,
    body TEXT,
    sender TEXT,
    receiver TEXT,
    UNIQUE(address, date, type, body, sender, receiver)
)
"""

INSERT_SQL = f"""
INSERT OR IGNORE INTO {TABLE_NAME} (address, date, type, body, sender, receiver)
VALUES (?, ?, ?, ?, ?, ?)
"""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Import SMS Backup & Restore XML into messages.db. "
            "SMS and text-bearing MMS are imported; file attachments are ignored."
        )
    )
    parser.add_argument("xml_path", type=Path, help="Path to an SMS Backup & Restore XML file")
    parser.add_argument(
        "--db",
        type=Path,
        default=Path("messages.db"),
        help="SQLite database path (default: ./messages.db)",
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


def convert_date(unix_epoch_ms: str | None) -> str:
    if not unix_epoch_ms:
        raise ValueError("Missing date value")
    return datetime.fromtimestamp(int(unix_epoch_ms) / 1000).strftime("%Y-%m-%d %H:%M:%S")


def ensure_database(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute(CREATE_TABLE_SQL)


def unique_preserving_order(values: list[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for value in values:
        if not value or value in seen:
            continue
        seen.add(value)
        ordered.append(value)
    return ordered


def build_sms_row(elem: ET.Element) -> tuple[str, str, str, str, str, str]:
    address = normalize_phone_number(elem.get("address"))
    date = convert_date(elem.get("date"))
    msg_type = elem.get("type", "")
    body = elem.get("body") or ""
    sender = address if msg_type == "1" else "me"
    receiver = "me" if msg_type == "1" else address
    return (address, date, msg_type, body, sender, receiver)


def build_mms_row(elem: ET.Element) -> tuple[str, str, str, str, str, str] | None:
    date = convert_date(elem.get("date"))
    msg_type = elem.get("msg_box", "")

    sender = ""
    receivers: list[str] = []
    for addr in elem.findall(".//addr"):
        normalized = normalize_phone_number(addr.get("address"))
        if not normalized:
            continue
        if addr.get("type") == "137":
            sender = normalized
        else:
            receivers.append(normalized)

    body_parts = []
    for part in elem.findall(".//part"):
        content_type = part.get("ct") or ""
        if content_type.startswith("text/"):
            text = part.get("text") or ""
            if text:
                body_parts.append(text)

    body = "\n".join(body_parts).strip()
    if not body:
        return None

    receiver = ", ".join(unique_preserving_order(receivers))
    address = sender or normalize_phone_number(elem.get("address")) or receiver.split(", ", 1)[0]
    return (address, date, msg_type, body, sender, receiver)


def import_xml_file(xml_path: Path, conn: sqlite3.Connection) -> Counter:
    stats: Counter = Counter()
    cursor = conn.cursor()

    context = ET.iterparse(xml_path, events=("start", "end"))
    _, root = next(context)

    for event, elem in context:
        if event != "end":
            continue

        row = None
        if elem.tag == "sms":
            stats["sms_seen"] += 1
            row = build_sms_row(elem)
        elif elem.tag == "mms":
            stats["mms_seen"] += 1
            row = build_mms_row(elem)
            if row is None:
                stats["mms_without_text"] += 1

        if elem.tag in {"sms", "mms"}:
            if row is not None:
                cursor.execute(INSERT_SQL, row)
                if cursor.rowcount == 1:
                    stats["inserted"] += 1
                else:
                    stats["skipped_existing"] += 1
            root.clear()

    return stats


def main() -> int:
    args = parse_args()

    if not args.xml_path.is_file():
        print(f"XML file not found: {args.xml_path}", file=sys.stderr)
        return 1

    args.db.parent.mkdir(parents=True, exist_ok=True)

    with sqlite3.connect(args.db, timeout=30) as conn:
        ensure_database(conn)
        stats = import_xml_file(args.xml_path, conn)

    print(f"Imported from {args.xml_path}")
    print(f"Database: {args.db}")
    print(f"SMS seen: {stats['sms_seen']}")
    print(f"MMS seen: {stats['mms_seen']}")
    print(f"MMS skipped with no text: {stats['mms_without_text']}")
    print(f"Inserted: {stats['inserted']}")
    print(f"Skipped existing: {stats['skipped_existing']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
