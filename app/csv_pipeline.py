"""
CSV ingestion pipeline:
  - Read input CSV
  - Validate & normalise UK phone numbers
  - Deduplicate on phone_e164
  - Apply suppression / DNC list
  - Return clean list of CandidateRecords
"""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Optional

import structlog

from app.models import CandidateRecord
from app.phone_utils import normalise_uk_phone

log = structlog.get_logger(__name__)

# Columns we require (case-insensitive matching)
_REQUIRED_COLUMNS = {"unique_record_id", "phone"}
# Columns we recognise but are optional
_OPTIONAL_COLUMNS = {"first_name", "last_name", "email"}


def _load_suppression_list(path: Path) -> set[str]:
    """Load a suppression/DNC file (one phone per line, or CSV with 'phone' column)."""
    suppressed: set[str] = set()
    if not path.exists():
        log.info("suppression_list_not_found", path=str(path))
        return suppressed

    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames and "phone" in [c.lower().strip() for c in reader.fieldnames]:
            col = next(c for c in reader.fieldnames if c.lower().strip() == "phone")
            for row in reader:
                phone_raw = row.get(col, "").strip()
                if phone_raw:
                    e164, valid = normalise_uk_phone(phone_raw)
                    suppressed.add(e164 if valid else phone_raw)
        else:
            # Fall back: treat each line as a phone number
            f.seek(0)
            for line in f:
                phone_raw = line.strip()
                if phone_raw:
                    e164, valid = normalise_uk_phone(phone_raw)
                    suppressed.add(e164 if valid else phone_raw)

    log.info("suppression_list_loaded", count=len(suppressed))
    return suppressed


def ingest_csv(
    csv_path: Path,
    suppression_path: Optional[Path] = None,
) -> tuple[list[CandidateRecord], list[dict]]:
    """
    Read and process a candidate CSV.

    Returns
    -------
    (valid_records, rejected_rows)
        valid_records  – deduplicated, normalised, non-suppressed candidates
        rejected_rows  – rows that failed validation (for audit)
    """
    suppressed = _load_suppression_list(suppression_path) if suppression_path else set()

    valid: list[CandidateRecord] = []
    rejected: list[dict] = []
    seen_phones: set[str] = set()
    seen_ids: set[str] = set()

    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames:
            raise ValueError(f"Empty or malformed CSV: {csv_path}")

        # Map actual header names to our expected names (case-insensitive)
        header_map: dict[str, str] = {}
        for col in reader.fieldnames:
            normalised = col.lower().strip().replace(" ", "_")
            header_map[normalised] = col

        # Check required columns
        for req in _REQUIRED_COLUMNS:
            if req not in header_map:
                raise ValueError(
                    f"CSV missing required column '{req}'. "
                    f"Found: {list(reader.fieldnames)}"
                )

        for row_num, row in enumerate(reader, start=2):
            record_id = row.get(header_map["unique_record_id"], "").strip()
            phone_raw = row.get(header_map["phone"], "").strip()

            # ── Validation ──────────────────────────────────────
            if not record_id:
                rejected.append({**row, "_reason": "missing_record_id", "_row": row_num})
                continue

            if record_id in seen_ids:
                rejected.append({**row, "_reason": "duplicate_record_id", "_row": row_num})
                continue

            if not phone_raw:
                rejected.append({**row, "_reason": "missing_phone", "_row": row_num})
                continue

            e164, is_valid = normalise_uk_phone(phone_raw)

            if not is_valid:
                rejected.append({**row, "_reason": "invalid_phone", "_row": row_num})
                continue

            # ── Deduplication on phone ──────────────────────────
            if e164 in seen_phones:
                rejected.append({**row, "_reason": "duplicate_phone", "_row": row_num})
                continue

            # ── Suppression / DNC ───────────────────────────────
            if e164 in suppressed:
                rejected.append({**row, "_reason": "suppressed_dnc", "_row": row_num})
                continue

            # ── Build record ────────────────────────────────────
            extra = {
                k: v
                for k, v in row.items()
                if k.lower().strip().replace(" ", "_")
                not in _REQUIRED_COLUMNS | _OPTIONAL_COLUMNS
            }

            candidate = CandidateRecord(
                unique_record_id=record_id,
                first_name=row.get(header_map.get("first_name", "first_name"), "").strip(),
                last_name=row.get(header_map.get("last_name", "last_name"), "").strip(),
                phone_raw=phone_raw,
                phone_e164=e164,
                email=row.get(header_map.get("email", "email"), "").strip() or None,
                extra_fields=extra,
            )

            valid.append(candidate)
            seen_phones.add(e164)
            seen_ids.add(record_id)

    log.info(
        "csv_ingestion_complete",
        valid=len(valid),
        rejected=len(rejected),
        suppressed_count=len(suppressed),
    )
    return valid, rejected
