"""Lead management service."""
import logging
from typing import Optional, List
from datetime import datetime, timedelta
from sqlalchemy.orm import Session
from sqlalchemy import and_, or_

from models import Lead, Message, LeadStatus, MessageDirection
from schemas import LeadCreate, LeadUpdate

logger = logging.getLogger(__name__)


class LeadService:
    """Service for managing leads."""

    @staticmethod
    def create_lead(db: Session, lead_data: LeadCreate) -> Lead:
        """Create a new lead."""
        # Check if lead already exists by email
        existing = db.query(Lead).filter(Lead.email == lead_data.email).first()
        if existing:
            logger.warning(f"Lead with email {lead_data.email} already exists")
            return existing

        lead = Lead(**lead_data.model_dump())
        db.add(lead)
        db.commit()
        db.refresh(lead)
        logger.info(f"Created new lead: {lead.id} - {lead.email}")
        return lead

    @staticmethod
    def get_lead(db: Session, lead_id: int) -> Optional[Lead]:
        """Get a lead by ID."""
        return db.query(Lead).filter(Lead.id == lead_id).first()

    @staticmethod
    def get_lead_by_email(db: Session, email: str) -> Optional[Lead]:
        """Get a lead by email address."""
        return db.query(Lead).filter(Lead.email == email).first()

    @staticmethod
    def get_lead_by_facebook_id(db: Session, facebook_lead_id: str) -> Optional[Lead]:
        """Get a lead by Facebook lead ID."""
        return (
            db.query(Lead)
            .filter(Lead.facebook_lead_id == facebook_lead_id)
            .first()
        )

    @staticmethod
    def update_lead(db: Session, lead_id: int, lead_data: LeadUpdate) -> Lead:
        """Update a lead."""
        lead = db.query(Lead).filter(Lead.id == lead_id).first()
        if not lead:
            raise ValueError(f"Lead {lead_id} not found")

        for key, value in lead_data.model_dump(exclude_unset=True).items():
            if value is not None:
                setattr(lead, key, value)

        lead.updated_at = datetime.utcnow()
        db.commit()
        db.refresh(lead)
        logger.info(f"Updated lead {lead_id}")
        return lead

    @staticmethod
    def get_leads_for_followup(db: Session, limit: int = 50) -> List[Lead]:
        """
        Get leads eligible for follow-up.

        Returns leads that:
        - Are not marked do_not_contact
        - Are not closed or not_interested
        - Don't require human review
        - Are due for follow-up (next_followup_date <= now)
        - Haven't exceeded max follow-ups
        """
        from config import settings

        now = datetime.utcnow()
        excluded_statuses = [
            LeadStatus.DO_NOT_CONTACT,
            LeadStatus.CLOSED,
            LeadStatus.NOT_INTERESTED,
            LeadStatus.NEEDS_HUMAN,
        ]

        query = db.query(Lead).filter(
            and_(
                ~Lead.status.in_(excluded_statuses),
                Lead.do_not_contact == False,
                Lead.ai_automation_enabled == True,
                or_(
                    Lead.next_followup_date == None,
                    Lead.next_followup_date <= now,
                ),
                Lead.followup_count < settings.max_automated_followups,
            )
        )

        return query.order_by(Lead.next_followup_date).limit(limit).all()

    @staticmethod
    def get_pending_approval_leads(db: Session) -> List[Lead]:
        """Get leads with pending AI approvals."""
        from models import AIDraft

        return (
            db.query(Lead)
            .join(AIDraft, Lead.id == AIDraft.lead_id)
            .filter(
                and_(
                    AIDraft.is_approved == False,
                    AIDraft.is_executed == False,
                    AIDraft.is_rejected == False,
                )
            )
            .distinct()
            .all()
        )

    @staticmethod
    def get_recent_messages(
        db: Session, lead_id: int, limit: int = 50
    ) -> List[Message]:
        """Get recent messages for a lead."""
        return (
            db.query(Message)
            .filter(Message.lead_id == lead_id)
            .order_by(Message.sent_at.desc())
            .limit(limit)
            .all()
        )

    @staticmethod
    def add_message(
        db: Session,
        lead_id: int,
        subject: str,
        body: str,
        from_address: str,
        to_address: str,
        direction: MessageDirection,
        sent_at: Optional[datetime] = None,
        html_body: Optional[str] = None,
        external_message_id: Optional[str] = None,
        hostinger_uid: Optional[int] = None,
    ) -> Message:
        """Add a message to a lead's conversation."""
        if sent_at is None:
            sent_at = datetime.utcnow()

        # Check for duplicate
        if external_message_id:
            existing = (
                db.query(Message)
                .filter(Message.external_message_id == external_message_id)
                .first()
            )
            if existing:
                logger.warning(
                    f"Duplicate message detected: {external_message_id}"
                )
                return existing

        message = Message(
            lead_id=lead_id,
            subject=subject,
            body=body,
            html_body=html_body,
            from_address=from_address,
            to_address=to_address,
            direction=direction,
            sent_at=sent_at,
            external_message_id=external_message_id,
            hostinger_uid=hostinger_uid,
        )

        db.add(message)

        # Update lead timestamps
        lead = db.query(Lead).filter(Lead.id == lead_id).first()
        if lead:
            if direction == MessageDirection.INBOUND:
                lead.last_inbound_date = sent_at
                lead.status = LeadStatus.REPLIED
            else:
                lead.last_outbound_date = sent_at

            if lead.status == LeadStatus.NEW:
                lead.status = LeadStatus.CONTACTED

            lead.updated_at = datetime.utcnow()

        db.commit()
        db.refresh(message)
        logger.info(
            f"Added {direction.value} message to lead {lead_id}: {message.id}"
        )
        return message

    @staticmethod
    def should_send_followup(
        db: Session, lead_id: int, min_hours: Optional[int] = None
    ) -> bool:
        """
        Check if a lead is eligible for a follow-up email.

        Returns False if:
        - Lead is do_not_contact
        - Lead has exceeded max follow-ups
        - Lead has had a message too recently (min_hours)
        """
        from config import settings

        if min_hours is None:
            min_hours = settings.min_followup_hours

        lead = db.query(Lead).filter(Lead.id == lead_id).first()
        if not lead:
            return False

        if lead.do_not_contact or lead.status == LeadStatus.DO_NOT_CONTACT:
            return False

        if lead.followup_count >= settings.max_automated_followups:
            return False

        # Check minimum time between messages
        if lead.last_outbound_date:
            min_time = lead.last_outbound_date + timedelta(hours=min_hours)
            if datetime.utcnow() < min_time:
                return False

        return True

    @staticmethod
    def increment_followup_count(db: Session, lead_id: int) -> None:
        """Increment the follow-up count for a lead."""
        lead = db.query(Lead).filter(Lead.id == lead_id).first()
        if lead:
            lead.followup_count += 1
            lead.updated_at = datetime.utcnow()
            db.commit()

    @staticmethod
    def mark_unsubscribed(db: Session, lead_id: int) -> Lead:
        """Mark a lead as do_not_contact."""
        lead = db.query(Lead).filter(Lead.id == lead_id).first()
        if lead:
            lead.do_not_contact = True
            lead.status = LeadStatus.DO_NOT_CONTACT
            lead.updated_at = datetime.utcnow()
            db.commit()
            db.refresh(lead)
            logger.info(f"Marked lead {lead_id} as do_not_contact")
        return lead

    @staticmethod
    def get_leads_by_status(
        db: Session, status: LeadStatus, limit: int = 50
    ) -> List[Lead]:
        """Get leads by status."""
        return (
            db.query(Lead)
            .filter(Lead.status == status)
            .order_by(Lead.last_inbound_date.desc())
            .limit(limit)
            .all()
        )

    @staticmethod
    def update_next_followup(
        db: Session, lead_id: int, next_followup_date: datetime
    ) -> None:
        """Update the next follow-up date for a lead."""
        lead = db.query(Lead).filter(Lead.id == lead_id).first()
        if lead:
            lead.next_followup_date = next_followup_date
            lead.updated_at = datetime.utcnow()
            db.commit()
