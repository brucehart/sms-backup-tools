---
name: update-sms-backup-databases
description: Update local SMS Backup & Restore SQLite databases from new `sms-*.xml` and `calls-*.xml` exports. Use when Codex needs to import the newest SMS/MMS and call-log XML files into `messages.db` and `call-log.db`, extract unique MMS images, and update `imageSignatures.db` with the repository tools.
---

# Update SMS Backup Databases

## Overview

Use the existing `tools/` scripts to incrementally import SMS Backup & Restore XML exports into the local databases and image signature index. Treat the workflow as live data maintenance: inspect paths and counts first, do not overwrite user data, and verify every database after the run.

## Working Directory

Run commands from the backup root, the directory that contains:

- `tools/`
- `messages.db`
- `call-log.db`
- `imageSignatures.db`
- `sms-*.xml`
- `calls-*.xml`

If the current directory is `tools/`, use its parent as the backup root.

## Workflow

1. Identify the newest backup files by modification time and filename:

```bash
find . -maxdepth 1 -type f \( -name 'sms-*.xml' -o -name 'calls-*.xml' \) \
  -printf '%TY-%Tm-%Td %TH:%TM:%TS %p\n' | sort -r
```

2. Capture baseline counts before importing:

```bash
sqlite3 messages.db 'select count(*) from messages;'
sqlite3 call-log.db 'select count(*) from calls;'
sqlite3 imageSignatures.db 'select count(*) from image_signatures;'
```

3. Import the newest SMS XML into `messages.db`:

```bash
python3 tools/smsToDb.py sms-YYYYMMDDHHMMSS.xml --db messages.db
```

4. Import the newest call XML into `call-log.db`:

```bash
python3 tools/callsToDb.py calls-YYYYMMDDHHMMSS.xml --db call-log.db
```

5. Run MMS image extraction and signature updates against the same SMS XML:

```bash
python3 tools/extract_unique_sms_images.py extract \
  --xml sms-YYYYMMDDHHMMSS.xml \
  --signature-db imageSignatures.db \
  --update-signature-db \
  --output-dir unique_sms_images \
  --progress-every 250
```

If `numpy` or `Pillow` is missing, create a temporary virtual environment outside the backup folder and rerun the extractor with that Python:

```bash
python3 -m venv /tmp/sms-backup-tools-venv
/tmp/sms-backup-tools-venv/bin/python -m pip install --upgrade pip
/tmp/sms-backup-tools-venv/bin/python -m pip install pillow numpy
/tmp/sms-backup-tools-venv/bin/python tools/extract_unique_sms_images.py extract \
  --xml sms-YYYYMMDDHHMMSS.xml \
  --signature-db imageSignatures.db \
  --update-signature-db \
  --output-dir unique_sms_images \
  --progress-every 250
```

6. Verify integrity and final counts:

```bash
sqlite3 messages.db 'pragma integrity_check; select count(*) from messages;'
sqlite3 call-log.db 'pragma integrity_check; select count(*) from calls;'
sqlite3 imageSignatures.db 'pragma integrity_check; select count(*) from image_signatures;'
```

7. Summarize the import outputs and final state:

- XML files used
- message rows inserted/skipped
- call rows inserted/skipped
- image parts processed, unique, duplicate, review, and error counts
- final row counts for all three databases
- location of `unique_sms_images/decisions.csv` and any `_review` files

## Operational Notes

- The import scripts are incremental and use duplicate suppression; re-importing the same XML should not duplicate rows.
- Use `imageSignatures.db` as both the preload reference and the update target for newly accepted unique images.
- Keep generated Python environments and dependency caches outside the Google Drive backup folder.
- If image extraction aborts mid-run after writing files, inspect `unique_sms_images/decisions.csv` and remove only partial artifacts from that failed run before retrying.
- On Google Drive mounts, timestamp-setting may be unsupported. A warning from `extract_unique_sms_images.py` about file timestamps is acceptable if extraction continues.
- Do not delete XML exports, databases, or existing images unless the user explicitly asks.
