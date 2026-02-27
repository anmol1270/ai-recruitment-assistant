"""Tests for webhook handler."""

import json
import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport
from pathlib import Path

from app.config import Settings
from app.database import Database
from app.models import CallRecord, Disposition
from app.webhook import create_webhook_app


@pytest_asyncio.fixture
async def db(tmp_path):
    database = Database(tmp_path / "test.db")
    await database.connect()
    yield database
    await database.close()


@pytest_asyncio.fixture
async def client(db):
    settings = Settings(
        vapi_api_key="test",
        vapi_phone_number_id="test",
        webhook_secret="",
    )
    app = create_webhook_app(settings, db)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.mark.asyncio
async def test_health(client):
    resp = await client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


@pytest.mark.asyncio
async def test_end_of_call_report(client, db):
    # Set up a test record
    record = CallRecord(
        unique_record_id="R001",
        phone_e164="+447700900001",
        status=Disposition.PENDING,
    )
    await db.upsert_candidate(record)
    await db.mark_call_started("R001", "call_abc123")

    # Simulate VAPI end-of-call webhook
    payload = {
        "message": {
            "type": "end-of-call-report",
            "call": {
                "id": "call_abc123",
                "endedReason": "customer-ended-call",
                "metadata": {"unique_record_id": "R001"},
            },
            "transcript": "Hi James, are you looking for new opportunities? Yes I am!",
            "analysis": {
                "structuredData": {
                    "disposition": "ACTIVE_LOOKING",
                    "summary": "Candidate is actively looking for roles.",
                    "location": "Manchester",
                    "availability": "2 weeks notice",
                },
                "summary": "Candidate is actively looking for roles.",
            },
        }
    }

    resp = await client.post("/webhook/vapi", json=payload)
    assert resp.status_code == 200

    # Verify the record was updated
    updated = await db.get_record_by_call_id("call_abc123")
    assert updated.status == Disposition.ACTIVE_LOOKING
    assert "actively looking" in updated.short_summary.lower()
    assert updated.extracted_location == "Manchester"


@pytest.mark.asyncio
async def test_no_answer_webhook(client, db):
    record = CallRecord(
        unique_record_id="R002",
        phone_e164="+447700900002",
        status=Disposition.PENDING,
    )
    await db.upsert_candidate(record)
    await db.mark_call_started("R002", "call_xyz789")

    payload = {
        "message": {
            "type": "end-of-call-report",
            "call": {
                "id": "call_xyz789",
                "endedReason": "customer-did-not-answer",
                "metadata": {"unique_record_id": "R002"},
            },
        }
    }

    resp = await client.post("/webhook/vapi", json=payload)
    assert resp.status_code == 200

    updated = await db.get_record_by_call_id("call_xyz789")
    assert updated.status == Disposition.NO_ANSWER
