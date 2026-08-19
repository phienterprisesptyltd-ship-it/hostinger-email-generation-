"""Scheduled tasks and background jobs."""
import logging
import asyncio
from datetime import datetime, timedelta
from typing import Optional, List
from sqlalchemy.orm import Session

from database import SessionLocal
from models import Lead, AIDraft, LeadStatus, ScheduledTask, AIAction, MessageDirection
from lead_service import LeadService
from hostinger_service import HostingerEmailService
from ai_service import AIDecisionEngine
from config import settings
import pytz

logger = logging.getLogger(__name__)

hostinger = HostingerEmailService()
ai_engine = AIDecisionEngine()


async def run_daily_followup_worker():
    """
    Run the 8:00 AM follow-up worker.

    This worker:
    1. Finds leads eligible for follow-up
    2. Asks the AI what action is appropriate
    3. Creates draft emails for approval or sends (if autonomous mode)
    4. Updates lead status
    """
    logger.info("Starting daily follow-up worker")
    db = SessionLocal()

    try:
        # Get current time in business timezone
        tz = pytz.timezone(settings.business_timezone)
        now = datetime.now(tz)

        # Check if within sending hours
        if not (
            settings.approved_sending_hours_start
            <= now.hour
            < settings.approved_sending_hours_end
        ):
            logger.info(
                f"Outside sending hours ({now.hour}). Skipping follow-up worker."
            )
            return

        # Get leads eligible for follow-up
        leads = LeadService.get_leads_for_followup(db, limit=50)
        logger.info(f"Found {len(leads)} leads for follow-up")

        for lead in leads:
            try:
                # Log the task
                task = ScheduledTask(
                    task_type="daily_followup",
                    lead_id=lead.id,
                    scheduled_time=now,
                    execution_status="in_progress",
                )
                db.add(task)
                db.commit()

                # Process the lead
                await process_lead_for_followup(db, lead)

                # Mark task as successful
                task.executed_time = datetime.utcnow()
                task.execution_status = "success"

            except Exception as e:
                logger.error(f"Error processing lead {lead.id}: {str(e)}")
                task.execution_status = "failed"
                task.execution_error = str(e)

            finally:
                db.commit()

    except Exception as e:
        logger.error(f"Error in daily follow-up worker: {str(e)}")

    finally:
        db.close()


async def process_lead_for_followup(db: Session, lead: Lead):
    """Process a single lead for follow-up."""
    logger.info(f"Processing lead {lead.id} for follow-up")

    # Check if still eligible
    if not LeadService.should_send_followup(db, lead.id):
        logger.info(f"Lead {lead.id} no longer eligible for follow-up")
        return

    # Get recent messages
    messages = LeadService.get_recent_messages(db, lead.id, limit=20)
    if not messages:
        messages = []

    # Get AI decision
    decision = await ai_engine.evaluate_lead(
        lead,
        messages,
        available_offers=[
            {
                "name": "Free Consultation",
                "description": "Book a free 30-minute consultation",
            }
        ],
        business_rules={"max_followups": settings.max_automated_followups},
    )

    logger.info(
        f"AI decision for lead {lead.id}: {decision['action']} "
        f"(confidence: {decision['confidence']})"
    )

    # Handle the decision
    if decision["action"] == AIAction.SEND_FOLLOWUP:
        await handle_send_followup(db, lead, decision)

    elif decision["action"] == AIAction.SEND_REPLY:
        await handle_send_reply(db, lead, decision)

    elif decision["action"] == AIAction.MARK_INTERESTED:
        lead.status = LeadStatus.INTERESTED
        lead.updated_at = datetime.utcnow()
        db.commit()

    elif decision["action"] == AIAction.MARK_NOT_INTERESTED:
        lead.status = LeadStatus.NOT_INTERESTED
        lead.updated_at = datetime.utcnow()
        db.commit()

    elif decision["action"] == AIAction.MARK_DO_NOT_CONTACT:
        LeadService.mark_unsubscribed(db, lead.id)

    elif decision["action"] == AIAction.ESCALATE_TO_HUMAN:
        lead.status = LeadStatus.NEEDS_HUMAN
        lead.human_review_required = True
        lead.updated_at = datetime.utcnow()
        db.commit()

    elif decision["action"] == AIAction.CLOSE_LEAD:
        lead.status = LeadStatus.CLOSED
        lead.updated_at = datetime.utcnow()
        db.commit()

    # Update next follow-up date if provided
    if decision.get("recommended_next_followup"):
        LeadService.update_next_followup(
            db, lead.id, decision["recommended_next_followup"]
        )


