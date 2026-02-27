# 🚀 Euro-English Deployment Guide

## UK Number (+44) Live Deploy - Quick Start

### Prerequisites
- GitHub account: https://github.com/signup
- Railway account: https://railway.app (free tier available)

### Step 1: Push to GitHub (2 minutes)

```bash
# In your project directory
cd "ai recruitment assistant"

# Use the deploy script
./deploy.sh

# Or manually:
git init
git add .
git commit -m "Initial commit"

# Create repo on GitHub first, then:
git remote add origin https://github.com/YOUR_USERNAME/ai-recruitment-caller.git
git push -u origin main
```

### Step 2: Deploy to Railway (3 minutes)

1. Go to: https://railway.app/new
2. Select **"Deploy from GitHub repo"**
3. Connect your GitHub account
4. Select your `ai-recruitment-caller` repository
5. Railway auto-detects `railway.toml` and provisions:
   - Python 3.11 environment
   - Webhook server on port 8000
   - Health checks enabled

### Step 3: Configure Environment Variables

In Railway dashboard → Variables, add:

```
VAPI_API_KEY=<your-vapi-api-key>
VAPI_PHONE_NUMBER_ID=<your-vapi-phone-number-id>
OPENAI_API_KEY=<your-openai-api-key>
WEBHOOK_BASE_URL=<Railway will provide this>
WEBHOOK_SECRET=<your-webhook-secret>
CALLING_TIMEZONE=Europe/London
CALLING_WINDOW_START=09:00
CALLING_WINDOW_END=18:00
MAX_CONCURRENT_CALLS=5
MAX_CALLS_PER_HOUR=50
MAX_CALLS_PER_DAY=200
```

**Important:** Leave `WEBHOOK_BASE_URL` blank initially — Railway provides it after first deploy.

### Step 4: Update VAPI Webhook URL

1. Copy Railway URL (e.g., `https://ai-recruitment-caller.up.railway.app`)
2. In Railway dashboard → Variables → Update `WEBHOOK_BASE_URL`
3. Railway redeploys automatically

### Step 5: Test Live (1 minute)

```bash
# Test deployment health
curl https://your-railway-url.up.railway.app/health

# Should return: {"status":"ok"}
```

### Step 6: Run First Campaign

```bash
# On your local machine
export VAPI_API_KEY=<your-vapi-api-key>
export VAPI_PHONE_NUMBER_ID=<your-vapi-phone-number-id>

# Create sample CSV
echo "unique_record_id,first_name,last_name,phone,email
TEST001,John,Smith,+447700900001,john@example.com
TEST002,Emma,Jones,+447700900002,emma@example.com" > data/input/test_candidates.csv

# Run pipeline
python -m app.cli run-all data/input/test_candidates.csv
```

### VAPI Costs (Your Responsibility)

| Item | Cost |
|------|------|
| UK Phone Number | ~£3/month |
| Voice Calls | ~£0.10-0.15/minute |
| GPT-4o-mini | Negligible (covered in VAPI pricing) |

**Your UK Number ID:** `<your-vapi-phone-number-id>`

### Troubleshooting

**Issue: Webhook not receiving**
- Check `WEBHOOK_BASE_URL` matches Railway URL exactly
- Ensure no trailing slash
- Check Railway logs: Dashboard → Deploy → Logs

**Issue: Calls not placing**
- Verify `VAPI_PHONE_NUMBER_ID` is correct UK number
- Check VAPI dashboard: https://dashboard.vapi.ai/assistants
- Ensure number has outbound calling enabled

**Issue: "Forbidden" errors**
- Check `WEBHOOK_SECRET` matches what VAPI expects

### Next Steps

Once live:
1. Test 5-10 calls to own numbers
2. Verify webhook receives data
3. Check output CSV in `data/output/`
4. Soft launch to first customer

### Support

Deployment issues? Check:
- Railway logs (primary debug source)
- VAPI dashboard logs
- Local test: `python -m app.cli server` then `curl http://localhost:8000/health`

---
**Status: Configuration Complete** ✅
**UK Number: +44** 🇬🇧
**Target: Euro-English Market** 🇪🇺
