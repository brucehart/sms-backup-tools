#!/usr/bin/env python3
"""Extract image parts from an SMS Backup & Restore XML file and dedupe them.

The script indexes reference images from a ZIP file, then streams an XML backup
produced by SMS Backup & Restore. HEIC/HEIF images are converted to JPEG
through ImageMagick `convert` so they can be read reliably, while other formats
are preserved as-is. MMS image parts are compared against the ZIP contents and
against images accepted earlier in the same run.

Duplicate detection uses two stages:
1. A normalized pixel hash catches metadata-only differences.
2. Perceptual hashes plus a small grayscale preview catch resized or
   recompressed versions of the same image.

Strong matches are skipped. Borderline matches can be written to a review
directory instead of the main output directory.
"""

from __future__ import annotations

import argparse
import base64
import csv
import datetime as dt
import hashlib
import io
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import zipfile
import xml.etree.ElementTree as ET
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Sequence, Tuple

import numpy as np
from PIL import Image, ImageFile, ImageOps, UnidentifiedImageError

ImageFile.LOAD_TRUNCATED_IMAGES = True
Image.MAX_IMAGE_PIXELS = None


RESAMPLE = Image.Resampling.LANCZOS
IMAGE_EXTENSIONS = {
    ".jpg",
    ".jpeg",
    ".png",
    ".gif",
    ".webp",
    ".bmp",
    ".tif",
    ".tiff",
    ".heic",
    ".heif",
    ".avif",
}
MIME_TO_EXTENSION = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/bmp": ".bmp",
    "image/tiff": ".tif",
    "image/heic": ".heic",
    "image/heif": ".heif",
    "image/avif": ".avif",
}
PIL_FORMAT_TO_EXTENSION = {
    "jpeg": ".jpg",
    "jpg": ".jpg",
    "png": ".png",
    "gif": ".gif",
    "webp": ".webp",
    "bmp": ".bmp",
    "tiff": ".tif",
    "heic": ".heic",
    "heif": ".heif",
    "avif": ".avif",
}
HEIC_OUTPUT_EXTENSION = ".jpg"
JPEG_EXTENSIONS = {".jpg", ".jpeg"}
EXIF_DATETIME = 0x0132
EXIF_DATETIME_ORIGINAL = 0x9003
EXIF_DATETIME_DIGITIZED = 0x9004
EXIF_OFFSET_TIME = 0x9010
EXIF_OFFSET_TIME_ORIGINAL = 0x9011
EXIF_OFFSET_TIME_DIGITIZED = 0x9012
EXIF_SUBSEC_TIME = 0x9290
EXIF_SUBSEC_TIME_ORIGINAL = 0x9291
EXIF_SUBSEC_TIME_DIGITIZED = 0x9292
SIGNATURE_TABLE_NAME = "image_signatures"
CREATE_SIGNATURE_TABLE_SQL = f"""
CREATE TABLE IF NOT EXISTS {SIGNATURE_TABLE_NAME} (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_kind TEXT NOT NULL,
    source_name TEXT NOT NULL,
    width INTEGER NOT NULL,
    height INTEGER NOT NULL,
    aspect_ratio REAL NOT NULL,
    normalized_sha256 TEXT NOT NULL,
    ahash_hex TEXT NOT NULL,
    dhash_hex TEXT NOT NULL,
    phash_hex TEXT NOT NULL,
    preview_bytes BLOB NOT NULL,
    UNIQUE(source_kind, source_name)
)
"""
CREATE_SIGNATURE_INDEX_SQL = (
    f"CREATE INDEX IF NOT EXISTS idx_{SIGNATURE_TABLE_NAME}_sha256 "
    f"ON {SIGNATURE_TABLE_NAME}(normalized_sha256)"
)
UPSERT_SIGNATURE_SQL = f"""
INSERT INTO {SIGNATURE_TABLE_NAME} (
    source_kind,
    source_name,
    width,
    height,
    aspect_ratio,
    normalized_sha256,
    ahash_hex,
    dhash_hex,
    phash_hex,
    preview_bytes
)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(source_kind, source_name) DO UPDATE SET
    width = excluded.width,
    height = excluded.height,
    aspect_ratio = excluded.aspect_ratio,
    normalized_sha256 = excluded.normalized_sha256,
    ahash_hex = excluded.ahash_hex,
    dhash_hex = excluded.dhash_hex,
    phash_hex = excluded.phash_hex,
    preview_bytes = excluded.preview_bytes
"""
SELECT_SIGNATURES_SQL = f"""
SELECT
    source_kind,
    source_name,
    width,
    height,
    aspect_ratio,
    normalized_sha256,
    ahash_hex,
    dhash_hex,
    phash_hex,
    preview_bytes
FROM {SIGNATURE_TABLE_NAME}
ORDER BY id
"""
UTIME_WARNING_EMITTED = False


def eprint(*parts: object) -> None:
    print(*parts, file=sys.stderr)


def hamming_distance(left: int, right: int) -> int:
    return (left ^ right).bit_count()


class BKNode:
    __slots__ = ("value", "payloads", "children")

    def __init__(self, value: int, payload: int) -> None:
        self.value = value
        self.payloads = [payload]
        self.children: Dict[int, BKNode] = {}


