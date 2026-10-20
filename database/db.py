import os
import logging
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from database.models import Base

from backend.config import settings

logger = logging.getLogger(__name__)

DATABASE_URL = settings.get_database_url()  # contains the password: never log it directly

# Configure engine connect args & pooling for SQLite vs Cloud SQL (PostgreSQL/MySQL)
connect_args = {}
engine_kwargs = {"pool_pre_ping": True}

if DATABASE_URL.startswith("sqlite"):
    connect_args = {"check_same_thread": False}
else:
    engine_kwargs.update({
        "pool_size": 5,
        "max_overflow": 10,
        "pool_timeout": 30,
    })

engine = create_engine(
    DATABASE_URL,
    connect_args=connect_args,
    **engine_kwargs
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

def safe_database_url() -> str:
    """The engine URL with the password masked, for logs and diagnostics."""
    return engine.url.render_as_string(hide_password=True)

def init_db():
    """
    Method 1: ORM Auto-Creation.
    Executes Base.metadata.create_all() directly in Python code.
    Creates 'tickets' and 'messages' tables in Cloud SQL or local database
    without requiring schema.sql files or GCS buckets!
    """
    logger.info(f"Initializing database tables via SQLAlchemy ORM on target: {safe_database_url()}")
    Base.metadata.create_all(bind=engine)
    logger.info("Database tables 'tickets' and 'messages' initialized successfully!")

def get_db():
    """FastAPI dependency for obtaining a database session."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    init_db()
