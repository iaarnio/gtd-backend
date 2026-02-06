import os
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker


"""
Database configuration and session management.

This module is intentionally small and explicit. It exposes:
- Base: declarative base for ORM models
- engine: SQLAlchemy engine bound to the SQLite file
- SessionLocal: factory for new sessions
- get_db: FastAPI dependency to provide a session per request
"""


def _get_database_path() -> Path:
    """
    Return the path to the SQLite database file.

    This mirrors the environment variable used by the runtime so that
    a single setting controls where data is stored.
    """
    default_path = "/app/data/gtd.db"
    db_path = os.environ.get("DATABASE_PATH", default_path)
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


DATABASE_PATH = _get_database_path()
DATABASE_URL = f"sqlite:///{DATABASE_PATH}"

# check_same_thread=False is required when using SQLite with FastAPI's
# dependency-injected sessions in a single process.
engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False},
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def get_db():
    """
    FastAPI dependency that yields a database session and ensures it is
    closed after the request is handled.
    """
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

