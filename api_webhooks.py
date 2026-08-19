"""API routes for webhooks."""
import logging
import hmac
import hashlib
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Header, Request
from sqlalchemy.orm import Session
from datetime import datetime

from database import get_db
from models import WebhookLog, MessageDirection, Message
from lead_service import LeadService
from ai_service import AIDecisionEngine
from tasks import process_lead_for_followup
from config import settings
import asyncio

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/webhooks", tags=["webhooks"])
ai_engine = AIDecisionEngine()


def verify_webhook_signature(
    payload_bytes: bytes, signature: Optional[str]
) -> bool:
    """
    Verify webhook signature using HMAC-SHA256.

    Args:
        payload_bytes: Raw webhook payload bytes
        signature: X-Hostinger-Signature header value

    Returns:
        True if signature is valid
    """
    if not settings.hostinger_webhook_secret:
        logger.warning("HOSTINGER_WEBHOOK_SECRET not configured, skipping verification")
        return True

    if not signature:
        logger.warning("Webhook request missing X-Hostinger-Signature header")
        return False

    expected_signature = hmac.new(
        settings.hostinger_webhook_secret.encode(),
        payload_bytes,
        hashlib.sha256
    ).hexdigest()

    return hmac.compare_digest(signature, expected_signature)


@router.post("/hostinger/message")
async def handle_hostinger_webhook(
    request: Request,
    db: Session = Depends(get_db),
    x_webhook_id: str = Header(None),
    x_hostinger_signature: str = Header(None),
):
    """
    Handle incoming email webhook from Hostinger.

    Expected payload:
    {
        "event": "message.received",
        "message_id": "...",
        "uid": 12345,
        "from_address": "customer@example.com",
        "to_address": "info@synchrobuild.com.au",
        "subject": "...",
        "body": "...",
        "folder": "INBOX"
    }
    """
    try:
        # Get raw request body for signature verification
        body_bytes = await request.body()

        # Verify webhook signature
        if not verify_webhook_signature(body_bytes, x_hostinger_signature):
            logger.error(f"Invalid webhook signature for webhook ID: {x_webhook_id}")
            raise HTTPException(status_code=401, detail="Invalid webhook signature")

        # Parse JSON payload
        import json
        payload = json.loads(body_bytes)
        webhook_id = x_webhook_id or f"{datetime.utcnow().timestamp()}"

        # Check for duplicate webhook
        existing = (
            db.query(WebhookLog).filter(WebhookLog.webhook_id == webhook_id).first()
        )
        if existing:
            logger.warning(f"Duplicate webhook received: {webhook_id}")
            return {"status": "duplicate", "webhook_id": webhook_id}

        # Log webhook
        webhook_log = WebhookLog(
            webhook_id=webhook_id,
            webhook_type=payload.get("event"),
            payload=payload,
        )
        db.add(webhook_log)
        db.commit()

        # Extract email details
        from_address = payload.get("from_address")
        to_address = payload.get("to_address")
        subject = payload.get("subject", "(no subject)")
        body = payload.get("body", "")
        message_id = payload.get("message_id")
        uid = payload.get("uid")

        if not from_address or not body:
            logger.error("Invalid webhook payload: missing from_address or body")
            webhook_log.processing_error = "Invalid payload"
            db.commit()
            raise HTTPException(status_code=400, detail="Invalid payload")

        # Find or create lead
        lead = LeadService.get_lead_by_email(db, from_address)
        if not lead:
            logger.info(f"Creating new lead from incoming email: {from_address}")
            from schemas import LeadCreate

            lead = LeadService.create_lead(
                db,
                LeadCreate(
                    email=from_address,
                    lead_source="email_reply",
                ),
            )

        # Check if lead is do-not-contact
        if lead.do_not_contact:
            logger.info(f"Ignoring email from do-not-contact lead {lead.id}")
            webhook_log.processed = True
            db.commit()
            return {
                "status": "ignored",
                "reason": "lead is do-not-contact",
                "webhook_id": webhook_id,
            }

        # Detect unsubscribe
        if ai_engine.detect_unsubscribe(body):
            logger.info(f"Unsubscribe request from lead {lead.id}")
            LeadService.mark_unsubscribed(db, lead.id)
            webhook_log.processed = True
            db.commit()
            return {
                "status": "unsubscribed",
                "webhook_id": webhook_id,
            }

        # Check for escalation keywords
        escalation_reason = ai_engine.extract_escalation_keywords(body)
        if escalation_reason:
            logger.warning(f"Escalation detected for lead {lead.id}: {escalation_reason}")
            lead.human_review_required = True
            from models import LeadStatus

            lead.status = LeadStatus.NEEDS_HUMAN
            db.commit()

        # Add message to conversation
        message = LeadService.add_message(
            db,
            lead.id,
            subject=subject,
            body=body,
            from_address=from_address,
            to_address=to_address,
            direction=MessageDirection.INBOUND,
            external_message_id=message_id,
            hostinger_uid=uid,
        )

        logger.info(f"Added incoming message {message.id} for lead {lead.id}")

        # Mark webhook as processed
        webhook_log.processed = True
        webhook_log.processed_at = datetime.utcnow()
        db.commit()

        # Trigger AI decision asynchronously (non-blocking)
        asyncio.create_task(process_lead_for_followup(db, lead))

        return {
            "status": "accepted",
            "lead_id": lead.id,
            "message_id": message.id,
            "webhook_id": webhook_id,
        }

    except Exception as e:
        logger.error(f"Error processing webhook: {str(e)}")
        if "webhook_log" in locals():
            webhook_log.processing_error = str(e)
            db.commit()
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/test")
async def test_webhook(payload: dict):
    """Test webhook endpoint."""
    logger.info(f"Test webhook received: {payload}")
    return {
        "status": "ok",
        "message": "Test webhook received",
    }


@router.get("/status")
async def webhook_status(db: Session = Depends(get_db)):
    """Get webhook status and statistics."""
    from sqlalchemy import func

    total_webhooks = db.query(func.count(WebhookLog.id)).scalar()
    processed = db.query(func.count(WebhookLog.id)).filter(
        WebhookLog.processed == True
    ).scalar()
    failed = db.query(func.count(WebhookLog.id)).filter(
        WebhookLog.processing_error.isnot(None)
    ).scalar()

    return {
        "total_webhooks_received": total_webhooks,
        "processed": processed,
        "failed": failed,
        "pending": total_webhooks - processed,
    }
