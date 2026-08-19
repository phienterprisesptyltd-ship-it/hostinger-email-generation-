#!/usr/bin/env python
"""Example usage and testing of the lead follow-up system."""
import sys
from pathlib import Path
from datetime import datetime, timedelta

# Add current directory to path
sys.path.insert(0, str(Path(__file__).parent))

from database import SessionLocal
from models import Lead, Message, LeadStatus, MessageDirection
from lead_service import LeadService
from schemas import LeadCreate
from ai_service import AIDecisionEngine
import asyncio


def example_create_leads():
    """Example: Create test leads."""
    db = SessionLocal()

    leads_data = [
        LeadCreate(
            email="john@example.com",
            full_name="John Smith",
            phone="+61234567890",
            lead_source="facebook",
            facebook_campaign_id="campaign-123",
        ),
        LeadCreate(
            email="sarah@example.com",
            full_name="Sarah Johnson",
            phone="+61234567891",
            lead_source="website_form",
        ),
        LeadCreate(
            email="mike@example.com",
            full_name="Mike Davis",
            lead_source="email_referral",
        ),
    ]

    for lead_data in leads_data:
        lead = LeadService.create_lead(db, lead_data)
        print(f"Created lead: {lead.id} - {lead.email}")

    db.close()


def example_add_conversation():
    """Example: Add conversation messages."""
    db = SessionLocal()

    # Get first lead
    lead = db.query(Lead).first()
    if not lead:
        print("No leads found. Run example_create_leads first.")
        db.close()
        return

    print(f"\nAdding messages to lead {lead.id} ({lead.email})...")

    # Simulate conversation
    now = datetime.utcnow()

    # Customer initiates contact
    msg1 = LeadService.add_message(
        db,
        lead.id,
        subject="Inquiry About Your Services",
        body="Hi, I'm interested in learning more about your services. Could you send me more information?",
        from_address=lead.email,
        to_address="info@synchrobuild.com.au",
        direction=MessageDirection.INBOUND,
        sent_at=now - timedelta(hours=24),
    )
    print(f"Message 1 (inbound): {msg1.id}")

    # We respond
    msg2 = LeadService.add_message(
        db,
        lead.id,
        subject="Re: Inquiry About Your Services",
        body="Thank you for reaching out! We'd love to help. Here's what we offer...",
        from_address="info@synchrobuild.com.au",
        to_address=lead.email,
        direction=MessageDirection.OUTBOUND,
        sent_at=now - timedelta(hours=20),
    )
    print(f"Message 2 (outbound): {msg2.id}")

    # Customer replies with interest
    msg3 = LeadService.add_message(
        db,
        lead.id,
        subject="Re: Inquiry About Your Services",
        body="That sounds great! I'm very interested. When can we schedule a call?",
        from_address=lead.email,
        to_address="info@synchrobuild.com.au",
        direction=MessageDirection.INBOUND,
        sent_at=now - timedelta(hours=2),
    )
    print(f"Message 3 (inbound): {msg3.id}")

    # Verify lead status changed
    lead = LeadService.get_lead(db, lead.id)
    print(f"\nLead status: {lead.status.value}")
    print(f"Last inbound: {lead.last_inbound_date}")
    print(f"Last outbound: {lead.last_outbound_date}")

    db.close()


async def example_ai_evaluation():
    """Example: Get AI evaluation for a lead."""
    db = SessionLocal()

    lead = db.query(Lead).first()
    if not lead:
        print("No leads found. Run example_create_leads first.")
        db.close()
        return

    messages = LeadService.get_recent_messages(db, lead.id)

    print(f"\nGetting AI evaluation for lead {lead.id} ({lead.email})...")
    print(f"Messages in conversation: {len(messages)}")

    ai = AIDecisionEngine()
    decision = await ai.evaluate_lead(
        lead,
        messages,
        available_offers=[
            {
                "name": "Free Consultation",
                "description": "30-minute consultation with our team",
            },
            {
                "name": "Starter Package",
                "description": "Full service setup for small teams",
            },
        ],
    )

    print("\nAI Decision:")
    print(f"  Action: {decision['action'].value}")
    print(f"  Confidence: {decision['confidence']:.2%}")
    print(f"  Reasoning: {decision['reasoning']}")
    print(f"  Requires Approval: {decision['requires_human_approval']}")

    if decision.get("email_draft"):
        print(f"  Email Subject: {decision['email_draft'].get('subject')}")
        print(f"  Email Preview: {decision['email_draft'].get('body')[:100]}...")

    if decision.get("recommended_status"):
        print(f"  Recommended Status: {decision['recommended_status'].value}")

    db.close()


def example_list_leads():
    """Example: List all leads with their status."""
    db = SessionLocal()

    leads = db.query(Lead).all()

    print("\n" + "=" * 80)
    print("All Leads")
    print("=" * 80)

    for lead in leads:
        messages_count = db.query(Message).filter(
            Message.lead_id == lead.id
        ).count()
        print(f"\nLead ID: {lead.id}")
        print(f"  Email: {lead.email}")
        print(f"  Name: {lead.full_name}")
        print(f"  Status: {lead.status.value}")
        print(f"  Messages: {messages_count}")
        print(f"  Follow-ups: {lead.followup_count}")
        print(f"  Last Contact: {lead.last_inbound_date}")

    db.close()


