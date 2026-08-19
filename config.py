from pydantic_settings import BaseSettings
from typing import Optional


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    # Database
    database_url: str

    # Hostinger
    hostinger_api_token: str
    hostinger_mailbox_resource_id: str
    hostinger_sender_address: str

    # Anthropic
    anthropic_api_key: str

    # App
    environment: str = "development"
    debug: bool = True
    secret_key: str = "dev-secret-key"
    log_level: str = "INFO"

    # Lead config
    business_timezone: str = "Australia/Sydney"
    daily_followup_hour: int = 8
    max_automated_followups: int = 5
    min_followup_hours: int = 24
    autonomous_mode: bool = False

    # Sending hours
    approved_sending_hours_start: int = 8
    approved_sending_hours_end: int = 18

    # Testing
    test_email_recipient: Optional[str] = None
    enable_test_mode: bool = True

    class Config:
        env_file = ".env"
        case_sensitive = False


settings = Settings()
