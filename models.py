from database import Base
from sqlalchemy import (
    Column, String, Integer, DateTime, Boolean, Text, Enum, ForeignKey, JSON, Float
)
from sqlalchemy.orm import relationship
from datetime import datetime
import enum
from typing import Optional


class LeadStatus(str, enum.Enum):
    """Lead status enumeration."""
    NEW = "new"
    CONTACTED = "contacted"
    REPLIED = "replied"
    INTERESTED = "interested"
    QUALIFIED = "qualified"
    FOLLOW_UP = "follow_up"
    NOT_INTERESTED = "not_interested"
    DO_NOT_CONTACT = "do_not_contact"
    NEEDS_HUMAN = "needs_human"
    CLOSED = "closed"


class MessageDirection(str, enum.Enum):
    """Message direction enumeration."""
    INBOUND = "inbound"
    OUTBOUND = "outbound"


class AIAction(str, enum.Enum):
    """AI action types."""
    SEND_REPLY = "SEND_REPLY"
    SEND_FOLLOWUP = "SEND_FOLLOWUP"
    WAIT = "WAIT"
    MARK_INTERESTED = "MARK_INTERESTED"
    MARK_NOT_INTERESTED = "MARK_NOT_INTERESTED"
    MARK_DO_NOT_CONTACT = "MARK_DO_NOT_CONTACT"
    ESCALATE_TO_HUMAN = "ESCALATE_TO_HUMAN"
    CLOSE_LEAD = "CLOSE_LEAD"


class Lead(Base):
    """Lead model storing customer/prospect information."""
    __tablename__ = "leads"

    id = Column(Integer, primary_key=True, index=True)

    # Basic info
    email = Column(String, unique=True, index=True, nullable=False)
    full_name = Column(String, nullable=True)
    phone = Column(String, nullable=True)

    # Lead source
    lead_source = Column(String, nullable=True)  # e.g., "facebook", "website", "referral"

    # Facebook metadata
    facebook_lead_id = Column(String, nullable=True, index=True)
    facebook_campaign_id = Column(String, nullable=True)
    facebook_ad_id = Column(String, nullable=True)
    facebook_form_id = Column(String, nullable=True)

    # Status and timeline
    status = Column(Enum(LeadStatus), default=LeadStatus.NEW, index=True)
    date_created = Column(DateTime, default=datetime.utcnow, index=True)
    last_inbound_date = Column(DateTime, nullable=True)
    last_outbound_date = Column(DateTime, nullable=True)

    # Follow-up tracking
    next_followup_date = Column(DateTime, nullable=True, index=True)
    followup_count = Column(Integer, default=0)

    # Conversation and notes
    ai_summary = Column(Text, nullable=True)
    internal_notes = Column(Text, nullable=True)

    # Configuration
    ai_automation_enabled = Column(Boolean, default=True)
    human_review_required = Column(Boolean, default=True)
    do_not_contact = Column(Boolean, default=False, index=True)

    # Relationships
    messages = relationship("Message", back_populates="lead", cascade="all, delete-orphan")
    ai_decisions = relationship("AIDraft", back_populates="lead", cascade="all, delete-orphan")

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class Message(Base):
    """Email message model for lead conversations."""
    __tablename__ = "messages"

    id = Column(Integer, primary_key=True, index=True)
    lead_id = Column(Integer, ForeignKey("leads.id"), nullable=False, index=True)

    # Message content
    subject = Column(String, nullable=True)
    body = Column(Text, nullable=False)
    html_body = Column(Text, nullable=True)

    # Message metadata
    direction = Column(Enum(MessageDirection), nullable=False, index=True)
    external_message_id = Column(String, nullable=True, unique=True, index=True)
    hostinger_uid = Column(Integer, nullable=True)

    # Sender/recipient
    from_address = Column(String, nullable=False)
    to_address = Column(String, nullable=False)
    cc_addresses = Column(JSON, nullable=True)  # Array of CC addresses
    bcc_addresses = Column(JSON, nullable=True)  # Array of BCC addresses

    # Timestamps
    sent_at = Column(DateTime, nullable=False, index=True)
    received_at = Column(DateTime, nullable=True)

    # Processing
    is_processed = Column(Boolean, default=False, index=True)
    processing_error = Column(Text, nullable=True)

    # AI analysis
    ai_sentiment = Column(String, nullable=True)  # "positive", "neutral", "negative"
    ai_analysis = Column(JSON, nullable=True)  # Structured AI analysis

    lead = relationship("Lead", back_populates="messages")

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class AIDraft(Base):
    """AI-generated email drafts awaiting approval."""
    __tablename__ = "ai_drafts"

    id = Column(Integer, primary_key=True, index=True)
    lead_id = Column(Integer, ForeignKey("leads.id"), nullable=False, index=True)

    # Proposed action
    action = Column(Enum(AIAction), nullable=False, index=True)

    # Email details (if applicable)
    subject = Column(String, nullable=True)
    body = Column(Text, nullable=True)
    html_body = Column(Text, nullable=True)

    # AI decision info
    reasoning = Column(Text, nullable=False)
    confidence = Column(Float, nullable=True)  # 0.0 to 1.0
    recommended_next_followup = Column(DateTime, nullable=True)
    recommended_status = Column(Enum(LeadStatus), nullable=True)
    requires_human_approval = Column(Boolean, default=True)

    # Processing
    is_approved = Column(Boolean, default=False, index=True)
    is_executed = Column(Boolean, default=False, index=True)
    is_rejected = Column(Boolean, default=False, index=True)

    approval_notes = Column(Text, nullable=True)
    executed_at = Column(DateTime, nullable=True)

    lead = relationship("Lead", back_populates="ai_decisions")

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class WebhookLog(Base):
    """Log of received Hostinger webhooks for deduplication."""
    __tablename__ = "webhook_logs"

    id = Column(Integer, primary_key=True, index=True)

    webhook_id = Column(String, unique=True, index=True)
    webhook_type = Column(String)
    payload = Column(JSON)

    processed = Column(Boolean, default=False, index=True)
    processing_error = Column(Text, nullable=True)

    received_at = Column(DateTime, default=datetime.utcnow, index=True)
    processed_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class Configuration(Base):
    """System-wide configuration."""
    __tablename__ = "configurations"

    id = Column(Integer, primary_key=True, index=True)

    key = Column(String, unique=True, index=True, nullable=False)
    value = Column(Text, nullable=False)
    description = Column(Text, nullable=True)
    config_type = Column(String)  # "string", "int", "bool", "json"

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class ScheduledTask(Base):
    """Log of scheduled tasks for monitoring."""
    __tablename__ = "scheduled_tasks"

    id = Column(Integer, primary_key=True, index=True)

    task_type = Column(String, index=True)  # e.g., "daily_followup", "webhook_retry"
    lead_id = Column(Integer, ForeignKey("leads.id"), nullable=True, index=True)

    scheduled_time = Column(DateTime, nullable=False, index=True)
    executed_time = Column(DateTime, nullable=True)
    execution_status = Column(String, nullable=True)  # "success", "failed", "skipped"
    execution_error = Column(Text, nullable=True)

    result = Column(JSON, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow)
