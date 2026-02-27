# AI Recruitment Caller — MVP

**VAPI VOICE PILOT**

An outbound AI voice calling platform for recruitment screening. Ingests candidate CSVs, places calls via VAPI, runs a short AI conversation to assess job-seeking status, and outputs an updated CSV with dispositions and summaries.

---

## Architecture

```
┌──────────────┐     ┌──────────────────┐     ┌─────────────┐
│  Input CSV   │────▶│  Python Runner    │────▶│  VAPI API   │
│  (candidates)│     │  (Orchestrator)   │     │  (Voice AI)  │
└──────────────┘     │                   │     └──────┬──────┘
                     │  • Phone validate │            │
                     │  • Dedup + DNC    │     ┌──────▼──────┐
                     │  • Throttle       │     │  Phone Call  │
                     │  • Retry logic    │     │  (Candidate) │
                     └────────┬──────────┘     └──────┬──────┘
                              │                       │
                     ┌────────▼──────────┐     ┌──────▼──────┐
                     │  SQLite DB        │◀────│  Webhook     │
                     │  (state + logs)   │     │  (FastAPI)   │
                     └────────┬──────────┘     └─────────────┘
                              │
                     ┌────────▼──────────┐
                     │  Output CSV       │
                     │  (dispositions)   │
                     └──────────────────┘
```

### Components

| Component | Technology | Purpose |
|-----------|-----------|---------|
| **Batch Runner** | Python + asyncio | Orchestrates CSV→calls→output pipeline |
| **Webhook Server** | FastAPI + uvicorn | Receives VAPI end-of-call reports |
| **Voice Agent** | VAPI + GPT-4o-mini + ElevenLabs | AI-powered screening calls |
| **Database** | SQLite (aiosqlite) | State, idempotency, audit logs |
| **Phone Validation** | `phonenumbers` lib | UK E.164 normalisation |
| **Tunnel** | Cloudflared (dev) | Expose webhooks to VAPI |

---

## Quick Start

### 1. Prerequisites

