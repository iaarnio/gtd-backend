import logging
import os
from datetime import datetime, timedelta
from typing import Optional, Tuple

from .db import SessionLocal
from .models import RtmAuth
from .rtm import auth_check_token, auth_get_frob, auth_get_token, is_configured

logger = logging.getLogger(__name__)

# How often to revalidate the token with RTM (in hours)
REVALIDATION_INTERVAL_HOURS = 24


def get_rtm_auth() -> Optional[RtmAuth]:
    """Get the current RTM auth record from DB."""
    db = SessionLocal()
    try:
        return db.query(RtmAuth).order_by(RtmAuth.id.desc()).first()
    finally:
        db.close()


def is_rtm_auth_valid() -> bool:
    """Check if RTM auth is currently valid (and recent)."""
    auth = get_rtm_auth()
    if not auth or not auth.auth_token:
        return False

    if auth.valid == "invalid":
        return False

    # If we haven't checked recently, revalidate
    if auth.last_checked_at is None or (
        datetime.utcnow() - auth.last_checked_at
    ) > timedelta(hours=REVALIDATION_INTERVAL_HOURS):
        ensure_valid_rtm_auth()
        # Refresh from DB
        auth = get_rtm_auth()

    return auth is not None and auth.valid == "valid"


def ensure_valid_rtm_auth() -> bool:
    """
    Verify the RTM token is valid by calling RTM's checkToken.
    Update DB with result.
    Returns True if token is valid.
    """
    auth = get_rtm_auth()
    if not auth or not auth.auth_token:
        logger.debug("No RTM auth token in DB, skipping validation")
        return False

    try:
        logger.debug("Checking RTM auth token validity")
        result = auth_check_token(auth.auth_token)

        # result should be {"stat": "ok", "auth": {"token": "...", "perms": "...", "user": {...}}}
        if result.get("stat") != "ok":
            logger.warning(
                f"RTM token validation failed: {result.get('err', {}).get('msg', 'unknown error')}"
            )
            _mark_auth_invalid(auth)
            return False

        auth_info = result.get("auth", {})
        user_info = auth_info.get("user", {})

        # Update auth record with validated info
        db = SessionLocal()
        try:
            auth.valid = "valid"
            auth.perms = auth_info.get("perms")
            auth.username = user_info.get("username")
            auth.user_id = user_info.get("id")
            auth.last_checked_at = datetime.utcnow()
            db.add(auth)
            db.commit()
            logger.info(
                f"RTM auth validated successfully for user: {auth.username}"
            )
            return True
        finally:
            db.close()

    except Exception as e:
        logger.error(f"Error validating RTM token: {e}", exc_info=True)
        return False


def _mark_auth_invalid(auth: RtmAuth) -> None:
    """Mark the auth token as invalid in the DB."""
    db = SessionLocal()
    try:
        auth.valid = "invalid"
        auth.last_checked_at = datetime.utcnow()
        db.add(auth)
        db.commit()
    finally:
        db.close()


def store_rtm_auth(
    auth_token: str, perms: str, username: str, user_id: str
) -> None:
    """Store a new RTM auth token and metadata in DB."""
    db = SessionLocal()
    try:
        auth = db.query(RtmAuth).first()
        if auth:
            # Update existing record
            auth.auth_token = auth_token
            auth.perms = perms
            auth.username = username
            auth.user_id = user_id
            auth.valid = "valid"
            auth.last_checked_at = datetime.utcnow()
        else:
            # Create new record
            auth = RtmAuth(
                auth_token=auth_token,
                perms=perms,
                username=username,
                user_id=user_id,
                valid="valid",
                last_checked_at=datetime.utcnow(),
            )
        db.add(auth)
        db.commit()
        logger.info(f"RTM auth stored for user: {username}")
    finally:
        db.close()


def bootstrap_rtm_auth_from_env() -> None:
    """
    On first startup, if RTM credentials are in .env, move them to DB.
    This is a one-time migration to get off .env-based config.
    """
    db = SessionLocal()
    try:
        existing = db.query(RtmAuth).first()
        if existing:
            # Already migrated
            return

        token = os.environ.get("RTM_AUTH_TOKEN")
        if not token:
            logger.debug("No RTM_AUTH_TOKEN in .env, skipping bootstrap")
            return

        logger.info("Bootstrapping RTM auth from .env to DB")
        auth = RtmAuth(
            auth_token=token,
            valid="unknown",  # Will be validated on first use
        )
        db.add(auth)
        db.commit()
        logger.info("RTM auth bootstrapped to DB from .env")
    finally:
        db.close()
