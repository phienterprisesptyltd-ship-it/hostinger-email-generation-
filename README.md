# Synchrobuild Autonomous Lead Follow-up System

A production-ready autonomous lead management and follow-up system with AI-powered decision engine, Hostinger email integration, and comprehensive safety guardrails.

## Features

### ✅ Stage 1: Email Reply Handling
- Detect incoming email replies via Hostinger webhooks
- Identify which lead/conversation the reply belongs to
- Retrieve full conversation history
- Store messages with full thread tracking
- Avoid duplicate processing with webhook deduplication
- Automatic unsubscribe detection

### ✅ Stage 2: Lead Conversation State
Complete lead model with:
- Full name, email, phone
- Lead source and Facebook campaign metadata
- Current status (new, contacted, replied, interested, qualified, follow_up, not_interested, do_not_contact, needs_human, closed)
- Conversation history with full email threads
- Last contact dates (inbound/outbound)
- Next follow-up scheduling
- Follow-up count tracking
- AI-generated summaries
- Internal notes
- Automation control flags

### ✅ Stage 3: AI Reply Decision Engine
Claude Opus AI agent that:
- Analyzes lead details and conversation history
- Makes structured decisions: SEND_REPLY, SEND_FOLLOWUP, WAIT, MARK_INTERESTED, MARK_NOT_INTERESTED, MARK_DO_NOT_CONTACT, ESCALATE_TO_HUMAN, CLOSE_LEAD
- Generates professional email drafts
- Provides confidence scores and reasoning
- Returns structured validated output

### ✅ Stage 4: Safety & Business Guardrails
- Never email do_not_contact leads
- Automatic unsubscribe detection and enforcement
- Duplicate email prevention
- Maximum follow-up limit enforcement (configurable)
- Minimum delay between follow-ups (configurable)
- Sending hour restrictions (configurable)
- Escalation for escalation keywords (refund, complaint, lawsuit, etc.)
- Prompt injection prevention (treat email content as untrusted)
- Complete audit logging

### ✅ Stage 5: Scheduled 8:00 AM Follow-up Worker
Background job that:
- Runs daily at configured time in business timezone
- Finds leads eligible for follow-up
- Loads conversation context
- Invokes AI decision engine
- Validates against guardrails
- Creates drafts or sends (based on mode)
- Updates lead status and next follow-up date
- Safe to run multiple times (idempotent)

### ✅ Stage 6: Incoming Reply Automation
When customer replies:
1. Receive/detect email
2. Match to correct lead
3. Store message
4. Update conversation
5. Run AI decision engine
6. Generate draft or send (based on mode)
7. Update lead status

### 🔄 Stage 7: Facebook Lead Ads (Prepared)
Architecture prepared for Facebook integration:
- Models include facebook_lead_id, campaign_id, ad_id, form_id
- Webhook handler can process Facebook payloads
- Requires: Facebook app credentials, Page access token, webhook verification

### ✅ Stage 8: Admin Dashboard
API endpoints for:
- All active leads with status
- Last contact info and next follow-up
- Full email conversations
- AI summaries and proposed actions
- Pending approvals
- Automation controls
- Lead source and campaign info

Controls:
- Approve and Send
- Edit and Send
- Reject
- Pause/Resume AI
- Mark Do Not Contact
- Escalate to human

### ✅ Stage 9: Configuration Management
Configurable via .env:
- Business timezone
- Daily follow-up time
- Maximum automated follow-ups
- Minimum delay between follow-ups
- Autonomous vs. approval mode
- Approved sending hours
- AI model selection
- Mailbox configuration
- Sender settings

### ✅ Stage 10: Testing (Ready)
Test endpoints and modes for:
- New lead handling
- Reply matching
- Duplicate handling
- Unsubscribe detection
- Do-not-contact enforcement
- Follow-up limits
- Sending hour restrictions
- Customer reply timing
- Prompt injection prevention

## Architecture

