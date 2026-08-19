"""Hostinger Email API integration."""
import httpx
import logging
from typing import Optional, List, Dict, Any
from datetime import datetime
from config import settings

logger = logging.getLogger(__name__)

BASE_URL = "https://api.mail.hostinger.com"


class HostingerEmailService:
    """Service for interacting with Hostinger Email API."""

    def __init__(self):
        self.base_url = BASE_URL
        self.token = settings.hostinger_api_token
        self.mailbox_resource_id = settings.hostinger_mailbox_resource_id
        self.sender_address = settings.hostinger_sender_address

    def _get_headers(self) -> Dict[str, str]:
        """Get request headers with authorization."""
        return {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        }

    async def send_email(
        self,
        to_address: str,
        subject: str,
        body: str,
        html_body: Optional[str] = None,
        cc_addresses: Optional[List[str]] = None,
        bcc_addresses: Optional[List[str]] = None,
        reply_to: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Send an email using Hostinger API.

        Args:
            to_address: Recipient email address
            subject: Email subject
            body: Plain text body
            html_body: HTML body (optional)
            cc_addresses: CC recipients
            cc_addresses: BCC recipients
            reply_to: Reply-To address

        Returns:
            API response
        """
        try:
            payload = {
                "to": [to_address],
                "subject": subject,
                "text": body,
            }

            if html_body:
                payload["html"] = html_body

            if cc_addresses:
                payload["cc"] = cc_addresses

            if bcc_addresses:
                payload["bcc"] = bcc_addresses

            if reply_to:
                payload["reply_to"] = reply_to

            # Add display name
            payload["displayName"] = "Synchrobuild"

            url = f"{self.base_url}/api/v1/mailboxes/{self.mailbox_resource_id}/send"

            async with httpx.AsyncClient() as client:
                response = await client.post(
                    url,
                    json=payload,
                    headers=self._get_headers(),
                    timeout=30.0,
                )

            if response.status_code in [200, 204]:
                logger.info(f"Email sent successfully to {to_address}")
                return {"success": True, "status": response.status_code}
            else:
                logger.error(
                    f"Failed to send email to {to_address}: "
                    f"{response.status_code} - {response.text}"
                )
                return {
                    "success": False,
                    "status": response.status_code,
                    "error": response.text,
                }

        except Exception as e:
            logger.error(f"Error sending email: {str(e)}")
            return {"success": False, "error": str(e)}

    async def get_messages(
        self,
        folder: str = "INBOX",
        limit: int = 50,
        offset: int = 0,
    ) -> Dict[str, Any]:
        """
        Retrieve messages from a folder.

        Args:
            folder: Folder name (default: INBOX)
            limit: Number of messages to retrieve
            offset: Pagination offset

        Returns:
            List of messages
        """
        try:
            url = (
                f"{self.base_url}/api/v1/mailboxes/{self.mailbox_resource_id}/"
                f"folders/{folder}/messages"
            )

            async with httpx.AsyncClient() as client:
                response = await client.get(
                    url,
                    headers=self._get_headers(),
                    params={"page": offset // limit + 1, "perPage": limit},
                    timeout=30.0,
                )

            if response.status_code == 200:
                return response.json()
            else:
                logger.error(f"Failed to retrieve messages: {response.status_code}")
                return {"success": False, "status": response.status_code}

        except Exception as e:
            logger.error(f"Error retrieving messages: {str(e)}")
            return {"success": False, "error": str(e)}

    async def get_message_body(
        self,
        folder: str,
        uid: int,
    ) -> Dict[str, Any]:
        """
        Retrieve full message body by UID.

        Args:
            folder: Folder name
            uid: Message UID

        Returns:
            Message details including body
        """
        try:
            url = (
                f"{self.base_url}/api/v1/mailboxes/{self.mailbox_resource_id}/"
                f"folders/{folder}/messages/{uid}"
            )

            async with httpx.AsyncClient() as client:
                response = await client.get(
                    url,
                    headers=self._get_headers(),
                    timeout=30.0,
                )

            if response.status_code == 200:
                return response.json()
            else:
                logger.error(f"Failed to retrieve message body: {response.status_code}")
                return {"success": False, "status": response.status_code}

        except Exception as e:
            logger.error(f"Error retrieving message body: {str(e)}")
            return {"success": False, "error": str(e)}

    async def search_messages(
        self,
        query: str,
        folder: str = "INBOX",
        limit: int = 50,
    ) -> Dict[str, Any]:
        """
        Search messages in a folder.

        Args:
            query: Search query
            folder: Folder name
            limit: Number of results

        Returns:
            Search results
        """
        try:
            url = (
                f"{self.base_url}/api/v1/mailboxes/{self.mailbox_resource_id}/"
                f"folders/{folder}/search"
            )

            async with httpx.AsyncClient() as client:
                response = await client.get(
                    url,
                    headers=self._get_headers(),
                    params={"q": query, "perPage": limit},
                    timeout=30.0,
                )

            if response.status_code == 200:
                return response.json()
            else:
                logger.error(f"Failed to search messages: {response.status_code}")
                return {"success": False, "status": response.status_code}

        except Exception as e:
            logger.error(f"Error searching messages: {str(e)}")
            return {"success": False, "error": str(e)}

    async def move_message(
        self,
        folder: str,
        uid: int,
        target_folder: str,
    ) -> Dict[str, Any]:
        """
        Move a message to another folder.

        Args:
            folder: Source folder
            uid: Message UID
            target_folder: Target folder name

        Returns:
            API response
        """
        try:
            url = (
                f"{self.base_url}/api/v1/mailboxes/{self.mailbox_resource_id}/"
                f"folders/{folder}/messages/{uid}/move"
            )

            async with httpx.AsyncClient() as client:
                response = await client.put(
                    url,
                    json={"folder": target_folder},
                    headers=self._get_headers(),
                    timeout=30.0,
                )

            if response.status_code in [200, 204]:
                logger.info(f"Message moved successfully to {target_folder}")
                return {"success": True}
            else:
                logger.error(f"Failed to move message: {response.status_code}")
                return {"success": False, "status": response.status_code}

        except Exception as e:
            logger.error(f"Error moving message: {str(e)}")
            return {"success": False, "error": str(e)}

    async def mark_as_read(
        self,
        folder: str,
        uid: int,
    ) -> Dict[str, Any]:
        """Mark a message as read."""
        try:
            url = (
                f"{self.base_url}/api/v1/mailboxes/{self.mailbox_resource_id}/"
                f"folders/{folder}/messages/{uid}/flag"
            )

            async with httpx.AsyncClient() as client:
                response = await client.put(
                    url,
                    json={"flags": ["\\Seen"]},
                    headers=self._get_headers(),
                    timeout=30.0,
                )

            return {"success": response.status_code in [200, 204]}

        except Exception as e:
            logger.error(f"Error marking message as read: {str(e)}")
            return {"success": False, "error": str(e)}
