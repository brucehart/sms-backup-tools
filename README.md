# sms-backup-tools

Utilities for working with XML exports from [SMS Backup & Restore](https://www.synctech.com.au/sms-backup-restore/). This repo contains scripts for:

- importing SMS/MMS text content into SQLite
- importing call logs into SQLite
- extracting MMS image attachments
- deduplicating extracted images against existing archives
- managing older hash-based image cleanup workflows

## Repository Contents

| File | Purpose |
| --- | --- |
| `smsToDb.py` | Imports SMS and text-bearing MMS messages from XML into SQLite. |
| `callsToDb.py` | Imports call-log XML into SQLite with duplicate suppression and metadata refresh. |
| `extract_unique_sms_images.py` | Main image-extraction tool. Extracts MMS image parts, dedupes against ZIP and/or SQLite signature references, and can stage borderline matches for review. |
| `extractImages.py` | Simpler legacy extractor that writes every image part from an XML file. |
| `imgHash.py` | Legacy MD5-based helper for building a hash list and finding matching files in a directory. |
| `deleteFiles.sh` | Deletes files listed in a text file from a target directory. |
| `imgHash.txt` | Existing image-hash reference data used by the legacy hash workflow. |
| `sms.xsd` | XML schema file for SMS Backup & Restore exports. |

## Requirements

The database import scripts use only the Python standard library.

`extract_unique_sms_images.py` also requires:

- Python 3
- `Pillow`
- `numpy`
- ImageMagick `convert` if you need HEIC/HEIF inputs converted to JPEG

Example setup:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install pillow numpy
```

## Common Workflows

### Import messages into SQLite

Imports SMS plus MMS entries that contain text parts. Binary attachments are ignored.

```bash
python3 smsToDb.py /path/to/sms-backup.xml --db messages.db
```

Behavior:

- normalizes phone numbers
- stores rows in a `messages` table
- ignores duplicates via a SQLite `UNIQUE` constraint

### Import call logs into SQLite

Imports one or more call-log XML files into a `calls` table.

```bash
python3 callsToDb.py /path/to/calls-1.xml /path/to/calls-2.xml --db call-log.db
```

Behavior:

- normalizes phone numbers
- suppresses duplicate calls based on stable call identity
- refreshes stored metadata when a later import has better contact or date information

### Extract and dedupe MMS images

This is the main image workflow in the repo.

```bash
python3 extract_unique_sms_images.py extract \
  --xml /path/to/sms-backup.xml \
  --zip /path/to/reference-images.zip \
  --signature-db image-signatures.db \
  --update-signature-db \
  --output-dir unique_sms_images
```

What it does:

- streams MMS image parts from SMS Backup & Restore XML
- compares them against optional ZIP reference images
- compares them against an optional SQLite signature database
- treats strong matches as duplicates
- writes borderline matches to a review directory unless `--skip-review` is used
- logs decisions to CSV

Useful flags:

- `--review-dir /path/to/review`
- `--log-csv decisions.csv`
- `--limit 1000`
- `--progress-every 250`
- `--skip-review`

To build or refresh the signature database from existing images, directories, or ZIP archives:

```bash
python3 extract_unique_sms_images.py update-db \
  --signature-db image-signatures.db \
  --recursive \
  /path/to/images \
  /path/to/other-images.zip
```

## Legacy Helpers

### Extract every image without dedupe

```bash
python3 extractImages.py /path/to/sms-backup.xml extracted_images
```

This script writes all image parts it finds and names them from the MMS timestamp plus a random suffix.

### Build or query a simple MD5 hash list

Store hashes for a directory:

```bash
python3 imgHash.py store /path/to/images imgHash.txt
```

Write matching filenames from another directory:

```bash
python3 imgHash.py find /path/to/images imgHash.txt matches.txt
```

### Delete files listed in a text file

```bash
./deleteFiles.sh /path/to/images matches.txt
```

`matches.txt` should contain one filename per line.

## Notes

- The import scripts are designed for incremental runs, so re-importing the same XML should not duplicate records.
- The newer image workflow in `extract_unique_sms_images.py` is the preferred option over the older `extractImages.py` and `imgHash.py` helpers.