- Python 3.10+
- A [VAPI account](https://vapi.ai) with:
  - API key
  - An outbound phone number (UK number recommended)
- OpenAI API key (configured in VAPI dashboard)

### 2. Install

```bash
cd "ai recruitment assistant"
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

### 3. Configure

```bash
cp .env.example .env
# Edit .env with your VAPI credentials
```

Key settings to fill in:
- `VAPI_API_KEY` — from your VAPI dashboard
- `VAPI_PHONE_NUMBER_ID` — the outbound phone number ID in VAPI
- `WEBHOOK_BASE_URL` — public URL where VAPI can reach your webhook server

### 4. Start the Webhook Server

The webhook server must be running to receive call results from VAPI.

**Option A: Local development with tunnel**
```bash
# Terminal 1: Start webhook server
python -m app.cli server

# Terminal 2: Expose via cloudflared (free)
cloudflared tunnel --url http://localhost:8000
# Copy the tunnel URL and set it as WEBHOOK_BASE_URL in .env
```

**Option B: Docker**
```bash
docker-compose up -d
# The tunnel service auto-creates a temporary URL — check logs:
docker-compose logs tunnel
```

### 5. Run the Pipeline

**Full pipeline (ingest → call → export):**
```bash
python -m app.cli run-all data/input/sample_candidates.csv
```

**Step by step:**
```bash
# 1. Ingest CSV
python -m app.cli ingest data/input/sample_candidates.csv

# 2. Place calls (single batch)
python -m app.cli call

# 3. Place calls (continuous until all done)
python -m app.cli call --continuous --max-hours 12

# 4. Export results
python -m app.cli export

# 5. Check status
python -m app.cli status
```

---

## Input CSV Format

Required columns:

| Column | Description |
|--------|-------------|
| `unique_record_id` | Unique identifier for the candidate |
| `phone` | UK phone number (any format) |

Optional columns:

| Column | Description |
|--------|-------------|
| `first_name` | Candidate's first name (used in greeting) |
| `last_name` | Candidate's last name |
| `email` | Email address |
| *any others* | Preserved in `extra_fields` |

Example:
```csv
unique_record_id,first_name,last_name,phone,email
REC001,James,Smith,07700900001,james@example.com
REC002,Emma,Johnson,+447700900002,emma@example.com
```

---

## Output CSV Format

| Column | Description |
|--------|-------------|
| `unique_record_id` | Same as input |
| `phone_e164` | Normalised E.164 phone number |
| `status` | Disposition (see below) |
| `short_summary` | 1-2 sentence call summary |
| `last_called_at` | ISO timestamp of last call |
| `attempt_count` | Number of call attempts |
| `raw_call_outcome` | VAPI's ended reason |
| `extracted_location` | Location mentioned (if any) |
| `extracted_availability` | Availability mentioned (if any) |
| `recording_url` | Link to call recording (if enabled) |

---

## Dispositions

| Status | Meaning |
|--------|---------|
| `ACTIVE_LOOKING` | Candidate is actively seeking / open to opportunities |
| `NOT_LOOKING` | Candidate is not interested |
| `CALL_BACK` | Candidate asked to be called back |
| `NO_ANSWER` | No answer (may retry) |
| `WRONG_NUMBER` | Wrong person / number |
| `DNC` | Do Not Call — candidate requested removal |
| `VOICEMAIL` | Reached voicemail |
| `BUSY` | Line busy |
| `FAILED` | Technical failure |
| `PENDING` | Not yet called |

---

## How It Works

### Calling Windows
- Calls are only placed within UK business hours: **09:00–20:00 London time**
- Outside this window, the system sleeps and waits

### Throttling
- **Max concurrent calls:** 5 (configurable)
- **Max calls/hour:** 50
- **Max calls/day:** 200
- For 1,000 records over a week: ~143/day average is well within limits

### Retry Policy
- Records with `NO_ANSWER`, `BUSY`, or `FAILED` are retried
- **Max 2 retries** (3 total attempts)
- **60-minute delay** between retries
- Records with terminal dispositions (`ACTIVE_LOOKING`, `NOT_LOOKING`, `DNC`, `WRONG_NUMBER`) are never retried

### Idempotency
- Each `unique_record_id` can only exist once in the database
- Re-ingesting the same CSV will update (not duplicate) records
- The system checks record status before placing each call
- VAPI call IDs are stored to prevent double-processing of webhooks

### Suppression / DNC
- Place a CSV at `data/suppression_list.csv` with a `phone` column
- Any matching numbers will be excluded during ingestion
- If a candidate says "remove me" during the call, they get `DNC` status

---

## AI Voice Agent

The VAPI assistant uses:
- **LLM:** GPT-4o-mini (fast, cost-effective)
- **Voice:** ElevenLabs (natural, conversational)
- **Max call duration:** 3 minutes
- **Silence timeout:** 15 seconds

### Conversation Flow
1. Greet by name, introduce as recruitment team AI assistant
2. Ask the key question: *"Are you currently open to new opportunities?"*
3. Brief follow-up if actively looking (role type, location)
4. Thank and end

### Post-Call Analysis
VAPI automatically analyses the transcript and extracts:
- Disposition (structured enum)
- Summary (1-2 sentences)
- Location preference
- Availability timeline

---

## Testing

```bash
pytest tests/ -v
```

---

## Deployment (Production)

### VPS / Cloud

1. Provision a small VPS (1 vCPU, 1GB RAM is sufficient)
2. Install Docker + Docker Compose
3. Clone the repo, configure `.env`
4. Point a domain to the VPS, set up HTTPS (Caddy/nginx)
5. Update `WEBHOOK_BASE_URL` to your domain
6. Start: `docker-compose up -d`
7. Set up a cron job or systemd timer for the calling batch:

```bash
# Run calls every 15 minutes during business hours
*/15 9-19 * * 1-5 cd /path/to/project && docker exec recruit-webhook python -m app.cli call
```

### Monitoring

- Health check: `GET /health`
- Logs: `data/logs/app.jsonl` (structured JSON)
- Status: `python -m app.cli status`
- SQLite DB: `data/calls.db` (can be inspected with any SQLite client)

---

## Project Structure

```
ai recruitment assistant/
├── app/
│   ├── __init__.py
│   ├── cli.py              # Typer CLI commands
│   ├── config.py            # Pydantic settings
│   ├── csv_pipeline.py      # CSV ingestion + validation
│   ├── database.py          # SQLite persistence
│   ├── logging_config.py    # Structured logging
│   ├── models.py            # Data models + enums
│   ├── orchestrator.py      # Main pipeline controller
│   ├── output.py            # Output CSV generation
│   ├── phone_utils.py       # UK phone normalisation
│   ├── scheduler.py         # Throttling + calling windows
│   ├── server.py            # FastAPI server entry point
│   ├── vapi_client.py       # VAPI API integration
│   └── webhook.py           # Webhook receiver
├── data/
│   ├── input/               # Input CSVs
│   ├── output/              # Generated result CSVs
│   ├── logs/                # Log files
│   └── suppression_list.csv # DNC list
├── tests/
│   ├── test_csv_pipeline.py
│   ├── test_database.py
│   ├── test_phone_utils.py
│   └── test_webhook.py
├── .env.example
├── .gitignore
├── docker-compose.yml
├── Dockerfile
├── pyproject.toml
└── README.md
```
