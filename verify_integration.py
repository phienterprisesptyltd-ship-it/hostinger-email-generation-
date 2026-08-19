#!/usr/bin/env python
"""Comprehensive integration testing and verification."""
import sys
import asyncio
import json
import hmac
import hashlib
from pathlib import Path
from datetime import datetime, timedelta

sys.path.insert(0, str(Path(__file__).parent))

from database import SessionLocal, init_db
from models import Lead, Message, LeadStatus, MessageDirection, AIDraft
from lead_service import LeadService
from hostinger_service import HostingerEmailService
from ai_service import AIDecisionEngine
from schemas import LeadCreate
from config import settings
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Color codes for output
GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
RESET = "\033[0m"
BOLD = "\033[1m"


class VerificationReport:
    """Track verification results."""

    def __init__(self):
        self.results = {}

    def add(self, name: str, passed: bool, details: str = ""):
        self.results[name] = {"passed": passed, "details": details}
        status = f"{GREEN}✓ PASS{RESET}" if passed else f"{RED}✗ FAIL{RESET}"
        print(f"{status} | {name}")
        if details:
            print(f"       {details}")

    def print_summary(self):
        print(f"\n{BOLD}{'='*80}")
        print("VERIFICATION REPORT")
        print(f"{'='*80}{RESET}\n")

        passed = sum(1 for r in self.results.values() if r["passed"])
        total = len(self.results)

        for name, result in self.results.items():
            status = f"{GREEN}PASS{RESET}" if result["passed"] else f"{RED}FAIL{RESET}"
            print(f"{status} | {name}")
            if result["details"]:
                print(f"      {result['details']}")

        print(f"\n{BOLD}Results: {passed}/{total} passed{RESET}")
        return passed == total


async def test_hostinger_send() -> bool:
    """Test 1: Send email via Hostinger."""
    print(f"\n{BOLD}[1/11] HOSTINGER SEND{RESET}")
    print("-" * 80)

    try:
        hostinger = HostingerEmailService()

        result = await hostinger.send_email(
            to_address=settings.test_email_recipient or "test@example.com",
            subject="Test Email from Synchrobuild",
            body="This is a test email to verify Hostinger integration.",
            html_body="<p>This is a <strong>test</strong> email.</p>",
        )

        if result["success"]:
            report.add(
                "HOSTINGER_SEND",
                True,
                f"Email sent successfully. Status: {result['status']}",
            )
            return True
        else:
            report.add(
                "HOSTINGER_SEND",
                False,
                f"Failed with error: {result.get('error', 'Unknown error')}",
            )
            return False

    except Exception as e:
        report.add("HOSTINGER_SEND", False, f"Exception: {str(e)}")
        return False


async def test_webhook_authentication() -> bool:
    """Test 2: Webhook authentication."""
    print(f"\n{BOLD}[2/11] WEBHOOK AUTHENTICATION{RESET}")
    print("-" * 80)

    try:
        from api_webhooks import verify_webhook_signature

        if not settings.hostinger_webhook_secret:
            report.add(
                "WEBHOOK_AUTHENTICATION",
                False,
                "HOSTINGER_WEBHOOK_SECRET not configured",
            )
            return False

        # Test 1: Valid signature
        payload = {"event": "message.received", "test": True}
        payload_bytes = json.dumps(payload).encode()

        expected_sig = hmac.new(
            settings.hostinger_webhook_secret.encode(),
            payload_bytes,
            hashlib.sha256,
        ).hexdigest()

        valid = verify_webhook_signature(payload_bytes, expected_sig)

        if not valid:
            report.add(
                "WEBHOOK_AUTHENTICATION", False, "Valid signature rejected"
            )
            return False

        # Test 2: Invalid signature should fail
        invalid_sig = "0" * 64
        invalid = verify_webhook_signature(payload_bytes, invalid_sig)

        if invalid:
            report.add(
                "WEBHOOK_AUTHENTICATION", False, "Invalid signature accepted"
            )
            return False

        # Test 3: Missing signature
        missing = verify_webhook_signature(payload_bytes, None)
        if missing:
            report.add(
                "WEBHOOK_AUTHENTICATION",
                False,
                "Missing signature accepted",
            )
            return False

        report.add(
            "WEBHOOK_AUTHENTICATION",
            True,
            "Valid signature accepted, invalid rejected, missing rejected",
        )
        return True

    except Exception as e:
        report.add("WEBHOOK_AUTHENTICATION", False, f"Exception: {str(e)}")
        return False


