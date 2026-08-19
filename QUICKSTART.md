# Quick Start Guide

## 5-Minute Setup

### 1. Clone and Install

```bash
git clone https://github.com/phienterprisesptyltd-ship-it/hostinger-email-generation-.git
cd hostinger-email-generation-

python -m venv venv
source venv/bin/activate  # or: venv\Scripts\activate on Windows

pip install -r requirements.txt
```

### 2. Configure

```bash
cp .env.example .env
```

Edit `.env` with:
- `HOSTINGER_API_TOKEN` - From Hostinger Mail API
- `HOSTINGER_MAILBOX_RESOURCE_ID` - Your mailbox ID
- `HOSTINGER_SENDER_ADDRESS` - Your business email
- `ANTHROPIC_API_KEY` - From Anthropic
- `DATABASE_URL` - PostgreSQL connection string

### 3. Initialize Database

```bash
python setup.py
```

### 4. Run Application

```bash
python -m uvicorn main:app --reload --port 8000
```

Visit: http://localhost:8000

## Test the System

### Create a Test Lead

```bash
curl -X POST http://localhost:8000/api/leads \
  -H "Content-Type: application/json" \
  -d '{
    "email": "customer@example.com",
    "full_name": "Test Customer",
    "lead_source": "test"
  }'
```

### Simulate Incoming Email

```bash
curl -X POST http://localhost:8000/api/webhooks/hostinger/message \
  -H "Content-Type: application/json" \
  -d '{
    "event": "message.received",
    "message_id": "msg-123",
    "uid": 12345,
    "from_address": "customer@example.com",
    "to_address": "info@synchrobuild.com.au",
    "subject": "Interested in your service",
    "body": "Hi, I would like to know more about your services.",
    "folder": "INBOX"
  }'
```

### Check Admin Dashboard

```bash
curl http://localhost:8000/api/admin/dashboard | jq
```

### Get Pending Approvals

```bash
curl http://localhost:8000/api/admin/leads/pending-approval | jq
```

### Get Lead Details

Replace `{lead_id}` with the lead ID from previous response:

```bash
curl http://localhost:8000/api/admin/leads/{lead_id}/summary | jq
```

### Approve an Email Draft

Replace `{draft_id}` with the draft ID:

```bash
curl -X POST http://localhost:8000/api/ai/drafts/{draft_id}/approve | jq
```

## Safe Test Mode Features

In the default **Safe Test Mode**:

✅ **What Works**
- Read incoming emails
- Create leads and messages
- AI generates decisions
- Email drafts created
- All logged and auditable

❌ **What's Blocked**
- Emails NOT sent to real leads
- Only test emails go out (if configured)
- No autonomous sending

## Switching to Autonomous Mode

⚠️ **Only after testing thoroughly**

1. Edit `.env`:
   ```env
   AUTONOMOUS_MODE=true
   ```

2. Restart the application

3. Now emails will send automatically (with AI approval if required)

## Key Configuration Options

### Business Timezone
```env
BUSINESS_TIMEZONE=Australia/Sydney
```

### Daily Follow-up Time
```env
DAILY_FOLLOWUP_HOUR=8
```
Runs at 8:00 AM in the specified timezone.

### Safety Limits
```env
MAX_AUTOMATED_FOLLOWUPS=5         # Max emails per lead
MIN_FOLLOWUP_HOURS=24              # Min hours between messages
APPROVED_SENDING_HOURS_START=8     # Start of sending window
APPROVED_SENDING_HOURS_END=18      # End of sending window
```

### AI Model
```env
# Configurable in ai_service.py
# Currently using Claude Opus (latest model)
# Can be changed to Claude Sonnet for speed/cost
```

## Common Tasks

### Create Lead from Facebook Lead Ad

```python
# In Python
from lead_service import LeadService
from schemas import LeadCreate

lead_data = LeadCreate(
    email="customer@example.com",
    full_name="John Doe",
    phone="+61234567890",
    lead_source="facebook",
    facebook_lead_id="facebook-lead-123",
    facebook_campaign_id="campaign-456",
    facebook_ad_id="ad-789",
)

lead = LeadService.create_lead(db, lead_data)
```

### Mark Lead as Do-Not-Contact

```bash
curl -X POST http://localhost:8000/api/leads/{lead_id}/mark-unsubscribed
```

### Get Full Conversation History

```bash
curl http://localhost:8000/api/leads/{lead_id}/conversation | jq
```

### Force AI Evaluation

```bash
curl -X POST http://localhost:8000/api/ai/evaluate/{lead_id} | jq
```

### List All Leads by Status

```bash
curl "http://localhost:8000/api/leads?status=interested" | jq
```

## Troubleshooting

### Database connection refused
- Check `DATABASE_URL` in `.env`
- Ensure PostgreSQL is running
- Try: `psql <connection-string>`

### Hostinger API errors
- Verify `HOSTINGER_API_TOKEN` is correct
- Check mailbox is active on Hostinger
- Verify sender email address

### AI service not responding
- Check `ANTHROPIC_API_KEY` is valid
- Verify API quota and rate limits
- Check application logs

### Emails not sending in autonomous mode
- Verify `AUTONOMOUS_MODE=true` in `.env`
- Check `APPROVED_SENDING_HOURS`
- Review lead's `ai_automation_enabled` flag
- Check if draft requires approval

### Scheduler not running
```bash
# Restart application
# Logs should show:
# "Scheduler started. Daily follow-up at 8:00 Australia/Sydney"
```

## Production Checklist

Before going live:

- [ ] Set `ENVIRONMENT=production`
- [ ] Set `DEBUG=false`
- [ ] Configure proper `DATABASE_URL`
- [ ] Set `AUTONOMOUS_MODE=true` (only after testing)
- [ ] Verify all API keys and tokens
- [ ] Set up database backups
- [ ] Configure error logging/monitoring
- [ ] Set up HTTPS/SSL
- [ ] Configure CORS properly
- [ ] Load test the system
- [ ] Monitor first week of automated sending

## Documentation

See [README.md](README.md) for:
- Full architecture overview
- Complete API reference
- Database schema details
- Security considerations
- Deployment options
- Facebook Lead Ads setup

## Support

- Check logs: `docker logs <container-id>`
- Verify configuration: Review `.env`
- Test endpoints: Use curl or Postman
- Database check: `psql <connection-string>`