class BKTree:
    def __init__(self) -> None:
        self.root: Optional[BKNode] = None

    def add(self, value: int, payload: int) -> None:
        if self.root is None:
            self.root = BKNode(value, payload)
            return

        node = self.root
        while True:
            distance = hamming_distance(value, node.value)
            if distance == 0:
                node.payloads.append(payload)
                return
            child = node.children.get(distance)
            if child is None:
                node.children[distance] = BKNode(value, payload)
                return
            node = child

    def query(self, value: int, max_distance: int) -> List[Tuple[int, int]]:
        if self.root is None:
            return []

        matches: List[Tuple[int, int]] = []
        stack = [self.root]
        while stack:
            node = stack.pop()
            distance = hamming_distance(value, node.value)
            if distance <= max_distance:
                for payload in node.payloads:
                    matches.append((payload, distance))
            lower = distance - max_distance
            upper = distance + max_distance
            for edge, child in node.children.items():
                if lower <= edge <= upper:
                    stack.append(child)
        return matches


@dataclass(frozen=True)
class ImageSignature:
    width: int
    height: int
    aspect_ratio: float
    normalized_sha256: str
    ahash: int
    dhash: int
    phash: int
    preview_bytes: bytes


@dataclass(frozen=True)
class PreparedImage:
    compare_bytes: bytes
    save_bytes: bytes
    output_extension: str


@dataclass
class IndexedImage:
    record_id: int
    source_kind: str
    source_name: str
    width: int
    height: int
    aspect_ratio: float
    normalized_sha256: str
    ahash: int
    dhash: int
    phash: int
    preview_bytes: bytes


@dataclass
class CandidateScore:
    record: IndexedImage
    phash_distance: int
    dhash_distance: int
    ahash_distance: int
    preview_mad: float
    ratio_delta: float


@dataclass
class Thresholds:
    auto_phash: int
    auto_dhash: int
    auto_ahash: int
    auto_preview_mad: float
    auto_ratio_delta: float
    review_phash: int
    review_dhash: int
    review_ahash: int
    review_preview_mad: float
    review_ratio_delta: float


def normalize_cli_argv(argv: Optional[Sequence[str]] = None) -> List[str]:
    values = list(argv) if argv is not None else sys.argv[1:]
    known_commands = {"extract", "update-db"}
    if not values:
        return values
    if values[0] in {"-h", "--help"}:
        return values
    if values[0] not in known_commands:
        return ["extract", *values]
    return values


def add_threshold_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--auto-phash", type=int, default=6)
    parser.add_argument("--auto-dhash", type=int, default=8)
    parser.add_argument("--auto-ahash", type=int, default=8)
    parser.add_argument("--auto-preview-mad", type=float, default=12.0)
    parser.add_argument("--auto-ratio-delta", type=float, default=0.15)

    parser.add_argument("--review-phash", type=int, default=10)
    parser.add_argument("--review-dhash", type=int, default=12)
    parser.add_argument("--review-ahash", type=int, default=12)
    parser.add_argument("--review-preview-mad", type=float, default=20.0)
    parser.add_argument("--review-ratio-delta", type=float, default=0.30)


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Extract image MMS parts from SMS Backup & Restore XML, compare "
            "them against optional ZIP and SQLite signature references, or "
            "update a SQLite signature database from existing images."
        )
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    extract_parser = subparsers.add_parser(
        "extract",
        help="Extract image MMS parts and dedupe against optional ZIP and SQLite references.",
    )
    extract_parser.add_argument("--xml", required=True, help="Path to the SMS backup XML file.")
    extract_parser.add_argument(
        "--zip",
        default=None,
        dest="zip_path",
        help="Optional path to a ZIP file of reference images.",
    )
    extract_parser.add_argument(
        "--signature-db",
        default=None,
        help=(
            "Optional SQLite database of stored image signatures to preload "
            "for comparison."
        ),
    )
    extract_parser.add_argument("--hash-data-file", dest="signature_db", help=argparse.SUPPRESS)
    extract_parser.add_argument(
        "--update-signature-db",
        action="store_true",
        help=(
            "When --signature-db is set, write indexed ZIP images and newly "
            "accepted unique images back into that SQLite database."
        ),
    )
    extract_parser.add_argument(
        "--output-dir",
        default="unique_sms_images",
        help="Directory for unique extracted images. Default: %(default)s",
    )
    extract_parser.add_argument(
        "--review-dir",
        default=None,
        help=(
            "Directory for borderline matches. "
            "Default: <output-dir>/_review when review handling is enabled."
        ),
    )
    extract_parser.add_argument(
        "--log-csv",
        default=None,
        help="Optional CSV log path. Default: <output-dir>/decisions.csv",
    )
    extract_parser.add_argument(
        "--progress-every",
        type=int,
        default=250,
        help="Emit progress every N indexed or processed images. Default: %(default)s",
    )
    extract_parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Process at most this many XML image parts. Useful for tuning thresholds.",
    )
    extract_parser.add_argument(
        "--skip-review",
        action="store_true",
        help="Do not create review files. Borderline cases will be treated as unique.",
    )
    add_threshold_arguments(extract_parser)

    update_parser = subparsers.add_parser(
        "update-db",
        help="Add image signatures from image files, directories, or ZIP archives into SQLite.",
    )
    update_parser.add_argument(
        "--signature-db",
        required=True,
        help="SQLite database path to create or update with image signatures.",
    )
    update_parser.add_argument(
        "--recursive",
        action="store_true",
        help="Recurse into source directories when indexing images.",
    )
    update_parser.add_argument(
        "--progress-every",
        type=int,
        default=250,
        help="Emit progress every N processed images. Default: %(default)s",
    )
    update_parser.add_argument(
        "sources",
        nargs="+",
        help="Image files, directories, or ZIP archives to add to the SQLite database.",
    )

    return parser.parse_args(normalize_cli_argv(argv))