async def test_lead_creation_and_matching() -> bool:
    """Test 3: Lead creation and email matching."""
    print(f"\n{BOLD}[3/11] LEAD MATCHING{RESET}")
    print("-" * 80)

    db = SessionLocal()

    try:
        test_email = "verify-test@example.com"

        # Create lead
        lead_data = LeadCreate(
            email=test_email,
            full_name="Verification Test Lead",
            lead_source="test",
        )
        lead = LeadService.create_lead(db, lead_data)

        if not lead or not lead.id:
            report.add("LEAD_MATCHING", False, "Failed to create lead")
            return False

        # Test matching by email
        matched = LeadService.get_lead_by_email(db, test_email)

        if not matched or matched.id != lead.id:
            report.add("LEAD_MATCHING", False, "Failed to match lead by email")
            return False

        # Test status
        if matched.status != LeadStatus.NEW:
            report.add(
                "LEAD_MATCHING", False, f"Expected NEW status, got {matched.status}"
            )
            return False

        report.add(
            "LEAD_MATCHING",
            True,
            f"Lead created (ID: {lead.id}) and matched successfully",
        )
        return True

    except Exception as e:
        report.add("LEAD_MATCHING", False, f"Exception: {str(e)}")
        return False
    finally:
        db.close()


async def test_ai_draft_generation() -> bool:
    """Test 4: AI draft generation."""
    print(f"\n{BOLD}[4/11] AI DRAFT GENERATION{RESET}")
    print("-" * 80)

    db = SessionLocal()

    try:
        # Get or create a test lead
        lead = db.query(Lead).filter(Lead.email == "verify-test@example.com").first()

        if not lead:
            lead_data = LeadCreate(
                email="verify-test@example.com",
                full_name="AI Test Lead",
            )
            lead = LeadService.create_lead(db, lead_data)

        # Add initial message
        LeadService.add_message(
            db,
            lead.id,
            subject="Test Subject",
            body="I am interested in your services.",
            from_address="verify-test@example.com",
            to_address=settings.hostinger_sender_address,
            direction=MessageDirection.INBOUND,
        )

        # Get AI decision
        messages = LeadService.get_recent_messages(db, lead.id)
        ai_engine = AIDecisionEngine()

        decision = await ai_engine.evaluate_lead(lead, messages)

        if not decision or not decision.get("action"):
            report.add("AI_DRAFT_GENERATION", False, "No AI decision returned")
            return False

        if not decision.get("reasoning"):
            report.add("AI_DRAFT_GENERATION", False, "No reasoning provided")
            return False

        if decision.get("confidence") is None:
            report.add("AI_DRAFT_GENERATION", False, "No confidence score")
            return False

        report.add(
            "AI_DRAFT_GENERATION",
            True,
            f"AI decision: {decision['action'].value} (confidence: {decision['confidence']:.0%})",
        )
        return True

    except Exception as e:
        report.add("AI_DRAFT_GENERATION", False, f"Exception: {str(e)}")
        return False
    finally:
        db.close()


async def test_safe_mode() -> bool:
    """Test 5: Safe mode enforcement."""
    print(f"\n{BOLD}[5/11] SAFE MODE{RESET}")
    print("-" * 80)

    try:
        if settings.autonomous_mode:
            report.add(
                "SAFE_MODE",
                False,
                f"AUTONOMOUS_MODE is {settings.autonomous_mode}, expected False",
            )
            return False

        # Verify safe mode is in effect
        db = SessionLocal()
        lead = db.query(Lead).filter(Lead.email == "verify-test@example.com").first()
        db.close()

        if not lead:
            report.add("SAFE_MODE", False, "Test lead not found")
            return False

        report.add(
            "SAFE_MODE",
            True,
            f"AUTONOMOUS_MODE={settings.autonomous_mode} - Safe mode is ACTIVE",
        )
        return True

    except Exception as e:
        report.add("SAFE_MODE", False, f"Exception: {str(e)}")
        return False


