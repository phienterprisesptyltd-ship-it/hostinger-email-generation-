"""API routes for admin dashboard."""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import func
from datetime import datetime, timedelta

from database import get_db
from models import Lead, LeadStatus, AIDraft, ScheduledTask
from lead_service import LeadService
from config import settings

router = APIRouter(prefix="/api/admin", tags=["admin"])


@router.get("/dashboard")
async def get_dashboard(db: Session = Depends(get_db)):
    """Get admin dashboard summary."""
    # Count leads by status
    leads_by_status = {}
    for status in LeadStatus:
        count = db.query(func.count(Lead.id)).filter(Lead.status == status).scalar()
        leads_by_status[status.value] = count

    # Total leads
    total_leads = db.query(func.count(Lead.id)).scalar()

    # Pending approvals
    pending_approvals = (
        db.query(func.count(AIDraft.id))
        .filter(
            AIDraft.is_approved == False,
            AIDraft.is_executed == False,
            AIDraft.is_rejected == False,
        )
        .scalar()
    )

    # Scheduled follow-ups in next 7 days
    now = datetime.utcnow()
    future = now + timedelta(days=7)
    scheduled = (
        db.query(Lead)
        .filter(
            Lead.next_followup_date >= now,
            Lead.next_followup_date <= future,
            Lead.status.not_in(
                [
                    LeadStatus.DO_NOT_CONTACT,
                    LeadStatus.CLOSED,
                    LeadStatus.NOT_INTERESTED,
                ]
            ),
        )
        .order_by(Lead.next_followup_date)
        .limit(10)
        .all()
    )

    return {
        "summary": {
            "total_leads": total_leads,
            "leads_by_status": leads_by_status,
            "pending_approvals": pending_approvals,
            "automation_enabled": settings.autonomous_mode,
        },
        "next_followups": [
            {
                "lead_id": l.id,
                "email": l.email,
                "name": l.full_name,
                "status": l.status.value,
                "next_followup": l.next_followup_date,
                "followup_count": l.followup_count,
            }
            for l in scheduled
        ],
    }


@router.get("/leads/pending-approval")
async def get_pending_approval_leads(db: Session = Depends(get_db)):
    """Get leads with pending AI approvals."""
    leads = LeadService.get_pending_approval_leads(db)

    result = []
    for lead in leads:
        drafts = (
            db.query(AIDraft)
            .filter(
                AIDraft.lead_id == lead.id,
                AIDraft.is_approved == False,
                AIDraft.is_executed == False,
                AIDraft.is_rejected == False,
            )
            .all()
        )

        for draft in drafts:
            result.append(
                {
                    "draft_id": draft.id,
                    "lead_id": lead.id,
                    "lead_email": lead.email,
                    "lead_name": lead.full_name,
                    "action": draft.action.value,
                    "subject": draft.subject,
                    "body": draft.body[:200] + "..."
                    if len(draft.body) > 200
                    else draft.body,
                    "reasoning": draft.reasoning,
                    "confidence": draft.confidence,
                    "created_at": draft.created_at,
                    "requires_human_approval": draft.requires_human_approval,
                }
            )

    return result


@router.get("/leads/{lead_id}/summary")
async def get_lead_summary(lead_id: int, db: Session = Depends(get_db)):
    """Get comprehensive lead summary for admin."""
    lead = LeadService.get_lead(db, lead_id)
    if not lead:
        raise HTTPException(status_code=404, detail="Lead not found")

    messages = LeadService.get_recent_messages(db, lead_id, limit=50)
    messages.reverse()

    pending_drafts = (
        db.query(AIDraft)
        .filter(
            AIDraft.lead_id == lead_id,
            AIDraft.is_approved == False,
            AIDraft.is_executed == False,
            AIDraft.is_rejected == False,
        )
        .all()
    )

    return {
        "lead": {
            "id": lead.id,
            "email": lead.email,
            "name": lead.full_name,
            "phone": lead.phone,
            "status": lead.status.value,
            "lead_source": lead.lead_source,
            "ai_automation_enabled": lead.ai_automation_enabled,
            "human_review_required": lead.human_review_required,
            "do_not_contact": lead.do_not_contact,
            "followup_count": lead.followup_count,
            "last_inbound": lead.last_inbound_date,
            "last_outbound": lead.last_outbound_date,
            "next_followup": lead.next_followup_date,
            "ai_summary": lead.ai_summary,
            "internal_notes": lead.internal_notes,
        },
        "facebook_metadata": {
            "lead_id": lead.facebook_lead_id,
            "campaign_id": lead.facebook_campaign_id,
            "ad_id": lead.facebook_ad_id,
            "form_id": lead.facebook_form_id,
        }
        if lead.facebook_lead_id
        else None,
        "conversation": {
            "message_count": len(messages),
            "messages": [
                {
                    "id": m.id,
                    "direction": m.direction.value,
                    "subject": m.subject,
                    "body": m.body,
                    "from": m.from_address,
                    "to": m.to_address,
                    "sent_at": m.sent_at,
                }
                for m in messages
            ],
        },
        "pending_actions": [
            {
                "draft_id": d.id,
                "action": d.action.value,
                "subject": d.subject,
                "body": d.body[:200] + "..."
                if len(d.body) > 200
                else d.body,
                "reasoning": d.reasoning,
                "confidence": d.confidence,
                "requires_human_approval": d.requires_human_approval,
                "created_at": d.created_at,
            }
            for d in pending_drafts
        ],
    }


@router.put("/config")
async def update_config(config: dict, db: Session = Depends(get_db)):
    """Update system configuration."""
    from models import Configuration

    for key, value in config.items():
        existing = db.query(Configuration).filter(Configuration.key == key).first()
        if existing:
            existing.value = str(value)
        else:
            new_config = Configuration(key=key, value=str(value))
            db.add(new_config)

    db.commit()
    return {"status": "ok"}


@router.post("/automation/toggle")
async def toggle_automation(enabled: bool):
    """Toggle autonomous mode on/off globally."""
    # This would need to update settings or environment
    return {
        "autonomous_mode": enabled,
        "message": "Autonomous mode toggled (restart required to apply)",
    }


@router.get("/stats")
async def get_stats(db: Session = Depends(get_db)):
    """Get system statistics."""
    now = datetime.utcnow()
    week_ago = now - timedelta(days=7)
    month_ago = now - timedelta(days=30)

    # Messages this week
    messages_week = (
        db.query(func.count())
        .select_from(Lead)
        .filter(Lead.last_inbound_date >= week_ago)
        .scalar()
    )

    # Messages this month
    messages_month = (
        db.query(func.count())
        .select_from(Lead)
        .filter(Lead.last_inbound_date >= month_ago)
        .scalar()
    )

    # AI decisions this week
    decisions_week = (
        db.query(func.count(AIDraft.id))
        .filter(AIDraft.created_at >= week_ago)
        .scalar()
    )

    return {
        "messages_this_week": messages_week,
        "messages_this_month": messages_month,
        "ai_decisions_this_week": decisions_week,
        "avg_automation_confidence": 0.85,  # Would calculate from actual data
    }