def build_thresholds(args: argparse.Namespace) -> Thresholds:
    return Thresholds(
        auto_phash=args.auto_phash,
        auto_dhash=args.auto_dhash,
        auto_ahash=args.auto_ahash,
        auto_preview_mad=args.auto_preview_mad,
        auto_ratio_delta=args.auto_ratio_delta,
        review_phash=args.review_phash,
        review_dhash=args.review_dhash,
        review_ahash=args.review_ahash,
        review_preview_mad=args.review_preview_mad,
        review_ratio_delta=args.review_ratio_delta,
    )


def hash_to_hex(value: int) -> str:
    return f"{value:016x}"


def hex_to_hash(value: str) -> int:
    return int(value, 16)


def ensure_signature_database(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute(CREATE_SIGNATURE_TABLE_SQL)
    conn.execute(CREATE_SIGNATURE_INDEX_SQL)


class SignatureDatabase:
    def __init__(self, path: Path, conn: sqlite3.Connection) -> None:
        self.path = path
        self.conn = conn

    @classmethod
    def open(cls, path: Path) -> "SignatureDatabase":
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path, timeout=30)
        ensure_signature_database(conn)
        return cls(path, conn)

    def close(self) -> None:
        self.conn.commit()
        self.conn.close()

    def load_into(self, index: "ImageIndex", progress_every: int) -> int:
        loaded = 0
        cursor = self.conn.execute(SELECT_SIGNATURES_SQL)
        for row in cursor:
            signature = ImageSignature(
                width=int(row[2]),
                height=int(row[3]),
                aspect_ratio=float(row[4]),
                normalized_sha256=row[5],
                ahash=hex_to_hash(row[6]),
                dhash=hex_to_hash(row[7]),
                phash=hex_to_hash(row[8]),
                preview_bytes=bytes(row[9]),
            )
            index.add(row[0], row[1], signature)
            loaded += 1
            if progress_every and loaded % progress_every == 0:
                eprint(f"Loaded {loaded} signatures from SQLite...")
        return loaded

    def upsert_signature(self, source_kind: str, source_name: str, signature: ImageSignature) -> None:
        self.conn.execute(
            UPSERT_SIGNATURE_SQL,
            (
                source_kind,
                source_name,
                signature.width,
                signature.height,
                signature.aspect_ratio,
                signature.normalized_sha256,
                hash_to_hex(signature.ahash),
                hash_to_hex(signature.dhash),
                hash_to_hex(signature.phash),
                sqlite3.Binary(signature.preview_bytes),
            ),
        )


def detect_convert_command() -> List[str]:
    convert_path = shutil.which("convert")
    if convert_path:
        return [convert_path]
    return []


def image_to_rgb(image: Image.Image) -> Image.Image:
    image = ImageOps.exif_transpose(image)
    if image.mode in {"RGBA", "LA"} or "transparency" in image.info:
        alpha = image.convert("RGBA")
        background = Image.new("RGBA", alpha.size, (255, 255, 255, 255))
        image = Image.alpha_composite(background, alpha).convert("RGB")
    elif image.mode != "RGB":
        image = image.convert("RGB")
    return image


def normalize_sha256(image: Image.Image) -> str:
    payload = hashlib.sha256()
    payload.update(f"{image.width}x{image.height}:RGB".encode("ascii"))
    payload.update(image.tobytes())
    return payload.hexdigest()


def average_hash(image: Image.Image, size: int = 8) -> int:
    resized = image.convert("L").resize((size, size), RESAMPLE)
    pixels = np.asarray(resized, dtype=np.uint8)
    average = float(pixels.mean())
    bits = pixels > average
    return pack_bits(bits)


def difference_hash(image: Image.Image, width: int = 9, height: int = 8) -> int:
    resized = image.convert("L").resize((width, height), RESAMPLE)
    pixels = np.asarray(resized, dtype=np.uint8)
    bits = pixels[:, 1:] > pixels[:, :-1]
    return pack_bits(bits)


_DCT_MATRIX_CACHE: Dict[int, np.ndarray] = {}


def get_dct_matrix(size: int) -> np.ndarray:
    matrix = _DCT_MATRIX_CACHE.get(size)
    if matrix is not None:
        return matrix

    values = np.arange(size, dtype=np.float32)
    matrix = np.empty((size, size), dtype=np.float32)
    scale0 = np.sqrt(1.0 / size)
    scale = np.sqrt(2.0 / size)
    for k in range(size):
        factor = scale0 if k == 0 else scale
        matrix[k, :] = factor * np.cos((np.pi * (2 * values + 1) * k) / (2.0 * size))
    _DCT_MATRIX_CACHE[size] = matrix
    return matrix


def perceptual_hash(image: Image.Image, hash_size: int = 8, highfreq_factor: int = 4) -> int:
    size = hash_size * highfreq_factor
    pixels = np.asarray(image.convert("L").resize((size, size), RESAMPLE), dtype=np.float32)
    dct_matrix = get_dct_matrix(size)
    dct = dct_matrix @ pixels @ dct_matrix.T
    low = dct[:hash_size, :hash_size]
    median = float(np.median(low.flatten()[1:]))
    bits = low > median
    return pack_bits(bits)


def preview_bytes(image: Image.Image, size: int = 32) -> bytes:
    preview = image.convert("L").resize((size, size), RESAMPLE)
    return preview.tobytes()


def pack_bits(bits: np.ndarray) -> int:
    flattened = bits.astype(np.uint8).ravel()
    value = 0
    for bit in flattened:
        value = (value << 1) | int(bit)
    return value