async def test_unsubscribe_detection() -> bool:
    """Test 6: Unsubscribe handling."""
    print(f"\n{BOLD}[6/11] UNSUBSCRIBE{RESET}")
    print("-" * 80)

    db = SessionLocal()

    try:
        # Create test lead
        lead_data = LeadCreate(
            email="unsubscribe-test@example.com",
            full_name="Unsubscribe Test",
        )
        lead = LeadService.create_lead(db, lead_data)

        # Add unsubscribe message
        LeadService.add_message(
            db,
            lead.id,
            subject="Unsubscribe",
            body="Please unsubscribe me from your emails. Stop sending me anything.",
            from_address="unsubscribe-test@example.com",
            to_address=settings.hostinger_sender_address,
            direction=MessageDirection.INBOUND,
        )

        # Get messages and check AI detection
        messages = LeadService.get_recent_messages(db, lead.id)
        ai_engine = AIDecisionEngine()

        last_message = messages[0]
        detected = ai_engine.detect_unsubscribe(last_message.body)

        if not detected:
            report.add(
                "UNSUBSCRIBE", False, "Unsubscribe not detected in message body"
            )
            return False

        # Mark as do-not-contact
        LeadService.mark_unsubscribed(db, lead.id)

        lead = LeadService.get_lead(db, lead.id)

        if not lead.do_not_contact:
            report.add(
                "UNSUBSCRIBE", False, "Lead not marked do_not_contact"
            )
            return False

        if lead.status != LeadStatus.DO_NOT_CONTACT:
            report.add(
                "UNSUBSCRIBE",
                False,
                f"Status is {lead.status}, expected DO_NOT_CONTACT",
            )
            return False

        # Test eligibility
        eligible = LeadService.should_send_followup(db, lead.id)

        if eligible:
            report.add(
                "UNSUBSCRIBE",
                False,
                "Do-not-contact lead still eligible for followup",
            )
            return False

        report.add(
            "UNSUBSCRIBE",
            True,
            "Unsubscribe detected, lead marked, followup blocked",
        )
        return True

    except Exception as e:
        report.add("UNSUBSCRIBE", False, f"Exception: {str(e)}")
        return False
    finally:
        db.close()


async def test_duplicate_protection() -> bool:
    """Test 7: Duplicate message prevention."""
    print(f"\n{BOLD}[7/11] DUPLICATE PROTECTION{RESET}")
    print("-" * 80)

    db = SessionLocal()

    try:
        # Create test lead
        lead_data = LeadCreate(
            email="duplicate-test@example.com",
            full_name="Duplicate Test",
        )
        lead = LeadService.create_lead(db, lead_data)

        # Add message with external ID
        msg1 = LeadService.add_message(
            db,
            lead.id,
            subject="Test",
            body="Test message",
            from_address="duplicate-test@example.com",
            to_address=settings.hostinger_sender_address,
            direction=MessageDirection.INBOUND,
            external_message_id="msg-12345",
        )

        # Try to add exact duplicate
        msg2 = LeadService.add_message(
            db,
            lead.id,
            subject="Test",
            body="Test message",
            from_address="duplicate-test@example.com",
            to_address=settings.hostinger_sender_address,
            direction=MessageDirection.INBOUND,
            external_message_id="msg-12345",
        )

        if msg1.id != msg2.id:
            report.add(
                "DUPLICATE_PROTECTION", False, "Duplicate message not prevented"
            )
            return False

        # Verify count
        count = (
            db.query(Message)
            .filter(
                Message.lead_id == lead.id,
                Message.external_message_id == "msg-12345",
            )
            .count()
        )

        if count != 1:
            report.add(
                "DUPLICATE_PROTECTION",
                False,
                f"Expected 1 message, found {count}",
            )
            return False

        report.add(
            "DUPLICATE_PROTECTION", True, "Duplicate message correctly prevented"
        )
        return True

    except Exception as e:
        report.add("DUPLICATE_PROTECTION", False, f"Exception: {str(e)}")
        return False
    finally:
        db.close()


async def test_scheduler() -> bool:
    """Test 8: Scheduler (manual invocation)."""
    print(f"\n{BOLD}[8/11] SCHEDULER{RESET}")
    print("-" * 80)

    db = SessionLocal()

    try:
        # Create an eligible lead
        lead_data = LeadCreate(
            email="scheduler-test@example.com",
            full_name="Scheduler Test",
            lead_source="test",
        )
        lead = LeadService.create_lead(db, lead_data)

        # Add initial message
        LeadService.add_message(
            db,
            lead.id,
            subject="Initial Contact",
            body="I am interested in learning more.",
            from_address="scheduler-test@example.com",
            to_address=settings.hostinger_sender_address,
            direction=MessageDirection.INBOUND,
        )

        # Manually invoke worker
        from tasks import run_daily_followup_worker

        await run_daily_followup_worker()

        # Check if AI draft was created
        db2 = SessionLocal()
        drafts = db2.query(AIDraft).filter(AIDraft.lead_id == lead.id).all()
        db2.close()

        if not drafts:
            report.add(
                "SCHEDULER",
                False,
                "No AI draft created by scheduler",
            )
            return False

        # Verify draft was not sent in safe mode
        draft = drafts[0]
        if draft.is_executed:
            report.add(
                "SCHEDULER",
                False,
                "Draft was executed in safe mode",
            )
            return False

        report.add(
            "SCHEDULER",
            True,
            f"Scheduler created draft (ID: {draft.id}), not executed in safe mode",
        )
        return True

    except Exception as e:
        report.add("SCHEDULER", False, f"Exception: {str(e)}")
        return False
    finally:
        db.close()


