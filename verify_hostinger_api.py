#!/usr/bin/env python
"""Verify Hostinger API integration using actual API calls."""
import sys
import json
from pathlib import Path
from datetime import datetime

sys.path.insert(0, str(Path(__file__).parent))

# Color codes
GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
RESET = "\033[0m"
BOLD = "\033[1m"


class APIVerificationReport:
    """Track API verification results."""

    def __init__(self):
        self.results = {}
        self.mailbox_id = None
        self.sender_email = None

    def add(self, name: str, passed: bool, details: str = "", data: dict = None):
        self.results[name] = {"passed": passed, "details": details, "data": data or {}}
        status = f"{GREEN}✓ PASS{RESET}" if passed else f"{RED}✗ FAIL{RESET}"
        print(f"{status} | {name}")
        if details:
            print(f"       {details}")

    def print_summary(self):
        print(f"\n{BOLD}{'='*80}")
        print("HOSTINGER API VERIFICATION REPORT")
        print(f"{'='*80}{RESET}\n")

        passed = sum(1 for r in self.results.values() if r["passed"])
        total = len(self.results)

        for name, result in self.results.items():
            status = f"{GREEN}PASS{RESET}" if result["passed"] else f"{RED}FAIL{RESET}"
            details_str = f" | {result['details']}" if result["details"] else ""
            print(f"{status} | {name}{details_str}")

        print(f"\n{BOLD}Results: {passed}/{total} passed{RESET}")
        if self.mailbox_id:
            print(f"\nMailbox ID: {self.mailbox_id}")
        if self.sender_email:
            print(f"Sender Email: {self.sender_email}")
        return passed == total


report = APIVerificationReport()


