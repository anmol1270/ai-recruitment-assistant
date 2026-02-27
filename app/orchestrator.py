"""
Main orchestrator — ties together CSV ingestion, VAPI calls, scheduling,
and output generation. Can run as a one-shot batch or continuous daemon.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime
from pathlib import Path

import structlog

from app.config import Settings
from app.csv_pipeline import ingest_csv
from app.database import Database
from app.models import CallRecord, Disposition
from app.output import generate_output_csv, generate_rejected_csv, generate_run_summary
from app.scheduler import CallScheduler
from app.vapi_client import VAPIClient

log = structlog.get_logger(__name__)


class Orchestrator:
    """
    Top-level controller for the recruitment calling pipeline.

    Usage:
        orch = Orchestrator(settings)
        await orch.start()
        await orch.ingest("data/input/candidates.csv")
        await orch.run_calls()          # one batch
        await orch.run_continuous()      # or: loop until all done
        await orch.export_results()
        await orch.stop()
    """

    def __init__(self, settings: Settings):
        self.settings = settings
        self.db = Database(settings.database_path)
        self.vapi = VAPIClient(settings)
        self.scheduler: CallScheduler | None = None
        self._assistant_id: str = ""
        self._run_id: str = ""

    async def start(self) -> None:
        """Initialise database connection and VAPI assistant."""
        self.settings.ensure_dirs()
        await self.db.connect()
        self._assistant_id = await self.vapi.get_or_create_assistant()
        self._run_id = uuid.uuid4().hex[:12]
        self.scheduler = CallScheduler(
            self.settings, self.db, self.vapi, self._run_id
        )
        log.info(
            "orchestrator_started",
            run_id=self._run_id,
            assistant_id=self._assistant_id,
        )

    async def stop(self) -> None:
        """Clean up resources."""
        await self.vapi.close()
        await self.db.close()
        log.info("orchestrator_stopped", run_id=self._run_id)

    async def ingest(self, csv_path: str | Path) -> dict:
        """
        Ingest a CSV file: validate, normalise, deduplicate, suppress.
        Returns stats dict.
        """
        csv_path = Path(csv_path)
        suppression_path = (
            self.settings.suppression_list_path
            if self.settings.suppression_list_path.exists()
            else None
        )

        valid, rejected = ingest_csv(csv_path, suppression_path)

        # Persist valid candidates
        for candidate in valid:
            record = CallRecord(
                unique_record_id=candidate.unique_record_id,
                phone_e164=candidate.phone_e164,
                status=Disposition.PENDING,
            )
            await self.db.upsert_candidate(record)

        # Write rejected rows for audit
        if rejected:
            await generate_rejected_csv(
                rejected, self.settings.output_csv_dir
            )

        await self.db.log_run_event(
            run_id=self._run_id,
            unique_record_id="__system__",
            action="csv_ingested",
            detail=f"valid={len(valid)} rejected={len(rejected)} file={csv_path.name}",
        )

        stats = {
            "valid": len(valid),
            "rejected": len(rejected),
            "file": str(csv_path),
        }
        log.info("ingestion_complete", **stats)
        return stats

    async def run_calls(self) -> dict:
        """Run a single batch of calls (respecting all constraints)."""
        if not self.scheduler:
            raise RuntimeError("Call start() first")

        # Wait for calling window if outside
        if not self.scheduler._in_calling_window():
            log.info("outside_calling_window")
            return {"placed": 0, "note": "Outside UK calling window"}

        return await self.scheduler.run_batch(self._assistant_id)

    async def run_continuous(
        self,
        poll_interval_seconds: int = 60,
        max_runtime_hours: float = 12,
    ) -> dict:
        """
        Continuously place calls until all records are processed or
        max runtime is reached. Respects calling windows by sleeping.
        """
        if not self.scheduler:
            raise RuntimeError("Call start() first")

        start_time = datetime.utcnow()
        total_stats = {"placed": 0, "skipped_window": 0, "skipped_throttle": 0, "errors": 0}
        batch_count = 0

        while True:
            # Check runtime limit
            elapsed = (datetime.utcnow() - start_time).total_seconds() / 3600
            if elapsed >= max_runtime_hours:
                log.info("max_runtime_reached", hours=elapsed)
                break

            # Wait for calling window
            if not self.scheduler._in_calling_window():
                await self.scheduler.wait_for_calling_window()
                continue

            # Check if there are any pending records
            pending = await self.db.get_pending_records(limit=1)
            if not pending:
                log.info("all_records_processed")
                break

            # Run a batch
            batch_stats = await self.scheduler.run_batch(self._assistant_id)
            batch_count += 1

            for key in total_stats:
                total_stats[key] += batch_stats.get(key, 0)

            # If no calls were placed this batch, wait before retrying
            if batch_stats.get("placed", 0) == 0:
                log.info("no_calls_placed_waiting", interval=poll_interval_seconds)
                await asyncio.sleep(poll_interval_seconds)
            else:
                # Brief pause between batches
                await asyncio.sleep(10)

        total_stats["batches"] = batch_count
        log.info("continuous_run_complete", **total_stats)
        return total_stats

    async def export_results(self, include_transcript: bool = False) -> Path:
        """Generate the output CSV with all call results."""
        path = await generate_output_csv(
            self.db,
            self.settings.output_csv_dir,
            self._run_id,
            include_transcript=include_transcript,
        )
        return path

    async def get_summary(self) -> dict:
        """Get current run summary statistics."""
        return await generate_run_summary(self.db)
