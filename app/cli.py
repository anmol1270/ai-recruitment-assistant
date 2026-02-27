"""
CLI interface for the AI Recruitment Caller.
Provides commands for ingestion, calling, exporting, and running the server.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from app.config import get_settings
from app.logging_config import setup_logging

app = typer.Typer(
    name="recruit-caller",
    help="AI-powered outbound recruitment calling platform",
    add_completion=False,
)
console = Console()


def _run(coro):
    """Helper to run async code from sync CLI."""
    return asyncio.run(coro)


@app.command()
def ingest(
    csv_file: Path = typer.Argument(..., help="Path to input CSV file"),
):
    """Ingest a candidate CSV — validate, normalise, deduplicate."""
    settings = get_settings()
    setup_logging(settings.log_dir, json_logs=True)

    async def _do():
        from app.orchestrator import Orchestrator

        orch = Orchestrator(settings)
        await orch.start()
        try:
            stats = await orch.ingest(csv_file)
            console.print(f"\n[green]✓ Ingestion complete[/green]")
            console.print(f"  Valid records:    {stats['valid']}")
            console.print(f"  Rejected rows:    {stats['rejected']}")
            console.print(f"  Source file:      {stats['file']}")
        finally:
            await orch.stop()

    _run(_do())


@app.command()
def call(
    batch: bool = typer.Option(True, help="Run a single batch (default) vs continuous"),
    continuous: bool = typer.Option(False, help="Run continuously until all records done"),
    max_hours: float = typer.Option(12.0, help="Max runtime in hours (continuous mode)"),
):
    """Place outbound calls for pending records."""
    settings = get_settings()
    setup_logging(settings.log_dir, json_logs=True)

    async def _do():
        from app.orchestrator import Orchestrator

        orch = Orchestrator(settings)
        await orch.start()
        try:
            if continuous:
                stats = await orch.run_continuous(max_runtime_hours=max_hours)
            else:
                stats = await orch.run_calls()

            console.print(f"\n[green]✓ Calling complete[/green]")
            for k, v in stats.items():
                console.print(f"  {k}: {v}")
        finally:
            await orch.stop()

    _run(_do())


@app.command()
def export(
    include_transcript: bool = typer.Option(False, help="Include full transcripts"),
):
    """Export call results to CSV."""
    settings = get_settings()
    setup_logging(settings.log_dir, json_logs=True)

    async def _do():
        from app.orchestrator import Orchestrator

        orch = Orchestrator(settings)
        await orch.start()
        try:
            path = await orch.export_results(include_transcript=include_transcript)
            console.print(f"\n[green]✓ Results exported to:[/green] {path}")
        finally:
            await orch.stop()

    _run(_do())


@app.command()
def status():
    """Show current run status and statistics."""
    settings = get_settings()
    setup_logging(settings.log_dir, json_logs=False)

    async def _do():
        from app.orchestrator import Orchestrator

        orch = Orchestrator(settings)
        await orch.start()
        try:
            summary = await orch.get_summary()

            table = Table(title="Call Pipeline Status")
            table.add_column("Metric", style="cyan")
            table.add_column("Value", style="green")

            table.add_row("Total Records", str(summary["total_records"]))
            table.add_row("Records Called", str(summary["records_with_calls"]))
            table.add_row("Total Attempts", str(summary["total_attempts"]))

            console.print(table)

            if summary["by_status"]:
                status_table = Table(title="By Disposition")
                status_table.add_column("Status", style="cyan")
                status_table.add_column("Count", style="green")
                for s, c in sorted(summary["by_status"].items()):
                    status_table.add_row(s, str(c))
                console.print(status_table)
        finally:
            await orch.stop()

    _run(_do())


@app.command()
def run_all(
    csv_file: Path = typer.Argument(..., help="Path to input CSV file"),
    continuous: bool = typer.Option(False, help="Run continuously"),
    max_hours: float = typer.Option(12.0, help="Max runtime hours"),
    include_transcript: bool = typer.Option(False, help="Include transcripts in output"),
):
    """Full pipeline: ingest → call → export."""
    settings = get_settings()
    setup_logging(settings.log_dir, json_logs=True)

    async def _do():
        from app.orchestrator import Orchestrator

        orch = Orchestrator(settings)
        await orch.start()
        try:
            # Step 1: Ingest
            console.print("\n[bold]Step 1: Ingesting CSV...[/bold]")
            stats = await orch.ingest(csv_file)
            console.print(f"  Valid: {stats['valid']}, Rejected: {stats['rejected']}")

            # Step 2: Call
            console.print("\n[bold]Step 2: Placing calls...[/bold]")
            if continuous:
                call_stats = await orch.run_continuous(max_runtime_hours=max_hours)
            else:
                call_stats = await orch.run_calls()
            for k, v in call_stats.items():
                console.print(f"  {k}: {v}")

            # Step 3: Export
            console.print("\n[bold]Step 3: Exporting results...[/bold]")
            path = await orch.export_results(include_transcript=include_transcript)
            console.print(f"  Output: {path}")

            # Summary
            console.print("\n[bold]Summary:[/bold]")
            summary = await orch.get_summary()
            for s, c in sorted(summary.get("by_status", {}).items()):
                console.print(f"  {s}: {c}")

            console.print("\n[green]✓ Pipeline complete![/green]")
        finally:
            await orch.stop()

    _run(_do())


@app.command()
def server():
    """Run the webhook receiver server (for VAPI callbacks)."""
    settings = get_settings()
    setup_logging(settings.log_dir, json_logs=True)

    async def _do():
        import uvicorn
        from app.database import Database
        from app.webhook import create_webhook_app

        db = Database(settings.database_path)
        await db.connect()

        webhook_app = create_webhook_app(settings, db)

        config = uvicorn.Config(
            webhook_app,
            host=settings.host,
            port=settings.port,
            log_level="info",
        )
        server = uvicorn.Server(config)
        console.print(
            f"\n[green]Webhook server running on {settings.host}:{settings.port}[/green]"
        )
        await server.serve()

    _run(_do())


if __name__ == "__main__":
    app()