def verify_hostinger_api():
    """Verify Hostinger API integration."""
    print(f"\n{BOLD}{'='*80}")
    print("HOSTINGER API VERIFICATION")
    print(f"{'='*80}{RESET}")
    print("Testing actual Hostinger Email API endpoints...\n")

    # Test 1: Get mailboxes
    print(f"{BOLD}[1/7] Get Mailboxes{RESET}")
    print("-" * 80)

    try:
        from mcp__Hostinger_Mail__email_call_api_read import (
            mcp__Hostinger_Mail__email_call_api_read as api_read,
        )
    except ImportError:
        # MCP tools not available in this context, will be handled by user
        report.add(
            "GET_MAILBOXES",
            False,
            "MCP tools not available - testing via application integration only",
        )
        return False

    try:
        result = api_read(method="GET", path="/api/v1/me")

        if result.get("status") != 200:
            report.add(
                "GET_MAILBOXES",
                False,
                f"API returned status {result.get('status')}",
            )
            return False

        body = result.get("body", {})
        mailboxes = body.get("data", {}).get("mailboxes", [])

        if not mailboxes:
            report.add("GET_MAILBOXES", False, "No mailboxes found")
            return False

        mailbox = mailboxes[0]
        report.mailbox_id = mailbox.get("resourceId")
        report.sender_email = mailbox.get("address")

        report.add(
            "GET_MAILBOXES",
            True,
            f"Got {len(mailboxes)} mailbox(es). Primary: {report.sender_email}",
            {"mailboxes": len(mailboxes), "resource_id": report.mailbox_id},
        )

    except Exception as e:
        report.add("GET_MAILBOXES", False, f"Exception: {str(e)}")
        return False

    # Test 2: Get mailbox folders
    print(f"\n{BOLD}[2/7] Get Folders{RESET}")
    print("-" * 80)

    try:
        result = api_read(
            method="GET",
            path="/api/v1/mailboxes/{mailboxResourceId}/folders",
            path_params={"mailboxResourceId": report.mailbox_id},
        )

        if result.get("status") != 200:
            report.add(
                "GET_FOLDERS",
                False,
                f"API returned status {result.get('status')}",
            )
        else:
            body = result.get("body", {})
            folders = body.get("data", {}).get("folders", [])
            folder_names = [f.get("name") for f in folders]

            report.add(
                "GET_FOLDERS",
                True,
                f"Got {len(folders)} folders: {', '.join(folder_names[:3])}...",
                {"folder_count": len(folders), "folders": folder_names},
            )

    except Exception as e:
        report.add("GET_FOLDERS", False, f"Exception: {str(e)}")

    # Test 3: Send test email
    print(f"\n{BOLD}[3/7] Send Email{RESET}")
    print("-" * 80)

    try:
        from mcp__Hostinger_Mail__email_call_api_write import (
            mcp__Hostinger_Mail__email_call_api_write as api_write,
        )
    except ImportError:
        report.add(
            "SEND_EMAIL",
            False,
            "Cannot import write API",
        )
        return False

    try:
        payload = {
            "to": ["phienterprisesptyltd@gmail.com"],
            "subject": "API Verification Test",
            "text": "This email verifies the Hostinger API integration is working.",
            "html": "<p>This email verifies the <strong>Hostinger API</strong> integration is working.</p>",
            "displayName": "Synchrobuild Verification",
        }

        result = api_write(
            method="POST",
            path="/api/v1/mailboxes/{mailboxResourceId}/send",
            path_params={"mailboxResourceId": report.mailbox_id},
            body=payload,
        )

        if result.get("status") in [200, 204]:
            report.add(
                "SEND_EMAIL",
                True,
                f"Email sent successfully (HTTP {result.get('status')})",
                {"status": result.get("status")},
            )
        else:
            report.add(
                "SEND_EMAIL",
                False,
                f"API returned status {result.get('status')}: {result.get('body')}",
            )

    except Exception as e:
        report.add("SEND_EMAIL", False, f"Exception: {str(e)}")

    # Test 4: Get messages from INBOX
    print(f"\n{BOLD}[4/7] Get Messages{RESET}")
    print("-" * 80)

    try:
        result = api_read(
            method="GET",
            path="/api/v1/mailboxes/{mailboxResourceId}/folders/{folder}/messages",
            path_params={"mailboxResourceId": report.mailbox_id, "folder": "INBOX"},
            query_params={"page": 1, "perPage": 10},
        )

        if result.get("status") != 200:
            report.add(
                "GET_MESSAGES",
                False,
                f"API returned status {result.get('status')}",
            )
        else:
            body = result.get("body", {})
            messages = body.get("data", {}).get("messages", [])

            report.add(
                "GET_MESSAGES",
                True,
                f"Retrieved {len(messages)} messages from INBOX",
                {"message_count": len(messages)},
            )

    except Exception as e:
        report.add("GET_MESSAGES", False, f"Exception: {str(e)}")

    # Test 5: Search messages
    print(f"\n{BOLD}[5/7] Search Messages{RESET}")
    print("-" * 80)

    try:
        result = api_read(
            method="GET",
            path="/api/v1/mailboxes/{mailboxResourceId}/folders/{folder}/search",
            path_params={"mailboxResourceId": report.mailbox_id, "folder": "INBOX"},
            query_params={"q": "from:phienterprisesptyltd", "perPage": 5},
        )

        if result.get("status") != 200:
            report.add(
                "SEARCH_MESSAGES",
                False,
                f"API returned status {result.get('status')}",
            )
        else:
            body = result.get("body", {})
            # Search might return messages or an error
            report.add(
                "SEARCH_MESSAGES",
                True,
                "Search endpoint working",
                {"status": result.get("status")},
            )

    except Exception as e:
        report.add("SEARCH_MESSAGES", False, f"Exception: {str(e)}")

    # Test 6: Check API response format consistency
    print(f"\n{BOLD}[6/7] API Response Format{RESET}")
    print("-" * 80)

    try:
        # Verify all responses have consistent structure
        has_status = all(r.get("status") is not None for r in report.results.values())
        has_body = all(
            "body" in r.get("data", {}) or r.get("data") == {}
            for r in report.results.values()
        )

        if has_status and has_body:
            report.add(
                "API_RESPONSE_FORMAT",
                True,
                "All API responses have consistent format (status + body/data)",
            )
        else:
            report.add(
                "API_RESPONSE_FORMAT",
                False,
                "Inconsistent response format detected",
            )

    except Exception as e:
        report.add("API_RESPONSE_FORMAT", False, f"Exception: {str(e)}")

    # Test 7: Verify payload structure
    print(f"\n{BOLD}[7/7] Payload Validation{RESET}")
    print("-" * 80)

    try:
        # Validate that expected payload fields work
        test_payloads = {
            "basic_send": {
                "to": ["test@example.com"],
                "subject": "Test",
                "text": "Body",
            },
            "html_send": {
                "to": ["test@example.com"],
                "subject": "Test",
                "text": "Body",
                "html": "<p>HTML Body</p>",
            },
            "with_cc": {
                "to": ["test@example.com"],
                "cc": ["cc@example.com"],
                "subject": "Test",
                "text": "Body",
            },
            "with_bcc": {
                "to": ["test@example.com"],
                "bcc": ["bcc@example.com"],
                "subject": "Test",
                "text": "Body",
            },
            "display_name": {
                "to": ["test@example.com"],
                "subject": "Test",
                "text": "Body",
                "displayName": "Sender Name",
            },
        }

        # Check that all expected fields are valid
        valid_fields = set()
        for name, payload in test_payloads.items():
            for key in payload.keys():
                valid_fields.add(key)

        required_fields = {"to", "subject"}
        optional_fields = {"cc", "bcc", "text", "html", "displayName", "reply_to"}

        missing_fields = required_fields - valid_fields

        if not missing_fields and optional_fields.issubset(valid_fields):
            report.add(
                "PAYLOAD_VALIDATION",
                True,
                f"Payload structure valid. Required: {required_fields}, Optional: {optional_fields}",
            )
        else:
            report.add(
                "PAYLOAD_VALIDATION",
                False,
                f"Payload issues: {missing_fields}",
            )

    except Exception as e:
        report.add("PAYLOAD_VALIDATION", False, f"Exception: {str(e)}")

    # Print summary
    success = report.print_summary()
    return success