```
┌─────────────────────────────────────────────────────┐
│         Incoming Email (Hostinger Webhook)          │
└────────────────────┬────────────────────────────────┘
                     │
                     ▼
        ┌────────────────────────────┐
        │   Webhook Handler API      │
        │  - Deduplication           │
        │  - Lead Matching           │
        │  - Message Storage         │
        └────────────┬───────────────┘
                     │
                     ▼
        ┌────────────────────────────┐
        │   AI Decision Engine       │
        │  (Claude Opus)             │
        │  - Conversation Analysis   │
        │  - Action Decision         │
        │  - Email Generation        │
        └────────────┬───────────────┘
                     │
                     ▼
        ┌────────────────────────────┐
        │   Safety Guardrails        │
        │  - Do-Not-Contact Check    │
        │  - Duplicate Prevention    │
        │  - Hour Restrictions       │
        │  - Escalation Detection    │
        └────────────┬───────────────┘
                     │
          ┌──────────┴──────────┐
          │                     │
          ▼                     ▼
    ┌──────────────┐    ┌──────────────────┐
    │Send Email    │    │Queue for Approval│
    │(Hostinger)   │    │(Safe Test Mode)  │
    └──────────────┘    └──────────────────┘
```

## Installation

### Requirements
- Python 3.9+
- PostgreSQL 12+
- Anthropic API key
- Hostinger Mail API token

### Setup

```bash
# Clone repository
git clone <repo-url>
cd hostinger-email-generation-

# Create virtual environment
python -m venv venv
source venv/bin/activate  # or: venv\Scripts\activate on Windows

# Install dependencies
pip install -r requirements.txt

# Copy and configure environment
cp .env.example .env
# Edit .env with your credentials and configuration

# Initialize database
python setup.py

# Run application
python -m uvicorn main:app --reload
```

## Configuration

### .env File

```env
# Database
DATABASE_URL=postgresql://user:password@localhost:5432/synchrobuild

# Hostinger Email API
HOSTINGER_API_TOKEN=your_token_here
HOSTINGER_MAILBOX_RESOURCE_ID=your_mailbox_id_here
HOSTINGER_SENDER_ADDRESS=info@synchrobuild.com.au

# Anthropic Claude API
ANTHROPIC_API_KEY=your_anthropic_key_here

# Application
ENVIRONMENT=development  # or: production
AUTONOMOUS_MODE=false    # Start in safe test mode
DEBUG=true

# Business Configuration
BUSINESS_TIMEZONE=Australia/Sydney
DAILY_FOLLOWUP_HOUR=8
MAX_AUTOMATED_FOLLOWUPS=5
MIN_FOLLOWUP_HOURS=24
APPROVED_SENDING_HOURS_START=8
APPROVED_SENDING_HOURS_END=18

# Testing
ENABLE_TEST_MODE=true
TEST_EMAIL_RECIPIENT=test@example.com
```

## API Endpoints

### Leads Management
```
POST   /api/leads              # Create lead
GET    /api/leads/{id}         # Get lead
PUT    /api/leads/{id}         # Update lead
GET    /api/leads              # List leads
POST   /api/leads/{id}/mark-unsubscribed  # Mark do-not-contact
GET    /api/leads/{id}/conversation       # Get full conversation
```

### AI Decisions
```
GET    /api/ai/drafts          # List pending approvals
POST   /api/ai/drafts/{id}/approve    # Approve draft
POST   /api/ai/drafts/{id}/reject     # Reject draft
POST   /api/ai/evaluate/{lead_id}     # Get AI evaluation
```

### Admin Dashboard
```
GET    /api/admin/dashboard              # Dashboard summary
GET    /api/admin/leads/pending-approval # Leads awaiting approval
GET    /api/admin/leads/{id}/summary     # Lead details
POST   /api/admin/automation/toggle      # Enable/disable automation
GET    /api/admin/stats                  # System statistics
```

### Webhooks
```
POST   /api/webhooks/hostinger/message   # Hostinger email webhook
POST   /api/webhooks/test                # Test webhook
GET    /api/webhooks/status              # Webhook statistics
```

## Operation

### Safe Test Mode (Default)

In safe test mode:
- ✅ Reads incoming real mail
- ✅ Creates database records
- ✅ Generates AI decisions
- ✅ Creates email drafts
- ❌ Does NOT send to real leads
- ✅ Sends to configured TEST_EMAIL_RECIPIENT only

### Autonomous Production Mode

To enable autonomous sending:

1. Edit `.env` and set `AUTONOMOUS_MODE=true`
2. Restart the application
3. The system will now send emails automatically

### Admin Controls

The admin API provides fine-grained control:

