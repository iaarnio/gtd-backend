import os
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# Force test-safe DB path before importing app.db (which creates directories on import).
TEST_DB_PATH = Path(__file__).resolve().parent / "test.db"
os.environ.setdefault("DATABASE_PATH", str(TEST_DB_PATH))

from app.db import Base

# Ensure all model tables are registered on Base.metadata.
from app import models  # noqa: F401


def _build_session():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base.metadata.create_all(bind=engine)
    return TestingSessionLocal()


def _db_session_generator():
    # This function is wrapped as a fixture below to keep mypy/linters simple.
    db = _build_session()
    try:
        yield db
    finally:
        db.close()


# Expose as pytest fixture without importing pytest at module top-level in app code.
import pytest  # noqa: E402


@pytest.fixture
def db_session():
    yield from _db_session_generator()