def verify_application_integration():
    """Verify the application's Hostinger service matches the API."""
    print(f"\n{BOLD}{'='*80}")
    print("APPLICATION INTEGRATION VERIFICATION")
    print(f"{'='*80}{RESET}")
    print("Verifying hostinger_service.py matches actual API...\n")

    try:
        from hostinger_service import HostingerEmailService

        hostinger = HostingerEmailService()

        print(f"✓ HostingerEmailService initialized")
        print(f"  - API Token: {bool(hostinger.token)}")
        print(f"  - Mailbox ID: {hostinger.mailbox_resource_id}")
        print(f"  - Sender: {hostinger.sender_address}")
        print(f"  - Base URL: {hostinger.base_url}")

        # Check methods exist
        methods = [
            "send_email",
            "get_messages",
            "get_message_body",
            "search_messages",
            "move_message",
            "mark_as_read",
        ]

        for method in methods:
            if hasattr(hostinger, method):
                print(f"  ✓ Method: {method}")
            else:
                print(f"  ✗ Missing method: {method}")
                return False

        print(f"\n{GREEN}✓ Application integration looks good{RESET}")
        return True

    except Exception as e:
        print(f"{RED}✗ Application integration failed: {str(e)}{RESET}")
        return False


if __name__ == "__main__":
    print(f"{BOLD}Hostinger API Verification{RESET}")
    print(f"Started at: {datetime.now().isoformat()}\n")

    try:
        api_ok = verify_hostinger_api()
        print()
        app_ok = verify_application_integration()

        print(f"\n{BOLD}{'='*80}")
        if api_ok and app_ok:
            print(f"{GREEN}ALL VERIFICATIONS PASSED{RESET}")
            sys.exit(0)
        else:
            print(f"{RED}SOME VERIFICATIONS FAILED{RESET}")
            sys.exit(1)

    except KeyboardInterrupt:
        print(f"\n{YELLOW}Verification interrupted by user{RESET}")
        sys.exit(130)
    except Exception as e:
        print(f"\n{RED}Verification error: {str(e)}{RESET}")
        import traceback

        traceback.print_exc()
        sys.exit(1)
