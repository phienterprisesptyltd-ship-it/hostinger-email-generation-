from pydantic import BaseModel, EmailStr, Field
from datetime import datetime
from typing import Optional, List
from models import LeadStatus, AIAction, MessageDirection


class MessageBase(BaseModel):
    subject: Optional[str] = None
    body: str
    html_body: Optional[str] = None
    direction: MessageDirection
    from_address: str
    to_address: str
    cc_addresses: Optional[List[str]] = None
    bcc_addresses: Optional[List[str]] = None
    sent_at: datetime


class MessageCreate(MessageBase):
    external_message_id: Optional[str] = None
    hostinger_uid: Optional[int] = None


class MessageResponse(MessageBase):
    id: int
    lead_id: int
    external_message_id: Optional[str]
    hostinger_uid: Optional[int]
    is_processed: bool
    ai_sentiment: Optional[str]
    ai_analysis: Optional[dict]
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class LeadBase(BaseModel):
    email: EmailStr
    full_name: Optional[str] = None
    phone: Optional[str] = None
    lead_source: Optional[str] = None
    facebook_lead_id: Optional[str] = None
    facebook_campaign_id: Optional[str] = None
    facebook_ad_id: Optional[str] = None
    facebook_form_id: Optional[str] = None
    internal_notes: Optional[str] = None


class LeadCreate(LeadBase):
    pass


class LeadUpdate(BaseModel):
    full_name: Optional[str] = None
    phone: Optional[str] = None
    status: Optional[LeadStatus] = None
    ai_automation_enabled: Optional[bool] = None
    human_review_required: Optional[bool] = None
    do_not_contact: Optional[bool] = None
    internal_notes: Optional[str] = None
    ai_summary: Optional[str] = None


class LeadResponse(LeadBase):
    id: int
    status: LeadStatus
    date_created: datetime
    last_inbound_date: Optional[datetime]
    last_outbound_date: Optional[datetime]
    next_followup_date: Optional[datetime]
    followup_count: int
    ai_summary: Optional[str]
    ai_automation_enabled: bool
    human_review_required: bool
    do_not_contact: bool
    messages: Optional[List[MessageResponse]] = []
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class AIDraftBase(BaseModel):
    action: AIAction
    subject: Optional[str] = None
    body: Optional[str] = None
    html_body: Optional[str] = None
    reasoning: str
    confidence: Optional[float] = None
    recommended_next_followup: Optional[datetime] = None
    recommended_status: Optional[LeadStatus] = None
    requires_human_approval: bool = True


class AIDraftCreate(AIDraftBase):
    lead_id: int


class AIDraftUpdate(BaseModel):
    is_approved: Optional[bool] = None
    is_executed: Optional[bool] = None
    is_rejected: Optional[bool] = None
    approval_notes: Optional[str] = None


class AIDraftResponse(AIDraftBase):
    id: int
    lead_id: int
    is_approved: bool
    is_executed: bool
    is_rejected: bool
    approval_notes: Optional[str]
    executed_at: Optional[datetime]
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class ConversationHistoryResponse(BaseModel):
    lead_id: int
    lead_email: str
    lead_name: Optional[str]
    lead_status: LeadStatus
    messages: List[MessageResponse]
    ai_summary: Optional[str]

    class Config:
        from_attributes = True


class HostingerWebhookPayload(BaseModel):
    """Expected Hostinger email webhook payload."""
    event: str
    mailbox_id: Optional[str] = None
    message_id: Optional[str] = None
    uid: Optional[int] = None
    from_address: Optional[str] = None
    to_address: Optional[str] = None
    subject: Optional[str] = None
    body: Optional[str] = None


class AIDecisionRequest(BaseModel):
    """Request for AI to make a decision about a lead."""
    lead_id: int
    force_check: Optional[bool] = False


class AIDecisionResponse(BaseModel):
    """AI decision output."""
    action: AIAction
    confidence: float
    reasoning: str
    requires_human_approval: bool
    email_draft: Optional[dict] = None  # {subject, body} if applicable
    recommended_next_followup: Optional[datetime] = None
    recommended_status: Optional[LeadStatus] = None


class AdminDashboardResponse(BaseModel):
    """Admin dashboard summary."""
    total_leads: int
    leads_by_status: dict
    pending_approvals: int
    recent_messages: int
    next_scheduled_followups: List[dict]