async def handle_send_followup(db: Session, lead: Lead, decision: dict):
    """Handle SEND_FOLLOWUP action."""
    if not decision.get("email_draft"):
        logger.warning(f"SEND_FOLLOWUP action for lead {lead.id} has no email_draft")
        return

    email_draft = decision["email_draft"]

    # Create draft for approval
    draft = AIDraft(
        lead_id=lead.id,
        action=AIAction.SEND_FOLLOWUP,
        subject=email_draft.get("subject", "(no subject)"),
        body=email_draft.get("body", ""),
        reasoning=decision["reasoning"],
        confidence=decision["confidence"],
        recommended_next_followup=decision.get("recommended_next_followup"),
        recommended_status=decision.get("recommended_status"),
        requires_human_approval=decision["requires_human_approval"],
        is_approved=False,
    )

    db.add(draft)

    # In autonomous mode without requiring approval, send directly
    if (
        settings.autonomous_mode
        and not decision["requires_human_approval"]
        and lead.ai_automation_enabled
    ):
        # Send the email
        result = await hostinger.send_email(
            to_address=lead.email,
            subject=email_draft.get("subject", ""),
            body=email_draft.get("body", ""),
            html_body=email_draft.get("html_body"),
        )

        if result["success"]:
            # Record the sent message
            LeadService.add_message(
                db,
                lead.id,
                subject=email_draft.get("subject", ""),
                body=email_draft.get("body", ""),
                from_address=settings.hostinger_sender_address,
                to_address=lead.email,
                direction=MessageDirection.OUTBOUND,
                sent_at=datetime.utcnow(),
            )

            # Update draft as executed
            draft.is_approved = True
            draft.is_executed = True
            draft.executed_at = datetime.utcnow()

            # Update lead
            LeadService.increment_followup_count(db, lead.id)
            lead.status = decision.get("recommended_status", LeadStatus.FOLLOW_UP)
            lead.next_followup_date = decision.get("recommended_next_followup")

            logger.info(f"Sent follow-up email to lead {lead.id}")
        else:
            logger.error(f"Failed to send email to lead {lead.id}: {result}")
            draft.processing_error = str(result)

    db.commit()


async def handle_send_reply(db: Session, lead: Lead, decision: dict):
    """Handle SEND_REPLY action."""
    if not decision.get("email_draft"):
        logger.warning(f"SEND_REPLY action for lead {lead.id} has no email_draft")
        return

    email_draft = decision["email_draft"]

    # Always require approval for replies to customer messages
    draft = AIDraft(
        lead_id=lead.id,
        action=AIAction.SEND_REPLY,
        subject=email_draft.get("subject", "(no subject)"),
        body=email_draft.get("body", ""),
        reasoning=decision["reasoning"],
        confidence=decision["confidence"],
        recommended_next_followup=decision.get("recommended_next_followup"),
        recommended_status=decision.get("recommended_status"),
        requires_human_approval=True,  # Always require approval for direct replies
        is_approved=False,
    )

    db.add(draft)
    db.commit()

    logger.info(f"Created draft reply for lead {lead.id} awaiting approval")


async def process_incoming_email_reply(db: Session, lead_id: int, message_id: str):
    """Process an incoming email reply."""
    logger.info(f"Processing incoming reply for lead {lead_id}")

    lead = LeadService.get_lead(db, lead_id)
    if not lead:
        logger.error(f"Lead {lead_id} not found")
        return

    # Detect unsubscribe
    # In real implementation, would fetch full message body from Hostinger
    # For now, this is a placeholder

    # Get recent messages
    messages = LeadService.get_recent_messages(db, lead_id, limit=10)

    # Get AI decision for the reply
    decision = await ai_engine.evaluate_lead(lead, messages)

    # Process based on decision (same as follow-up)
    if decision["action"] == AIAction.SEND_REPLY:
        await handle_send_reply(db, lead, decision)
    elif decision["action"] == AIAction.ESCALATE_TO_HUMAN:
        lead.status = LeadStatus.NEEDS_HUMAN
        db.commit()

    logger.info(f"Processed incoming reply for lead {lead_id}")
