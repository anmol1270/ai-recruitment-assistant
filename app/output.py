"""
Output CSV generation — produces the updated CSV with dispositions,
summaries, and all required columns for re-import.
"""

from __future__ import annotations

import csv
from datetime import datetime
from pathlib import Path
from typing import Optional

import structlog

from app.database import Database
from app.models import CallRecord

log = structlog.get_logger(__name__)

# Output columns in order
OUTPUT_COLUMNS = [
    "unique_record_id",
    "first_name",
    "last_name",
    "phone_e164",
    "job_role",
    "status",
    "short_summary",
    "last_called_at",
    "attempt_count",
    "raw_call_outcome",
    "extracted_location",
    "extracted_availability",
    "recording_url",
]


async def generate_output_csv(
    db: Database,
    output_dir: Path,
    run_id: str = "",
    include_transcript: bool = False,
) -> Path:
    """
    Generate an output CSV from all call records in the database.

    Returns the path to the generated file.
    """
    output_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    filename = f"call_results_{run_id}_{timestamp}.csv" if run_id else f"call_results_{timestamp}.csv"
    output_path = output_dir / filename

    records = await db.get_all_records()

    columns = list(OUTPUT_COLUMNS)
    if include_transcript:
        columns.append("transcript")

    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=columns)
        writer.writeheader()

        for record in records:
            row = {
                "unique_record_id": record.unique_record_id,
                "first_name": record.first_name,
                "last_name": record.last_name,
                "phone_e164": record.phone_e164,
                "job_role": record.job_role,
                "status": record.status.value,
                "short_summary": record.short_summary,
                "last_called_at": record.last_called_at.isoformat()
                if record.last_called_at
                else "",
                "attempt_count": record.attempt_count,
                "raw_call_outcome": record.raw_call_outcome,
                "extracted_location": record.extracted_location,
                "extracted_availability": record.extracted_availability,
                "recording_url": record.recording_url,
            }
            if include_transcript:
                row["transcript"] = record.transcript

            writer.writerow(row)

    log.info("output_csv_generated", path=str(output_path), records=len(records))
    return output_path


async def generate_rejected_csv(
    rejected_rows: list[dict],
    output_dir: Path,
) -> Optional[Path]:
    """Write rejected/invalid rows to a separate CSV for audit."""
    if not rejected_rows:
        return None

    output_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    output_path = output_dir / f"rejected_rows_{timestamp}.csv"

    # Gather all keys across rejected rows
    all_keys = set()
    for row in rejected_rows:
        all_keys.update(row.keys())
    columns = sorted(all_keys)

    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=columns)
        writer.writeheader()
        for row in rejected_rows:
            writer.writerow(row)

    log.info("rejected_csv_generated", path=str(output_path), rows=len(rejected_rows))
    return output_path


async def generate_run_summary(
    db: Database,
) -> dict:
    """Generate summary statistics for the current state of all records."""
    records = await db.get_all_records()

    summary = {
        "total_records": len(records),
        "by_status": {},
        "total_attempts": 0,
        "records_with_calls": 0,
    }

    for record in records:
        status = record.status.value
        summary["by_status"][status] = summary["by_status"].get(status, 0) + 1
        summary["total_attempts"] += record.attempt_count
        if record.attempt_count > 0:
            summary["records_with_calls"] += 1

    return summary