def build_signature(image_bytes: bytes) -> ImageSignature:
    with Image.open(io.BytesIO(image_bytes)) as opened:
        image = image_to_rgb(opened)
        image.load()

    width, height = image.size
    aspect_ratio = width / height if height else 0.0
    return ImageSignature(
        width=width,
        height=height,
        aspect_ratio=aspect_ratio,
        normalized_sha256=normalize_sha256(image),
        ahash=average_hash(image),
        dhash=difference_hash(image),
        phash=perceptual_hash(image),
        preview_bytes=preview_bytes(image),
    )


def parse_xml_timestamp(raw_value: Optional[str]) -> Optional[dt.datetime]:
    if not raw_value or raw_value.lower() == "null":
        return None

    try:
        numeric = int(raw_value)
    except ValueError:
        return None

    if numeric == 0:
        return None

    if abs(numeric) >= 10**12:
        timestamp = numeric / 1000.0
    else:
        timestamp = float(numeric)

    return dt.datetime.fromtimestamp(timestamp, tz=dt.timezone.utc).astimezone()


def select_message_timestamp(mms_attrs: dict) -> Optional[dt.datetime]:
    for key in ("date_sent", "date"):
        parsed = parse_xml_timestamp(mms_attrs.get(key))
        if parsed is not None:
            return parsed
    return None


def exif_datetime_parts(timestamp: dt.datetime) -> Tuple[str, str, str]:
    main_value = timestamp.strftime("%Y:%m:%d %H:%M:%S")
    subsec_value = f"{int(timestamp.microsecond / 1000):03d}"
    offset_value = timestamp.strftime("%z")
    if offset_value:
        offset_value = f"{offset_value[:3]}:{offset_value[3:]}"
    return main_value, subsec_value, offset_value


def jpeg_safe_image(image: Image.Image) -> Image.Image:
    if image.mode in {"RGBA", "LA"} or "transparency" in image.info:
        alpha = image.convert("RGBA")
        background = Image.new("RGBA", alpha.size, (255, 255, 255, 255))
        return Image.alpha_composite(background, alpha).convert("RGB")
    if image.mode != "RGB":
        return image.convert("RGB")
    return image


def apply_jpeg_metadata(image_bytes: bytes, timestamp: Optional[dt.datetime]) -> bytes:
    if timestamp is None:
        return image_bytes

    with Image.open(io.BytesIO(image_bytes)) as source_image:
        source_image.load()
        image = jpeg_safe_image(source_image)
        exif = source_image.getexif()
        main_value, subsec_value, offset_value = exif_datetime_parts(timestamp)

        exif[EXIF_DATETIME] = main_value
        exif[EXIF_DATETIME_ORIGINAL] = main_value
        exif[EXIF_DATETIME_DIGITIZED] = main_value
        exif[EXIF_SUBSEC_TIME] = subsec_value
        exif[EXIF_SUBSEC_TIME_ORIGINAL] = subsec_value
        exif[EXIF_SUBSEC_TIME_DIGITIZED] = subsec_value
        if offset_value:
            exif[EXIF_OFFSET_TIME] = offset_value
            exif[EXIF_OFFSET_TIME_ORIGINAL] = offset_value
            exif[EXIF_OFFSET_TIME_DIGITIZED] = offset_value

        output = io.BytesIO()
        save_kwargs = {
            "format": "JPEG",
            "exif": exif.tobytes(),
        }
        if "icc_profile" in source_image.info:
            save_kwargs["icc_profile"] = source_image.info["icc_profile"]
        if "dpi" in source_image.info:
            save_kwargs["dpi"] = source_image.info["dpi"]

        try:
            image.save(output, quality="keep", subsampling="keep", qtables="keep", **save_kwargs)
        except Exception:
            image.save(output, quality=95, **save_kwargs)

    return output.getvalue()


def is_heic_source(source_name: Optional[str], mime_type: Optional[str]) -> bool:
    suffix = Path(source_name or "").suffix.lower()
    if suffix in {".heic", ".heif"}:
        return True
    return (mime_type or "").lower() in {"image/heic", "image/heif"}


def detect_extension(source_name: Optional[str], mime_type: Optional[str], image_bytes: Optional[bytes] = None) -> str:
    suffix = Path(source_name or "").suffix.lower()
    if suffix:
        return suffix
    mapped = MIME_TO_EXTENSION.get((mime_type or "").lower())
    if mapped:
        return mapped
    if image_bytes is not None:
        try:
            with Image.open(io.BytesIO(image_bytes)) as opened:
                fmt = opened.format.lower() if opened.format else ""
        except (UnidentifiedImageError, OSError):
            fmt = ""
        mapped = PIL_FORMAT_TO_EXTENSION.get(fmt)
        if mapped:
            return mapped
    return ".bin"