async def test_automated_tests() -> bool:
    """Test 9: Run automated test suite."""
    print(f"\n{BOLD}[9/11] AUTOMATED TESTS{RESET}")
    print("-" * 80)

    try:
        import subprocess

        result = subprocess.run(
            ["python", "-m", "pytest", "tests.py", "-v", "--tb=short"],
            capture_output=True,
            text=True,
            timeout=60,
        )

        if result.returncode == 0:
            # Count passed tests
            passed = result.stdout.count(" PASSED")
            report.add(
                "AUTOMATED_TESTS",
                True,
                f"All tests passed ({passed} tests)",
            )
            return True
        else:
            # Get failure count
            failed = result.stdout.count(" FAILED")
            errors = result.stdout.count(" ERROR")
            report.add(
                "AUTOMATED_TESTS",
                False,
                f"Tests failed: {failed} failed, {errors} errors",
            )
            print("\nTest output:")
            print(result.stdout[-1000:])  # Last 1000 chars
            return False

    except subprocess.TimeoutExpired:
        report.add("AUTOMATED_TESTS", False, "Test suite timed out")
        return False
    except FileNotFoundError:
        report.add("AUTOMATED_TESTS", False, "pytest not installed")
        return False
    except Exception as e:
        report.add("AUTOMATED_TESTS", False, f"Exception: {str(e)}")
        return False


async def test_configuration() -> bool:
    """Test 10: Configuration validation."""
    print(f"\n{BOLD}[10/11] CONFIGURATION{RESET}")
    print("-" * 80)

    try:
        issues = []

        if not settings.hostinger_api_token:
            issues.append("HOSTINGER_API_TOKEN not set")
        if not settings.hostinger_mailbox_resource_id:
            issues.append("HOSTINGER_MAILBOX_RESOURCE_ID not set")
        if not settings.hostinger_sender_address:
            issues.append("HOSTINGER_SENDER_ADDRESS not set")
        if not settings.anthropic_api_key:
            issues.append("ANTHROPIC_API_KEY not set")
        if not settings.anthropic_model:
            issues.append("ANTHROPIC_MODEL not set")
        if not settings.database_url:
            issues.append("DATABASE_URL not set")

        if issues:
            report.add(
                "CONFIGURATION",
                False,
                f"Missing: {', '.join(issues)}",
            )
            return False

        report.add(
            "CONFIGURATION",
            True,
            f"All required settings configured. Model: {settings.anthropic_model}",
        )
        return True

    except Exception as e:
        report.add("CONFIGURATION", False, f"Exception: {str(e)}")
        return False


async def test_hostinger_retrieve() -> bool:
    """Test 11: Verify Hostinger can retrieve messages."""
    print(f"\n{BOLD}[11/11] HOSTINGER RECEIVE{RESET}")
    print("-" * 80)

    try:
        hostinger = HostingerEmailService()
        result = await hostinger.get_messages(folder="INBOX", limit=5)

        if "success" in result and not result["success"]:
            report.add(
                "HOSTINGER_RECEIVE",
                False,
                f"Failed to retrieve messages: {result.get('error')}",
            )
            return False

        # Check if we got a response
        if not result or ("data" not in result and "success" not in result):
            report.add(
                "HOSTINGER_RECEIVE",
                False,
                "Unexpected response format",
            )
            return False

        report.add(
            "HOSTINGER_RECEIVE",
            True,
            "Successfully retrieved messages from Hostinger",
        )
        return True

    except Exception as e:
        report.add("HOSTINGER_RECEIVE", False, f"Exception: {str(e)}")
        return False


async def main():
    """Run all verification tests."""
    global report
    report = VerificationReport()

    print(f"\n{BOLD}{'='*80}")
    print("SYNCHROBUILD INTEGRATION VERIFICATION")
    print(f"{'='*80}{RESET}")
    print(f"Environment: {settings.environment}")
    print(f"Autonomous Mode: {settings.autonomous_mode}")
    print(f"Hostinger Sender: {settings.hostinger_sender_address}")
    print(f"Test Recipient: {settings.test_email_recipient or 'Not configured'}")

    # Initialize database
    try:
        init_db()
        print(f"{GREEN}✓ Database initialized{RESET}")
    except Exception as e:
        print(f"{RED}✗ Database initialization failed: {str(e)}{RESET}")
        return False

    # Run tests
    print(f"\n{BOLD}Running Verification Tests...{RESET}\n")

    await test_configuration()
    await test_hostinger_send()
    await test_hostinger_retrieve()
    await test_webhook_authentication()
    await test_lead_creation_and_matching()
    await test_ai_draft_generation()
    await test_safe_mode()
    await test_duplicate_protection()
    await test_unsubscribe_detection()
    await test_scheduler()
    await test_automated_tests()

    # Print summary
    success = report.print_summary()

    return success


if __name__ == "__main__":
    success = asyncio.run(main())
    sys.exit(0 if success else 1)