def example_follow_up_eligibility():
    """Example: Check follow-up eligibility."""
    db = SessionLocal()

    print("\n" + "=" * 80)
    print("Follow-up Eligibility Check")
    print("=" * 80)

    leads = db.query(Lead).all()

    for lead in leads:
        eligible = LeadService.should_send_followup(db, lead.id)
        status = "✓ Eligible" if eligible else "✗ Not Eligible"
        print(f"\n{status}: {lead.email}")
        print(f"  Status: {lead.status.value}")
        print(f"  Do-Not-Contact: {lead.do_not_contact}")
        print(f"  Follow-ups: {lead.followup_count}")
        print(f"  Last Outbound: {lead.last_outbound_date}")

    db.close()


def example_webhook_simulation():
    """Example: Simulate incoming webhook."""
    db = SessionLocal()

    print("\n" + "=" * 80)
    print("Webhook Simulation - Incoming Email")
    print("=" * 80)

    webhook_payload = {
        "event": "message.received",
        "message_id": "hostinger-msg-456",
        "uid": 54321,
        "from_address": "prospect@example.com",
        "to_address": "info@synchrobuild.com.au",
        "subject": "Following up on your proposal",
        "body": "Hi team, I've reviewed your proposal and I'm interested. Can we schedule a demo?",
        "folder": "INBOX",
    }

    print(f"Incoming webhook: {webhook_payload['event']}")
    print(f"From: {webhook_payload['from_address']}")
    print(f"Subject: {webhook_payload['subject']}")

    # Create or find lead
    from_address = webhook_payload["from_address"]
    lead = LeadService.get_lead_by_email(db, from_address)

    if not lead:
        print("\nLead not found, creating new lead...")
        lead = LeadService.create_lead(
            db,
            LeadCreate(
                email=from_address,
                lead_source="email_reply",
            ),
        )

    # Add message
    message = LeadService.add_message(
        db,
        lead.id,
        subject=webhook_payload["subject"],
        body=webhook_payload["body"],
        from_address=webhook_payload["from_address"],
        to_address=webhook_payload["to_address"],
        direction=MessageDirection.INBOUND,
        external_message_id=webhook_payload["message_id"],
        hostinger_uid=webhook_payload["uid"],
    )

    print(f"\nProcessed webhook:")
    print(f"  Lead ID: {lead.id}")
    print(f"  Message ID: {message.id}")
    print(f"  Lead Status: {lead.status.value}")

    db.close()


def example_unsubscribe_detection():
    """Example: Detect unsubscribe requests."""
    print("\n" + "=" * 80)
    print("Unsubscribe Detection")
    print("=" * 80)

    ai = AIDecisionEngine()

    texts = [
        "Please unsubscribe me from your mailing list",
        "Stop sending me emails",
        "I'm no longer interested, remove me",
        "Unsubscribe",
        "I'm very interested in your service",  # Should not trigger
        "Can you tell me more?",  # Should not trigger
    ]

    for text in texts:
        detected = ai.detect_unsubscribe(text)
        status = "🚫 UNSUBSCRIBE" if detected else "✓ Normal"
        print(f"\n{status}")
        print(f"  Text: {text[:60]}...")


def example_escalation_detection():
    """Example: Detect escalation keywords."""
    print("\n" + "=" * 80)
    print("Escalation Keyword Detection")
    print("=" * 80)

    ai = AIDecisionEngine()

    texts = [
        "I want a refund for this service",
        "This is absolutely unacceptable, I'm considering legal action",
        "Your product caused damage to my equipment",
        "I'd like to file a complaint",
        "I'm very happy with your service",  # Should not trigger
        "Can you help me with setup?",  # Should not trigger
    ]

    for text in texts:
        reason = ai.extract_escalation_keywords(text)
        if reason:
            print(f"\n⚠️  ESCALATION: {reason}")
            print(f"  Text: {text[:60]}...")
        else:
            print(f"\n✓ Normal")
            print(f"  Text: {text[:60]}...")


def main():
    """Run all examples."""
    print("Synchrobuild Lead Follow-up System - Examples")
    print("=" * 80)

    import argparse

    parser = argparse.ArgumentParser(description="Run system examples")
    parser.add_argument(
        "--example",
        default="all",
        choices=[
            "all",
            "create",
            "conversation",
            "ai",
            "list",
            "eligibility",
            "webhook",
            "unsubscribe",
            "escalation",
        ],
        help="Which example to run",
    )

    args = parser.parse_args()

    try:
        if args.example in ["all", "create"]:
            example_create_leads()

        if args.example in ["all", "conversation"]:
            example_add_conversation()

        if args.example in ["all", "ai"]:
            asyncio.run(example_ai_evaluation())

        if args.example in ["all", "list"]:
            example_list_leads()

        if args.example in ["all", "eligibility"]:
            example_follow_up_eligibility()

        if args.example in ["all", "webhook"]:
            example_webhook_simulation()

        if args.example in ["all", "unsubscribe"]:
            example_unsubscribe_detection()

        if args.example in ["all", "escalation"]:
            example_escalation_detection()

        print("\n" + "=" * 80)
        print("Examples completed!")
        print("=" * 80)

    except Exception as e:
        print(f"\n❌ Error: {str(e)}")
        import traceback

        traceback.print_exc()


if __name__ == "__main__":
    main()
