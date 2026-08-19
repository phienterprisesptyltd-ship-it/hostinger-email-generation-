"""API routes for lead management."""
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from database import get_db
from schemas import LeadCreate, LeadUpdate, LeadResponse
from lead_service import LeadService
from models import LeadStatus

router = APIRouter(prefix="/api/leads", tags=["leads"])


@router.post("", response_model=LeadResponse)
async def create_lead(lead_data: LeadCreate, db: Session = Depends(get_db)):
    """Create a new lead."""
    return LeadService.create_lead(db, lead_data)


@router.get("/{lead_id}", response_model=LeadResponse)
async def get_lead(lead_id: int, db: Session = Depends(get_db)):
    """Get a lead by ID."""
    lead = LeadService.get_lead(db, lead_id)
    if not lead:
        raise HTTPException(status_code=404, detail="Lead not found")
    return lead


@router.put("/{lead_id}", response_model=LeadResponse)
async def update_lead(
    lead_id: int, lead_data: LeadUpdate, db: Session = Depends(get_db)
):
    """Update a lead."""
    try:
        return LeadService.update_lead(db, lead_id, lead_data)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.get("", response_model=list[LeadResponse])
async def list_leads(
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
    status: LeadStatus = Query(None),
    db: Session = Depends(get_db),
):
    """List leads."""
    query = db.query(LeadService)
    if status:
        query = query.filter(LeadService.status == status)
    return query.offset(skip).limit(limit).all()


@router.post("/{lead_id}/mark-unsubscribed", response_model=LeadResponse)
async def mark_unsubscribed(lead_id: int, db: Session = Depends(get_db)):
    """Mark a lead as do-not-contact."""
    return LeadService.mark_unsubscribed(db, lead_id)


@router.get("/{lead_id}/conversation", response_model=dict)
async def get_conversation(lead_id: int, db: Session = Depends(get_db)):
    """Get full conversation history for a lead."""
    lead = LeadService.get_lead(db, lead_id)
    if not lead:
        raise HTTPException(status_code=404, detail="Lead not found")

    messages = LeadService.get_recent_messages(db, lead_id, limit=100)
    messages.reverse()  # Chronological order

    return {
        "lead_id": lead.id,
        "email": lead.email,
        "name": lead.full_name,
        "status": lead.status,
        "messages": [
            {
                "id": m.id,
                "direction": m.direction.value,
                "subject": m.subject,
                "body": m.body,
                "sent_at": m.sent_at,
                "from": m.from_address,
                "to": m.to_address,
            }
            for m in messages
        ],
    }
