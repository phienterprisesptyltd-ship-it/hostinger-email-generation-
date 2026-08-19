"""AI decision engine using Anthropic Claude."""
import json
import logging
from typing import Optional, Dict, Any, List
from datetime import datetime
from anthropic import Anthropic
from config import settings
from models import LeadStatus, AIAction, Message, Lead

logger = logging.getLogger(__name__)


class AIDecisionEngine:
    """AI service for making lead follow-up decisions."""

    def __init__(self):
        self.client = Anthropic(api_key=settings.anthropic_api_key)
        self.model = "claude-opus-4-1-20250805"

    def _prepare_conversation_summary(
        self,
        lead: Lead,
        recent_messages: List[Message],
        max_history: int = 10,
    ) -> str:
        """Prepare a summary of the conversation for context."""
        summary = f"Lead: {lead.full_name or 'Unknown'}\n"
        summary += f"Email: {lead.email}\n"
        summary += f"Status: {lead.status.value}\n"
        summary += f"Source: {lead.lead_source or 'Unknown'}\n"
        summary += f"Follow-ups sent: {lead.followup_count}\n"
        summary += f"Last contact: {lead.last_outbound_date or 'Never'}\n\n"

        summary += "Recent conversation:\n"
        for msg in recent_messages[-max_history:]:
            direction = "→ TO" if msg.direction.value == "outbound" else "← FROM"
            timestamp = msg.sent_at.strftime("%Y-%m-%d %H:%M")
            summary += f"[{timestamp}] {direction} {msg.from_address}\n"
            summary += f"Subject: {msg.subject or '(no subject)'}\n"
            summary += f"Body: {msg.body[:200]}...\n\n"

        return summary

    async def evaluate_lead(
        self,
        lead: Lead,
        recent_messages: List[Message],
        available_offers: Optional[List[Dict[str, Any]]] = None,
        business_rules: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Evaluate a lead and decide on next action.

        Args:
            lead: Lead object
            recent_messages: Recent messages in conversation
            available_offers: Approved offers available
            business_rules: Business rules configuration

        Returns:
            AI decision with action and reasoning
        """
        try:
            # Prepare context
            conversation_summary = self._prepare_conversation_summary(
                lead, recent_messages
            )

            offers_context = ""
            if available_offers:
                offers_context = "\n\nAvailable offers:\n"
                for offer in available_offers:
                    offers_context += f"- {offer.get('name')}: {offer.get('description')}\n"

            rules_context = ""
            if business_rules:
                rules_context = (
                    "\n\nBusiness rules:\n"
                    "- Be professional and helpful\n"
                    "- Never invent prices or guarantees\n"
                    "- Respect customer preferences\n"
                    "- Escalate complaints or unclear situations\n"
                )

            prompt = f"""You are an AI assistant helping manage sales leads for Synchrobuild.

{conversation_summary}
{offers_context}
{rules_context}

Based on the conversation history and lead status, determine the next action.

You MUST respond ONLY with a valid JSON object (no markdown, no explanation) in this exact format:
{{
    "action": "SEND_REPLY" | "SEND_FOLLOWUP" | "WAIT" | "MARK_INTERESTED" | "MARK_NOT_INTERESTED" | "MARK_DO_NOT_CONTACT" | "ESCALATE_TO_HUMAN" | "CLOSE_LEAD",
    "confidence": 0.0 to 1.0,
    "reasoning": "Short operational reason (max 200 chars)",
    "requires_human_approval": true | false,
    "email_draft": {{
        "subject": "Email subject if SEND_REPLY or SEND_FOLLOWUP",
        "body": "Plain text email body"
    }} or null,
    "recommended_next_followup": "ISO8601 datetime or null",
    "recommended_status": "new" | "contacted" | "replied" | "interested" | "qualified" | "follow_up" | "not_interested" | "do_not_contact" | "needs_human" | "closed"
}}

Guidelines:
1. Only include email_draft if action is SEND_REPLY or SEND_FOLLOWUP
2. If customer hasn't replied, typically recommend SEND_FOLLOWUP
3. If customer shows interest, recommend MARK_INTERESTED
4. If customer declines, recommend MARK_NOT_INTERESTED
5. If unsure or risky, recommend ESCALATE_TO_HUMAN
6. Be concise and professional in email drafts
7. DO NOT invent prices, discounts, or guarantees
8. DO NOT use data not provided in context
9. Confidence should reflect how certain you are about the action
10. Recommend human approval for anything unusual or high-value

Respond ONLY with the JSON object, no other text."""

            response = self.client.messages.create(
                model=self.model,
                max_tokens=1500,
                messages=[{"role": "user", "content": prompt}],
            )

            # Extract and parse response
            response_text = response.content[0].text.strip()

            # Handle potential markdown code blocks
            if response_text.startswith("```"):
                response_text = response_text.split("```")[1]
                if response_text.startswith("json"):
                    response_text = response_text[4:]
                response_text = response_text.strip()

            decision = json.loads(response_text)

            # Validate decision structure
            decision = self._validate_decision(decision)

            logger.info(f"AI decision for lead {lead.id}: {decision['action']}")
            return decision

        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse AI response: {str(e)}")
            return {
                "action": AIAction.ESCALATE_TO_HUMAN,
                "confidence": 0.0,
                "reasoning": "AI response parsing failed",
                "requires_human_approval": True,
                "email_draft": None,
                "recommended_next_followup": None,
                "recommended_status": LeadStatus.NEEDS_HUMAN,
            }

        except Exception as e:
            logger.error(f"AI decision error: {str(e)}")
            return {
                "action": AIAction.ESCALATE_TO_HUMAN,
                "confidence": 0.0,
                "reasoning": f"AI service error: {str(e)}",
                "requires_human_approval": True,
                "email_draft": None,
                "recommended_next_followup": None,
                "recommended_status": LeadStatus.NEEDS_HUMAN,
            }

    def _validate_decision(self, decision: Dict[str, Any]) -> Dict[str, Any]:
        """Validate and clean AI decision output."""
        # Ensure all required fields exist
        defaults = {
            "action": AIAction.WAIT,
            "confidence": 0.5,
            "reasoning": "Unable to determine action",
            "requires_human_approval": True,
            "email_draft": None,
            "recommended_next_followup": None,
            "recommended_status": None,
        }

        for key, default in defaults.items():
            if key not in decision or decision[key] is None:
                decision[key] = default

        # Validate action is valid enum
        try:
            action_str = decision["action"]
            if isinstance(action_str, str):
                decision["action"] = AIAction[action_str]
            elif isinstance(action_str, AIAction):
                pass
            else:
                decision["action"] = AIAction.WAIT
        except (KeyError, ValueError):
            decision["action"] = AIAction.WAIT

        # Validate confidence is between 0 and 1
        try:
            confidence = float(decision["confidence"])
            decision["confidence"] = max(0.0, min(1.0, confidence))
        except (ValueError, TypeError):
            decision["confidence"] = 0.5

        # Limit reasoning length
        if isinstance(decision["reasoning"], str):
            decision["reasoning"] = decision["reasoning"][:200]

        # Validate email_draft structure
        if decision.get("email_draft"):
            if not isinstance(decision["email_draft"], dict):
                decision["email_draft"] = None
            elif "subject" not in decision["email_draft"] or "body" not in decision[
                "email_draft"
            ]:
                decision["email_draft"] = None

        return decision

    def extract_escalation_keywords(self, text: str) -> Optional[str]:
        """
        Detect if text contains escalation keywords.

        Returns reason if escalation keyword found, None otherwise.
        """
        escalation_keywords = [
            "refund",
            "complaint",
            "angry",
            "lawsuit",
            "lawyer",
            "dispute",
            "broken",
            "scam",
            "fraud",
            "unsafe",
            "dangerous",
            "urgent",
            "emergency",
            "crisis",
        ]

        text_lower = text.lower()
        for keyword in escalation_keywords:
            if keyword in text_lower:
                return f"Escalation keyword detected: {keyword}"

        return None

    def detect_unsubscribe(self, text: str) -> bool:
        """Detect if text contains unsubscribe request."""
        unsubscribe_keywords = [
            "unsubscribe",
            "remove me",
            "stop emailing",
            "no more emails",
            "do not contact",
            "stop contacting",
        ]

        text_lower = text.lower()
        return any(keyword in text_lower for keyword in unsubscribe_keywords)