```bash
# Toggle global automation
curl -X POST http://localhost:8000/api/admin/automation/toggle?enabled=true

# Approve a specific draft
curl -X POST http://localhost:8000/api/ai/drafts/123/approve

# Reject a draft
curl -X POST http://localhost:8000/api/ai/drafts/123/reject

# Get pending approvals
curl http://localhost:8000/api/admin/leads/pending-approval

# View lead details
curl http://localhost:8000/api/admin/leads/456/summary
```

## Database Schema

### Key Tables

**leads**
- Stores lead information
- Tracks status and conversation state
- Holds automation preferences
- Records follow-up schedule

**messages**
- Stores inbound/outbound emails
- Full message body and metadata
- AI sentiment analysis
- Processing state

**ai_drafts**
- AI-generated email drafts
- Pending approvals
- Action decisions
- Reasoning and confidence

**webhook_logs**
- Incoming webhooks for deduplication
- Processing status
- Error tracking

**scheduled_tasks**
- Audit log for scheduled jobs
- Execution status
- Results and errors

## Safety Features

### Hard Guardrails (Cannot be overridden)

1. **Do-Not-Contact Enforcement**: System refuses to email leads marked do_not_contact
2. **Unsubscribe Detection**: Automatic detection and enforcement
3. **Duplicate Prevention**: Message deduplication by external ID
4. **Follow-up Limits**: Configurable maximum (default: 5)
5. **Timing Restrictions**: Minimum hours between messages, sending hours
6. **Escalation Triggers**: Automatic human escalation for risky content
7. **Audit Logging**: All actions logged and traceable
8. **Input Validation**: Email content treated as untrusted input

### AI Safeguards

- Claude explicitly instructed not to invent data
- Structured output validation
- Confidence scoring
- Human approval recommendations for uncertain decisions
- Prompt injection prevention

## Monitoring

### Key Metrics

- Leads by status
- Pending approvals
- Messages received (weekly/monthly)
- AI decisions made
- Follow-up success rate
- Escalation rate

### Logs

Check application logs for:
```bash
docker logs <container-id>
# or
tail -f logs/app.log
```

## Troubleshooting

### "Lead not found" when processing webhook
- Check that lead matching by email works
- Verify sender email addresses match

### AI drafts not generated
- Check ANTHROPIC_API_KEY is set
- Verify API quota and rate limits
- Check application logs

### Emails not sending
- Verify HOSTINGER_API_TOKEN
- Check HOSTINGER_MAILBOX_RESOURCE_ID
- Verify sender email is configured on Hostinger
- Check APPROVED_SENDING_HOURS

### Scheduler not running
- Restart application
- Check APScheduler logs
- Verify BUSINESS_TIMEZONE is valid

## Facebook Lead Ads Integration (Future)

To enable Facebook lead integration:

1. **Set up Meta App**
   - Create app at developers.facebook.com
   - Configure webhooks for lead generation

2. **Collect Credentials**
   - App ID
   - App Secret
   - Page Access Token
   - Business Account ID

3. **Configuration**
   - Add to .env
   - Update webhook handler for Facebook payloads
   - Set up webhook verification

4. **Flow**
   - Facebook Lead Ad → API Endpoint
   - Create/update lead with Facebook metadata
   - Deduplicate by email/phone/external_lead_id
   - Trigger initial contact sequence
   - Track campaign performance

## Deployment

### Docker

```dockerfile
FROM python:3.11-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["python", "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
```

### Environment Variables (Production)

```bash
DATABASE_URL=postgresql://prod-user:secure-password@prod-db.example.com:5432/synchrobuild
HOSTINGER_API_TOKEN=prod-token
ANTHROPIC_API_KEY=prod-key
ENVIRONMENT=production
DEBUG=false
AUTONOMOUS_MODE=true  # Only after thorough testing
```

### Database Backups

Set up PostgreSQL backups:
```bash
pg_dump synchrobuild > backup-$(date +%Y%m%d).sql
```

## Security Notes

- ⚠️ Never commit `.env` files with real credentials
- ⚠️ Use strong database passwords in production
- ⚠️ Enable HTTPS/SSL on API in production
- ⚠️ Rotate API tokens regularly
- ⚠️ Set CORS origins properly in production
- ⚠️ Rate limit webhook endpoints
- ⚠️ Monitor for suspicious activity

## Support

For issues or questions:
1. Check logs for error messages
2. Verify all configuration variables
3. Test webhook endpoints with curl
4. Check database connections
5. Review AI decision reasoning

## License

Proprietary - Synchrobuild
