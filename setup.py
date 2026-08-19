#!/usr/bin/env python
"""Setup and initialization script."""
import os
import sys
from pathlib import Path

# Add current directory to path
sys.path.insert(0, str(Path(__file__).parent))

from database import init_db
from config import settings
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def setup():
    """Initialize the application."""
    logger.info("Setting up Synchrobuild Lead Follow-up System...")

    # Check environment
    if not os.path.exists(".env"):
        logger.warning(".env file not found. Copy .env.example to .env and configure.")
        logger.info("Creating .env from .env.example...")
        with open(".env.example", "r") as src:
            content = src.read()
        with open(".env", "w") as dst:
            dst.write(content)
        logger.info(".env created. Please edit it with your configuration.")

    # Initialize database
    logger.info("Creating database tables...")
    init_db()
    logger.info("Database initialized successfully!")

    # Test Hostinger connection
    from hostinger_service import HostingerEmailService

    hostinger = HostingerEmailService()
    logger.info(f"Hostinger configuration: {hostinger.sender_address}")
    logger.info("✓ Configuration loaded")

    # Test AI service
    from ai_service import AIDecisionEngine

    ai = AIDecisionEngine()
    logger.info(f"AI model: {ai.model}")
    logger.info("✓ AI service ready")

    logger.info("\n" + "=" * 60)
    logger.info("Setup complete!")
    logger.info("=" * 60)
    logger.info("\nNext steps:")
    logger.info("1. Edit .env with your configuration")
    logger.info("2. Run: python -m uvicorn main:app --reload")
    logger.info("3. Visit: http://localhost:8000")
    logger.info("4. Admin dashboard: http://localhost:8000/api/admin/dashboard")


if __name__ == "__main__":
    setup()