def convert_heic_to_jpeg(image_bytes: bytes, source_name: Optional[str], convert_cmd: Sequence[str]) -> bytes:
    if not convert_cmd:
        raise ValueError("ImageMagick 'convert' command is required for HEIC/HEIF inputs.")

    source_path = Path(source_name or "image")
    suffix = source_path.suffix.lower() or ".img"

    with tempfile.TemporaryDirectory(prefix="sms-image-") as tmpdir:
        tmpdir_path = Path(tmpdir)
        input_path = tmpdir_path / f"input{suffix}"
        output_path = tmpdir_path / f"output{HEIC_OUTPUT_EXTENSION}"
        input_path.write_bytes(image_bytes)

        command = [
            *convert_cmd,
            f"{input_path}[0]",
            "-auto-orient",
            "-background",
            "white",
            "-alpha",
            "remove",
            "-alpha",
            "off",
            "-strip",
            str(output_path),
        ]
        try:
            subprocess.run(
                command,
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
        except subprocess.CalledProcessError as exc:
            stderr = exc.stderr.strip() if exc.stderr else ""
            raise ValueError(f"ImageMagick convert failed for {source_name or 'image'}: {stderr}") from exc

        return output_path.read_bytes()


def prepare_image(image_bytes: bytes, source_name: Optional[str], mime_type: Optional[str], convert_cmd: Sequence[str]) -> PreparedImage:
    if is_heic_source(source_name, mime_type):
        jpeg_bytes = convert_heic_to_jpeg(image_bytes, source_name, convert_cmd)
        return PreparedImage(
            compare_bytes=jpeg_bytes,
            save_bytes=jpeg_bytes,
            output_extension=HEIC_OUTPUT_EXTENSION,
        )

    return PreparedImage(
        compare_bytes=image_bytes,
        save_bytes=image_bytes,
        output_extension=detect_extension(source_name, mime_type, image_bytes),
    )


def is_probable_image(path: str) -> bool:
    return Path(path).suffix.lower() in IMAGE_EXTENSIONS


def sanitize_component(value: Optional[str], fallback: str) -> str:
    text = (value or "").strip()
    if not text or text.lower() == "null":
        text = fallback
    cleaned = []
    for char in text:
        if char.isalnum() or char in {"-", "_", "."}:
            cleaned.append(char)
        else:
            cleaned.append("_")
    normalized = "".join(cleaned).strip("._")
    return normalized or fallback


def unique_path(directory: Path, stem: str, suffix: str) -> Path:
    candidate = directory / f"{stem}{suffix}"
    counter = 1
    while candidate.exists():
        candidate = directory / f"{stem}_{counter:03d}{suffix}"
        counter += 1
    return candidate


class ImageIndex:
    def __init__(self, thresholds: Thresholds) -> None:
        self.thresholds = thresholds
        self.records: List[IndexedImage] = []
        self.exact_index: Dict[str, List[int]] = defaultdict(list)
        self.phash_tree = BKTree()
        self.dhash_tree = BKTree()
        self.ahash_tree = BKTree()

    def add(self, source_kind: str, source_name: str, signature: ImageSignature) -> IndexedImage:
        record = IndexedImage(
            record_id=len(self.records),
            source_kind=source_kind,
            source_name=source_name,
            width=signature.width,
            height=signature.height,
            aspect_ratio=signature.aspect_ratio,
            normalized_sha256=signature.normalized_sha256,
            ahash=signature.ahash,
            dhash=signature.dhash,
            phash=signature.phash,
            preview_bytes=signature.preview_bytes,
        )
        self.records.append(record)
        self.exact_index[record.normalized_sha256].append(record.record_id)
        self.phash_tree.add(record.phash, record.record_id)
        self.dhash_tree.add(record.dhash, record.record_id)
        self.ahash_tree.add(record.ahash, record.record_id)
        return record

    def find_match(self, signature: ImageSignature) -> Tuple[str, Optional[CandidateScore]]:
        exact_hits = self.exact_index.get(signature.normalized_sha256)
        if exact_hits:
            record = self.records[exact_hits[0]]
            return (
                "duplicate",
                CandidateScore(
                    record=record,
                    phash_distance=0,
                    dhash_distance=0,
                    ahash_distance=0,
                    preview_mad=0.0,
                    ratio_delta=0.0,
                ),
            )

        candidates: Dict[int, Dict[str, int]] = defaultdict(dict)
        for record_id, distance in self.phash_tree.query(signature.phash, self.thresholds.review_phash):
            candidates[record_id]["phash"] = min(distance, candidates[record_id].get("phash", 1 << 30))
        for record_id, distance in self.dhash_tree.query(signature.dhash, self.thresholds.review_dhash):
            candidates[record_id]["dhash"] = min(distance, candidates[record_id].get("dhash", 1 << 30))
        for record_id, distance in self.ahash_tree.query(signature.ahash, self.thresholds.review_ahash):
            candidates[record_id]["ahash"] = min(distance, candidates[record_id].get("ahash", 1 << 30))

        best_auto: Optional[CandidateScore] = None
        best_review: Optional[CandidateScore] = None

        for record_id, distances in candidates.items():
            record = self.records[record_id]
            score = CandidateScore(
                record=record,
                phash_distance=distances.get("phash", 64),
                dhash_distance=distances.get("dhash", 64),
                ahash_distance=distances.get("ahash", 64),
                preview_mad=preview_mad(signature.preview_bytes, record.preview_bytes),
                ratio_delta=abs(signature.aspect_ratio - record.aspect_ratio),
            )

            if qualifies_as_duplicate(score, self.thresholds):
                if best_auto is None or rank_candidate(score) < rank_candidate(best_auto):
                    best_auto = score
                continue

            if qualifies_for_review(score, self.thresholds):
                if best_review is None or rank_candidate(score) < rank_candidate(best_review):
                    best_review = score

        if best_auto is not None:
            return "duplicate", best_auto
        if best_review is not None:
            return "review", best_review
        return "unique", None


def preview_mad(left: bytes, right: bytes) -> float:
    left_values = np.frombuffer(left, dtype=np.uint8)
    right_values = np.frombuffer(right, dtype=np.uint8)
    return float(np.mean(np.abs(left_values.astype(np.int16) - right_values.astype(np.int16))))


def qualifies_as_duplicate(score: CandidateScore, thresholds: Thresholds) -> bool:
    close_hashes = sum(
        (
            score.phash_distance <= thresholds.auto_phash,
            score.dhash_distance <= thresholds.auto_dhash,
            score.ahash_distance <= thresholds.auto_ahash,
        )
    )
    return (
        close_hashes >= 2
        and score.preview_mad <= thresholds.auto_preview_mad
        and score.ratio_delta <= thresholds.auto_ratio_delta
    )


def qualifies_for_review(score: CandidateScore, thresholds: Thresholds) -> bool:
    close_hashes = sum(
        (
            score.phash_distance <= thresholds.review_phash,
            score.dhash_distance <= thresholds.review_dhash,
            score.ahash_distance <= thresholds.review_ahash,
        )
    )
    return (
        close_hashes >= 2
        and score.preview_mad <= thresholds.review_preview_mad
        and score.ratio_delta <= thresholds.review_ratio_delta
    )


def rank_candidate(score: CandidateScore) -> Tuple[float, int, int, int]:
    return (
        score.preview_mad + (score.ratio_delta * 100.0),
        score.phash_distance,
        score.dhash_distance,
        score.ahash_distance,
    )


def iter_zip_images(zip_path: Path) -> Iterator[Tuple[str, bytes]]:
    with zipfile.ZipFile(zip_path) as archive:
        for info in archive.infolist():
            if info.is_dir():
                continue
            if not is_probable_image(info.filename):
                continue
            try:
                yield info.filename, archive.read(info)
            except KeyError:
                continue


def iter_directory_images(directory: Path, recursive: bool) -> Iterator[Path]:
    iterator = directory.rglob("*") if recursive else directory.iterdir()
    for path in sorted(iterator):
        if path.is_file() and is_probable_image(path.name):
            yield path


def iter_input_images(sources: Sequence[Path], recursive: bool) -> Iterator[Tuple[str, str, bytes]]:
    for source in sources:
        if not source.exists():
            eprint(f"Skipping missing source: {source}")
            continue

        if source.is_dir():
            for image_path in iter_directory_images(source, recursive):
                try:
                    yield "file", str(image_path.resolve()), image_path.read_bytes()
                except OSError as exc:
                    eprint(f"Skipping unreadable image {image_path}: {exc}")
            continue

        if source.is_file() and zipfile.is_zipfile(source):
            try:
                for name, image_bytes in iter_zip_images(source):
                    yield "zip", f"{source.resolve()}!{name}", image_bytes
            except (OSError, zipfile.BadZipFile) as exc:
                eprint(f"Skipping unreadable ZIP source {source}: {exc}")
            continue

        if source.is_file() and is_probable_image(source.name):
            try:
                yield "file", str(source.resolve()), source.read_bytes()
            except OSError as exc:
                eprint(f"Skipping unreadable image {source}: {exc}")
            continue

        eprint(f"Skipping unsupported source: {source}")


def iter_xml_image_parts(xml_path: Path) -> Iterator[Tuple[dict, dict]]:
    context = ET.iterparse(xml_path, events=("end",))
    for _, elem in context:
        if elem.tag != "mms":
            continue

        mms_attrs = dict(elem.attrib)
        for parts in elem.findall("parts"):
            for part in parts.findall("part"):
                part_attrs = dict(part.attrib)
                mime_type = (part_attrs.get("ct") or "").lower()
                data = part_attrs.get("data")
                if mime_type.startswith("image/") and data and data.lower() != "null":
                    yield mms_attrs, part_attrs
        elem.clear()


def decode_base64_payload(payload: str) -> bytes:
    return base64.b64decode(payload, validate=False)


def make_output_stem(xml_path: Path, mms_attrs: dict, part_attrs: dict) -> str:
    base = sanitize_component(xml_path.stem, "sms")
    date = sanitize_component(mms_attrs.get("date"), "date")
    mms_id = sanitize_component(mms_attrs.get("_id") or mms_attrs.get("m_id"), "mms")
    seq = sanitize_component(part_attrs.get("seq"), "0")
    raw_name = part_attrs.get("name") or part_attrs.get("cl") or "image"
    name = sanitize_component(Path(raw_name).stem, "image")
    return f"{base}_{date}_{mms_id}_{seq}_{name}"


def open_log_writer(log_path: Path) -> Tuple[csv.DictWriter, io.TextIOBase]:
    handle = log_path.open("w", newline="", encoding="utf-8")
    fieldnames = [
        "action",
        "xml_file",
        "mms_internal_id",
        "mms_message_id",
        "mms_date",
        "mms_readable_date",
        "part_seq",
        "part_name",
        "mime_type",
        "saved_path",
        "matched_source_kind",
        "matched_source_name",
        "phash_distance",
        "dhash_distance",
        "ahash_distance",
        "preview_mad",
        "ratio_delta",
        "error",
    ]
    writer = csv.DictWriter(handle, fieldnames=fieldnames)
    writer.writeheader()
    return writer, handle


def log_decision(
    writer: csv.DictWriter,
    xml_path: Path,
    mms_attrs: dict,
    part_attrs: dict,
    action: str,
    saved_path: Optional[Path] = None,
    candidate: Optional[CandidateScore] = None,
    error: Optional[str] = None,
) -> None:
    row = {
        "action": action,
        "xml_file": str(xml_path),
        "mms_internal_id": mms_attrs.get("_id", ""),
        "mms_message_id": mms_attrs.get("m_id", ""),
        "mms_date": mms_attrs.get("date", ""),
        "mms_readable_date": mms_attrs.get("readable_date", ""),
        "part_seq": part_attrs.get("seq", ""),
        "part_name": part_attrs.get("name") or part_attrs.get("cl") or "",
        "mime_type": part_attrs.get("ct", ""),
        "saved_path": str(saved_path) if saved_path else "",
        "matched_source_kind": candidate.record.source_kind if candidate else "",
        "matched_source_name": candidate.record.source_name if candidate else "",
        "phash_distance": candidate.phash_distance if candidate else "",
        "dhash_distance": candidate.dhash_distance if candidate else "",
        "ahash_distance": candidate.ahash_distance if candidate else "",
        "preview_mad": f"{candidate.preview_mad:.3f}" if candidate else "",
        "ratio_delta": f"{candidate.ratio_delta:.6f}" if candidate else "",
        "error": error or "",
    }
    writer.writerow(row)


def add_indexed_image(
    index: ImageIndex,
    source_kind: str,
    source_name: str,
    signature: ImageSignature,
    signature_db: Optional[SignatureDatabase],
    persist_to_db: bool = True,
) -> IndexedImage:
    record = index.add(source_kind, source_name, signature)
    if signature_db is not None and persist_to_db:
        signature_db.upsert_signature(source_kind, source_name, signature)
    return record


def index_reference_images(
    zip_path: Path,
    index: ImageIndex,
    progress_every: int,
    convert_cmd: Sequence[str],
    signature_db: Optional[SignatureDatabase],
) -> int:
    indexed = 0
    skipped = 0
    for name, image_bytes in iter_zip_images(zip_path):
        try:
            prepared = prepare_image(image_bytes, name, None, convert_cmd)
            signature = build_signature(prepared.compare_bytes)
        except (UnidentifiedImageError, OSError, ValueError, zipfile.BadZipFile) as exc:
            skipped += 1
            if skipped <= 10:
                eprint(f"Skipping unreadable ZIP image {name!r}: {exc}")
            continue
        add_indexed_image(index, "zip", f"{zip_path}!{name}", signature, signature_db)
        indexed += 1
        if progress_every and indexed % progress_every == 0:
            eprint(f"Indexed {indexed} reference images from ZIP...")
    return indexed


def save_bytes(path: Path, payload: bytes, timestamp: Optional[dt.datetime] = None) -> None:
    global UTIME_WARNING_EMITTED

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    if timestamp is not None:
        epoch = timestamp.timestamp()
        try:
            os.utime(path, (epoch, epoch))
        except OSError as exc:
            if not UTIME_WARNING_EMITTED:
                eprint(f"Could not set extracted file timestamps on this filesystem: {exc}")
                UTIME_WARNING_EMITTED = True


def process_xml(
    xml_path: Path,
    output_dir: Path,
    review_dir: Optional[Path],
    index: ImageIndex,
    writer: csv.DictWriter,
    progress_every: int,
    limit: Optional[int],
    skip_review: bool,
    convert_cmd: Sequence[str],
    signature_db: Optional[SignatureDatabase],
) -> dict:
    stats = {
        "processed": 0,
        "duplicates": 0,
        "unique": 0,
        "review": 0,
        "errors": 0,
    }

    for mms_attrs, part_attrs in iter_xml_image_parts(xml_path):
        if limit is not None and stats["processed"] >= limit:
            break

        stats["processed"] += 1
        try:
            original_payload = decode_base64_payload(part_attrs["data"])
            source_name = part_attrs.get("name") or part_attrs.get("cl") or part_attrs.get("seq") or "image"
            mime_type = part_attrs.get("ct")
            prepared = prepare_image(original_payload, source_name, mime_type, convert_cmd)
            signature = build_signature(prepared.compare_bytes)
        except (KeyError, ValueError, OSError, UnidentifiedImageError, base64.binascii.Error) as exc:
            stats["errors"] += 1
            log_decision(writer, xml_path, mms_attrs, part_attrs, action="error", error=str(exc))
            if stats["errors"] <= 10:
                eprint(
                    "Skipping unreadable XML image part",
                    part_attrs.get("name") or part_attrs.get("cl") or part_attrs.get("seq"),
                    f"({exc})",
                )
            continue

        decision, candidate = index.find_match(signature)
        stem = make_output_stem(xml_path, mms_attrs, part_attrs)
        output_bytes = prepared.save_bytes
        output_timestamp = select_message_timestamp(mms_attrs)
        if prepared.output_extension in JPEG_EXTENSIONS:
            output_bytes = apply_jpeg_metadata(output_bytes, output_timestamp)
        final_signature = signature
        if output_bytes is not prepared.compare_bytes:
            final_signature = build_signature(output_bytes)

        if decision == "duplicate":
            stats["duplicates"] += 1
            log_decision(writer, xml_path, mms_attrs, part_attrs, action="duplicate", candidate=candidate)
        elif decision == "review" and not skip_review:
            stats["review"] += 1
            target = unique_path(review_dir, stem, prepared.output_extension) if review_dir else unique_path(output_dir, stem, prepared.output_extension)
            save_bytes(target, output_bytes, output_timestamp)
            log_decision(writer, xml_path, mms_attrs, part_attrs, action="review", saved_path=target, candidate=candidate)
            add_indexed_image(index, "review", str(target), final_signature, signature_db, persist_to_db=False)
        else:
            stats["unique"] += 1
            target = unique_path(output_dir, stem, prepared.output_extension)
            save_bytes(target, output_bytes, output_timestamp)
            log_decision(writer, xml_path, mms_attrs, part_attrs, action="unique", saved_path=target, candidate=candidate)
            add_indexed_image(index, "saved", str(target), final_signature, signature_db)

        if progress_every and stats["processed"] % progress_every == 0:
            eprint(
                "Processed",
                stats["processed"],
                "XML image parts...",
                f"unique={stats['unique']}",
                f"duplicate={stats['duplicates']}",
                f"review={stats['review']}",
                f"errors={stats['errors']}",
            )

    return stats


def validate_extract_paths(xml_path: Path, zip_path: Optional[Path]) -> None:
    if not xml_path.is_file():
        raise SystemExit(f"XML file not found: {xml_path}")
    if zip_path is not None and not zip_path.is_file():
        raise SystemExit(f"ZIP file not found: {zip_path}")


def validate_update_sources(sources: Sequence[Path]) -> None:
    if not sources:
        raise SystemExit("At least one source path is required for update-db.")


def run_extract_mode(args: argparse.Namespace) -> int:
    xml_path = Path(args.xml).expanduser().resolve()
    zip_path = Path(args.zip_path).expanduser().resolve() if args.zip_path else None
    signature_db_path = Path(args.signature_db).expanduser().resolve() if args.signature_db else None
    output_dir = Path(args.output_dir).expanduser().resolve()
    review_dir = None
    if not args.skip_review:
        if args.review_dir:
            review_dir = Path(args.review_dir).expanduser().resolve()
        else:
            review_dir = output_dir / "_review"
        review_dir.mkdir(parents=True, exist_ok=True)

    output_dir.mkdir(parents=True, exist_ok=True)
    log_path = Path(args.log_csv).expanduser().resolve() if args.log_csv else (output_dir / "decisions.csv")
    log_path.parent.mkdir(parents=True, exist_ok=True)

    if args.update_signature_db and signature_db_path is None:
        raise SystemExit("--update-signature-db requires --signature-db.")
    if signature_db_path is not None and not signature_db_path.exists() and not args.update_signature_db:
        raise SystemExit(f"Signature DB not found: {signature_db_path}")

    validate_extract_paths(xml_path, zip_path)
    thresholds = build_thresholds(args)
    index = ImageIndex(thresholds)
    convert_cmd = detect_convert_command()
    writer, handle = open_log_writer(log_path)
    signature_db: Optional[SignatureDatabase] = None
    loaded_from_db = 0
    indexed = 0

    try:
        if signature_db_path is not None:
            signature_db = SignatureDatabase.open(signature_db_path)
            eprint(f"Loading signature DB: {signature_db_path}")
            loaded_from_db = signature_db.load_into(index, args.progress_every)
            eprint(f"Loaded {loaded_from_db} signatures from SQLite.")

        persist_db = signature_db if args.update_signature_db else None

        if zip_path is not None:
            eprint(f"Indexing reference ZIP: {zip_path}")
            indexed = index_reference_images(zip_path, index, args.progress_every, convert_cmd, persist_db)
            eprint(f"Indexed {indexed} reference images from ZIP.")

        eprint(f"Processing XML backup: {xml_path}")
        stats = process_xml(
            xml_path=xml_path,
            output_dir=output_dir,
            review_dir=review_dir,
            index=index,
            writer=writer,
            progress_every=args.progress_every,
            limit=args.limit,
            skip_review=args.skip_review,
            convert_cmd=convert_cmd,
            signature_db=persist_db,
        )
    finally:
        handle.close()
        if signature_db is not None:
            signature_db.close()

    print(f"Reference signatures loaded from SQLite: {loaded_from_db}")
    print(f"Reference images indexed from ZIP: {indexed}")
    print(f"XML image parts processed: {stats['processed']}")
    print(f"Unique images saved: {stats['unique']}")
    print(f"Duplicates skipped: {stats['duplicates']}")
    print(f"Review images saved: {stats['review']}")
    print(f"Errors: {stats['errors']}")
    print(f"Log CSV: {log_path}")
    if signature_db_path is not None:
        print(f"Signature DB: {signature_db_path}")
        if args.update_signature_db:
            print(f"Signature DB writes attempted: {indexed + stats['unique']}")
    if review_dir is not None:
        print(f"Review directory: {review_dir}")
    print(f"Output directory: {output_dir}")
    return 0


def run_update_db_mode(args: argparse.Namespace) -> int:
    signature_db_path = Path(args.signature_db).expanduser().resolve()
    sources = [Path(value).expanduser().resolve() for value in args.sources]
    validate_update_sources(sources)

    convert_cmd = detect_convert_command()
    signature_db = SignatureDatabase.open(signature_db_path)
    stats = {
        "processed": 0,
        "stored": 0,
        "errors": 0,
    }

    try:
        for source_kind, source_name, image_bytes in iter_input_images(sources, args.recursive):
            stats["processed"] += 1
            try:
                prepared = prepare_image(image_bytes, source_name, None, convert_cmd)
                signature = build_signature(prepared.compare_bytes)
                signature_db.upsert_signature(source_kind, source_name, signature)
                stats["stored"] += 1
            except (UnidentifiedImageError, OSError, ValueError, zipfile.BadZipFile) as exc:
                stats["errors"] += 1
                if stats["errors"] <= 10:
                    eprint(f"Skipping unreadable source {source_name}: {exc}")
                continue

            if args.progress_every and stats["processed"] % args.progress_every == 0:
                eprint(
                    "Indexed",
                    stats["processed"],
                    "images into SQLite...",
                    f"stored={stats['stored']}",
                    f"errors={stats['errors']}",
                )
    finally:
        signature_db.close()

    print(f"Sources processed: {stats['processed']}")
    print(f"Signature records stored/updated: {stats['stored']}")
    print(f"Errors: {stats['errors']}")
    print(f"Signature DB: {signature_db_path}")
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    if args.command == "extract":
        return run_extract_mode(args)
    if args.command == "update-db":
        return run_update_db_mode(args)
    raise SystemExit(f"Unsupported command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
