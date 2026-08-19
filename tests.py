"""Tests for the lead follow-up system."""
import pytest
from datetime import datetime, timedelta
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from database import Base
from models import (
    Lead,
    Message,
    LeadStatus,
    MessageDirection,
    AIDraft,
    AIAction,
)
from lead_service import LeadService
from schemas import LeadCreate
from ai_service import AIDecisionEngine

# Use in-memory SQLite for tests
TEST_DATABASE_URL = "sqlite:///:memory:"


@pytest.fixture
def test_db():
    """Create test database."""
    engine = create_engine(TEST_DATABASE_URL, connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    db = TestingSessionLocal()
    yield db
    db.close()


class TestLeadService:
    """Tests for LeadService."""

    def test_create_lead(self, test_db):
        """Test creating a new lead."""
        lead_data = LeadCreate(
            email="test@example.com",
            full_name="Test Lead",
            lead_source="email",
        )
        lead = LeadService.create_lead(test_db, lead_data)

        assert lead.id is not None
        assert lead.email == "test@example.com"
        assert lead.full_name == "Test Lead"
        assert lead.status == LeadStatus.NEW

    def test_duplicate_lead_prevention(self, test_db):
        """Test that duplicate leads are not created."""
        lead_data = LeadCreate(email="test@example.com", full_name="Test")
        lead1 = LeadService.create_lead(test_db, lead_data)
        lead2 = LeadService.create_lead(test_db, lead_data)

        assert lead1.id == lead2.id

    def test_get_lead_by_email(self, test_db):
        """Test finding lead by email."""
        lead_data = LeadCreate(email="test@example.com")
        created = LeadService.create_lead(test_db, lead_data)
        found = LeadService.get_lead_by_email(test_db, "test@example.com")

        assert found.id == created.id

    def test_add_message(self, test_db):
        """Test adding message to lead."""
        lead_data = LeadCreate(email="test@example.com")
        lead = LeadService.create_lead(test_db, lead_data)

        message = LeadService.add_message(
            test_db,
            lead.id,
            subject="Test Subject",
            body="Test body",
            from_address="test@example.com",
            to_address="info@example.com",
            direction=MessageDirection.INBOUND,
        )

        assert message.lead_id == lead.id
        assert message.subject == "Test Subject"
        assert message.direction == MessageDirection.INBOUND

    def test_duplicate_message_prevention(self, test_db):
        """Test that duplicate messages are not created."""
        lead_data = LeadCreate(email="test@example.com")
        lead = LeadService.create_lead(test_db, lead_data)

        msg1 = LeadService.add_message(
            test_db,
            lead.id,
            subject="Test",
            body="Test body",
            from_address="test@example.com",
            to_address="info@example.com",
            direction=MessageDirection.INBOUND,
            external_message_id="msg-123",
        )

        msg2 = LeadService.add_message(
            test_db,
            lead.id,
            subject="Test",
            body="Test body",
            from_address="test@example.com",
            to_address="info@example.com",
            direction=MessageDirection.INBOUND,
            external_message_id="msg-123",
        )

        assert msg1.id == msg2.id

    def test_should_send_followup_basic(self, test_db):
        """Test basic follow-up eligibility."""
        lead_data = LeadCreate(email="test@example.com")
        lead = LeadService.create_lead(test_db, lead_data)

        # Should be eligible initially
        assert LeadService.should_send_followup(test_db, lead.id)

    def test_should_not_send_to_do_not_contact(self, test_db):
        """Test that do-not-contact leads are skipped."""
        lead_data = LeadCreate(email="test@example.com")
        lead = LeadService.create_lead(test_db, lead_data)

        LeadService.mark_unsubscribed(test_db, lead.id)
        assert not LeadService.should_send_followup(test_db, lead.id)

    def test_follow_up_count_limit(self, test_db):
        """Test follow-up count limit enforcement."""
        from config import settings

        lead_data = LeadCreate(email="test@example.com")
        lead = LeadService.create_lead(test_db, lead_data)

        # Add follow-ups until limit
        for _ in range(settings.max_automated_followups):
            LeadService.increment_followup_count(test_db, lead.id)

        # Should not be eligible for more follow-ups
        assert not LeadService.should_send_followup(test_db, lead.id)

    def test_min_followup_timing(self, test_db):
        """Test minimum follow-up timing enforcement."""
        from config import settings

        lead_data = LeadCreate(email="test@example.com")
        lead = LeadService.create_lead(test_db, lead_data)

        # Send a message
        now = datetime.utcnow()
        LeadService.add_message(
            test_db,
            lead.id,
            subject="Test",
            body="Body",
            from_address="info@example.com",
            to_address="test@example.com",
            direction=MessageDirection.OUTBOUND,
            sent_at=now,
        )

        # Should not be eligible immediately
        assert not LeadService.should_send_followup(test_db, lead.id)

    def test_get_leads_for_followup(self, test_db):
        """Test retrieving leads eligible for follow-up."""
        # Create some leads
        for i in range(3):
            lead_data = LeadCreate(email=f"lead{i}@example.com")
            LeadService.create_lead(test_db, lead_data)

        # Should be eligible for follow-up
        leads = LeadService.get_leads_for_followup(test_db)
        assert len(leads) >= 3


class TestUnsubscribeDetection:
    """Tests for unsubscribe detection."""

    def test_detect_unsubscribe(self):
        """Test detection of unsubscribe keywords."""
        ai = AIDecisionEngine()

        texts = [
            "Please unsubscribe me from your emails",
            "Stop emailing me",
            "Remove me from your list",
            "Do not contact me again",
        ]

        for text in texts:
            assert ai.detect_unsubscribe(text), f"Failed to detect: {text}"

    def test_no_false_positive_unsubscribe(self):
        """Test that normal text doesn't trigger unsubscribe."""
        ai = AIDecisionEngine()

        text = "I'm not interested in your product"
        assert not ai.detect_unsubscribe(text)


class TestEscalationDetection:
    """Tests for escalation keyword detection."""

    def test_detect_escalation(self):
        """Test detection of escalation keywords."""
        ai = AIDecisionEngine()

        texts = [
            "I want a refund immediately",
            "This is a complaint about your service",
            "I'm considering legal action",
            "Your product is dangerous",
        ]

        for text in texts:
            reason = ai.extract_escalation_keywords(text)
            assert reason is not None, f"Failed to detect escalation: {text}"

    def test_no_false_positive_escalation(self):
        """Test that normal text doesn't trigger escalation."""
        ai = AIDecisionEngine()

        text = "I'm interested in learning more about your service"
        assert ai.extract_escalation_keywords(text) is None


class TestLeadStatusTransitions:
    """Tests for lead status transitions."""

    def test_status_new_to_contacted(self, test_db):
        """Test transition from NEW to CONTACTED."""
        lead_data = LeadCreate(email="test@example.com")
        lead = LeadService.create_lead(test_db, lead_data)

        assert lead.status == LeadStatus.NEW

        # Send an outbound message
        LeadService.add_message(
            test_db,
            lead.id,
            subject="Test",
            body="Body",
            from_address="info@example.com",
            to_address="test@example.com",
            direction=MessageDirection.OUTBOUND,
        )

        lead = LeadService.get_lead(test_db, lead.id)
        assert lead.status == LeadStatus.CONTACTED

    def test_status_to_replied(self, test_db):
        """Test transition to REPLIED when customer responds."""
        lead_data = LeadCreate(email="test@example.com")
        lead = LeadService.create_lead(test_db, lead_data)

        # Add inbound message
        LeadService.add_message(
            test_db,
            lead.id,
            subject="Re: Test",
            body="Customer reply",
            from_address="test@example.com",
            to_address="info@example.com",
            direction=MessageDirection.INBOUND,
        )

        lead = LeadService.get_lead(test_db, lead.id)
        assert lead.status == LeadStatus.REPLIED


class TestMessageStorage:
    """Tests for message storage."""

    def test_message_timestamps(self, test_db):
        """Test that message timestamps are stored correctly."""
        lead_data = LeadCreate(email="test@example.com")
        lead = LeadService.create_lead(test_db, lead_data)

        sent_time = datetime.utcnow()
        message = LeadService.add_message(
            test_db,
            lead.id,
            subject="Test",
            body="Body",
            from_address="test@example.com",
            to_address="info@example.com",
            direction=MessageDirection.INBOUND,
            sent_at=sent_time,
        )

        assert message.sent_at == sent_time

    def test_full_conversation_retrieval(self, test_db):
        """Test retrieving full conversation history."""
        lead_data = LeadCreate(email="test@example.com")
        lead = LeadService.create_lead(test_db, lead_data)

        # Add multiple messages
        for i in range(5):
            LeadService.add_message(
                test_db,
                lead.id,
                subject=f"Message {i}",
                body=f"Body {i}",
                from_address="test@example.com" if i % 2 == 0 else "info@example.com",
                to_address="info@example.com" if i % 2 == 0 else "test@example.com",
                direction=MessageDirection.INBOUND if i % 2 == 0 else MessageDirection.OUTBOUND,
            )

        messages = LeadService.get_recent_messages(test_db, lead.id)
        assert len(messages) == 5


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
