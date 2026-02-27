"""Tests for CSV ingestion pipeline."""

import csv
import tempfile
from pathlib import Path

import pytest
from app.csv_pipeline import ingest_csv


def _write_csv(rows: list[dict], path: Path) -> None:
    """Helper – write rows to a CSV file."""
    if not rows:
        return
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)


class TestIngestion:
    def test_valid_records(self, tmp_path):
        csv_file = tmp_path / "candidates.csv"
        _write_csv(
            [
                {"unique_record_id": "R1", "phone": "07700900001", "first_name": "Alice"},
                {"unique_record_id": "R2", "phone": "07700900002", "first_name": "Bob"},
            ],
            csv_file,
        )
        valid, rejected = ingest_csv(csv_file)
        assert len(valid) == 2
        assert len(rejected) == 0
        assert valid[0].phone_e164 == "+447700900001"

    def test_dedup_phone(self, tmp_path):
        csv_file = tmp_path / "candidates.csv"
        _write_csv(
            [
                {"unique_record_id": "R1", "phone": "07700900001"},
                {"unique_record_id": "R2", "phone": "07700900001"},  # duplicate phone
            ],
            csv_file,
        )
        valid, rejected = ingest_csv(csv_file)
        assert len(valid) == 1
        assert len(rejected) == 1
        assert rejected[0]["_reason"] == "duplicate_phone"

    def test_dedup_record_id(self, tmp_path):
        csv_file = tmp_path / "candidates.csv"
        _write_csv(
            [
                {"unique_record_id": "R1", "phone": "07700900001"},
                {"unique_record_id": "R1", "phone": "07700900002"},  # duplicate ID
            ],
            csv_file,
        )
        valid, rejected = ingest_csv(csv_file)
        assert len(valid) == 1
        assert rejected[0]["_reason"] == "duplicate_record_id"

    def test_invalid_phone(self, tmp_path):
        csv_file = tmp_path / "candidates.csv"
        _write_csv(
            [
                {"unique_record_id": "R1", "phone": "invalid"},
            ],
            csv_file,
        )
        valid, rejected = ingest_csv(csv_file)
        assert len(valid) == 0
        assert rejected[0]["_reason"] == "invalid_phone"

    def test_missing_phone(self, tmp_path):
        csv_file = tmp_path / "candidates.csv"
        _write_csv(
            [
                {"unique_record_id": "R1", "phone": ""},
            ],
            csv_file,
        )
        valid, rejected = ingest_csv(csv_file)
        assert len(valid) == 0
        assert rejected[0]["_reason"] == "missing_phone"

    def test_missing_record_id(self, tmp_path):
        csv_file = tmp_path / "candidates.csv"
        _write_csv(
            [
                {"unique_record_id": "", "phone": "07700900001"},
            ],
            csv_file,
        )
        valid, rejected = ingest_csv(csv_file)
        assert len(valid) == 0
        assert rejected[0]["_reason"] == "missing_record_id"

    def test_suppression_list(self, tmp_path):
        csv_file = tmp_path / "candidates.csv"
        suppression_file = tmp_path / "dnc.csv"

        _write_csv(
            [
                {"unique_record_id": "R1", "phone": "07700900001"},
                {"unique_record_id": "R2", "phone": "07700900002"},
            ],
            csv_file,
        )
        _write_csv(
            [{"phone": "07700900001"}],
            suppression_file,
        )

        valid, rejected = ingest_csv(csv_file, suppression_file)
        assert len(valid) == 1
        assert valid[0].unique_record_id == "R2"
        assert rejected[0]["_reason"] == "suppressed_dnc"

    def test_missing_required_column(self, tmp_path):
        csv_file = tmp_path / "candidates.csv"
        _write_csv(
            [{"name": "Alice", "phone": "07700900001"}],  # missing unique_record_id
            csv_file,
        )
        with pytest.raises(ValueError, match="missing required column"):
            ingest_csv(csv_file)
