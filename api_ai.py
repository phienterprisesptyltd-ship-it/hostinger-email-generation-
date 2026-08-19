"""API routes for AI decision engine."""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from database import get_db
from schemas import AIDraftResponse, AIDraftUpdate
from lead_service import LeadService
from models import AIDraft, LeadStatus, AIAction
from ai_service import AIDecisionEngine

router = APIRouter(prefix="/api/ai", tags=["ai"])

ai_engine = AIDecisionEngine()


@router.get("/drafts", response_model=list[AIDraftResponse])
async def list_pending_drafts(db: Session = Depends(get_db)):
    """List pending AI drafts awaiting approval."""
    drafts = (
        db.query(AIDraft)
        .filter(
            AIDraft.is_approved == False,
            AIDraft.is_executed == False,
            AIDraft.is_rejected == False,
        )
        .order_by(AIDraft.created_at.desc())
        .limit(100)
        .all()
    )
    return drafts


@router.get("/drafts/{draft_id}", response_model=AIDraftResponse)
async def get_draft(draft_id: int, db: Session = Depends(get_db)):
    """Get a specific AI draft."""
    draft = db.query(AIDraft).filter(AIDraft.id == draft_id).first()
    if not draft:
        raise HTTPException(status_code=404, detail="Draft not found")
    return draft


@router.post("/drafts/{draft_id}/approve", response_model=AIDraftResponse)
async def approve_draft(draft_id: int, db: Session = Depends(get_db)):
    """Approve an AI draft."""
    draft = db.query(AIDraft).filter(AIDraft.id == draft_id).first()
    if not draft:
        raise HTTPException(status_code=404, detail="Draft not found")

    if draft.is_executed or draft.is_rejected:
        raise HTTPException(
            status_code=400, detail="Cannot approve executed or rejected draft"
        )

    draft.is_approved = True
    db.commit()
    db.refresh(draft)

    return draft


@router.post("/drafts/{draft_id}/reject", response_model=AIDraftResponse)
async def reject_draft(
    draft_id: int, notes: str = "", db: Session = Depends(get_db)
):
    """Reject an AI draft."""
    draft = db.query(AIDraft).filter(AIDraft.id == draft_id).first()
    if not draft:
        raise HTTPException(status_code=404, detail="Draft not found")

    if draft.is_executed:
        raise HTTPException(status_code=400, detail="Cannot reject executed draft")

    draft.is_rejected = True
    draft.approval_notes = notes
    db.commit()
    db.refresh(draft)

    return draft


@router.post("/evaluate/{lead_id}")
async def evaluate_lead(lead_id: int, db: Session = Depends(get_db)):
    """Get AI evaluation for a lead."""
    lead = LeadService.get_lead(db, lead_id)
    if not lead:
        raise HTTPException(status_code=404, detail="Lead not found")

    messages = LeadService.get_recent_messages(db, lead_id, limit=20)

    # Get AI decision
    decision = await ai_engine.evaluate_lead(lead, messages)

    return {
        "lead_id": lead_id,
        "decision": {
            "action": decision["action"].value,
            "confidence": decision["confidence"],
            "reasoning": decision["reasoning"],
            "requires_human_approval": decision["requires_human_approval"],
            "recommended_next_followup": decision.get("recommended_next_followup"),
            "recommended_status": decision.get("recommended_status"),
            "email_draft": decision.get("email_draft"),
        },
    }
