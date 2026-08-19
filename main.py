"""FastAPI main application."""
import logging
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
from apscheduler.schedulers.background import BackgroundScheduler
import pytz
from datetime import datetime

from config import settings
from database import init_db
from api_leads import router as leads_router
from api_ai import router as ai_router
from api_admin import router as admin_router
from api_webhooks import router as webhooks_router
from tasks import run_daily_followup_worker

# Configure logging
logging.basicConfig(
    level=settings.log_level,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

# Scheduler for background tasks
scheduler: BackgroundScheduler = None


def start_scheduler():
    """Start the background job scheduler."""
    global scheduler

    if scheduler and scheduler.running:
        return

    scheduler = BackgroundScheduler(timezone=settings.business_timezone)

    # Schedule daily follow-up at 8:00 AM
    tz = pytz.timezone(settings.business_timezone)

    scheduler.add_job(
        run_daily_followup_worker,
        "cron",
        hour=settings.daily_followup_hour,
        minute=0,
        timezone=tz,
        id="daily_followup",
        name="Daily Follow-up Worker",
    )

    scheduler.start()
    logger.info(
        f"Scheduler started. Daily follow-up at {settings.daily_followup_hour}:00 "
        f"{settings.business_timezone}"
    )


def stop_scheduler():
    """Stop the background job scheduler."""
    global scheduler
    if scheduler and scheduler.running:
        scheduler.shutdown()
        logger.info("Scheduler stopped")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan context manager."""
    # Startup
    logger.info("Starting Synchrobuild Lead Follow-up System")
    init_db()
    logger.info("Database initialized")

    start_scheduler()
    logger.info(f"Environment: {settings.environment}")
    logger.info(f"Autonomous mode: {settings.autonomous_mode}")

    yield

    # Shutdown
    logger.info("Shutting down")
    stop_scheduler()


# Create FastAPI app
app = FastAPI(
    title="Synchrobuild Lead Follow-up",
    description="Autonomous lead follow-up system with AI decision engine",
    version="1.0.0",
    lifespan=lifespan,
)

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Include routers
app.include_router(leads_router)
app.include_router(ai_router)
app.include_router(admin_router)
app.include_router(webhooks_router)


@app.get("/")
async def root():
    """Health check endpoint."""
    return {
        "status": "ok",
        "service": "Synchrobuild Lead Follow-up",
        "version": "1.0.0",
        "environment": settings.environment,
        "autonomous_mode": settings.autonomous_mode,
        "scheduler_running": scheduler.running if scheduler else False,
    }


@app.get("/health")
async def health_check():
    """Health check for load balancers."""
    return {"status": "healthy"}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=8000,
        reload=settings.debug,
        log_level=settings.log_level.lower(),
    )
