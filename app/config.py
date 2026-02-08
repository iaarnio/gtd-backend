"""
Centralized configuration module for GTD backend.

This module provides a single source of truth for all configuration,
with environment variable overrides. All modules should import settings from here
rather than using hardcoded values or os.environ.get() calls.
"""

import os
from typing import Optional


class Config:
    """
    Centralized configuration with environment variable overrides.

    Environment variables can override any setting using uppercase name.
    Example: CLARIFY_RETRY_DELAYS would override clarify_retry_delays.
    """

    # ============================================================================
    # LOGGING CONFIGURATION
    # ============================================================================
    LOG_FORMAT: str = os.environ.get("LOG_FORMAT", "json")
    """
    Log format: "json" for production (Loki), "text" for local development.
    Environment: LOG_FORMAT
    """

    LOG_LEVEL: str = os.environ.get("LOG_LEVEL", "INFO")
    """
    Log level: DEBUG, INFO, WARNING, ERROR, CRITICAL.
    Environment: LOG_LEVEL
    """

    # ============================================================================
    # HTTP TIMEOUTS (seconds)
    # ============================================================================
    RTM_API_TIMEOUT: int = int(os.environ.get("RTM_API_TIMEOUT", "20"))
    """
    RTM API request timeout in seconds.
    Environment: RTM_API_TIMEOUT
    """

    LLM_API_TIMEOUT: int = int(os.environ.get("LLM_API_TIMEOUT", "30"))
    """
    LLM/OpenRouter API request timeout in seconds.
    Environment: LLM_API_TIMEOUT
    """

    IMAP_TIMEOUT: int = int(os.environ.get("IMAP_TIMEOUT", "30"))
    """
    IMAP connection timeout in seconds.
    Environment: IMAP_TIMEOUT
    """

    # ============================================================================
    # DATABASE CONFIGURATION
    # ============================================================================
    DB_LOCK_TIMEOUT: float = float(os.environ.get("DB_LOCK_TIMEOUT", "30.0"))
    """
    SQLite BUSY timeout in seconds. When database is locked, wait this long
    before raising an error.
    Environment: DB_LOCK_TIMEOUT
    """

    # ============================================================================
    # RETRY LIMITS (max number of attempts)
    # ============================================================================
    MAX_HTTP_RETRIES: int = int(os.environ.get("MAX_HTTP_RETRIES", "3"))
    """
    Maximum number of retries for HTTP calls (RTM, LLM).
    Retryable errors: Timeout, 429 (rate limit), 500-503 (server error).
    Non-retryable: 400-404 (client error), 401-403 (auth error).
    Environment: MAX_HTTP_RETRIES
    """

    MAX_CLARIFY_RETRIES: int = int(os.environ.get("MAX_CLARIFY_RETRIES", "5"))
    """
    Maximum number of clarification attempts for failed captures.
    After this, capture is marked permanently_failed.
    Environment: MAX_CLARIFY_RETRIES
    """

    MAX_COMMIT_RETRIES: int = int(os.environ.get("MAX_COMMIT_RETRIES", "5"))
    """
    Maximum number of RTM commit attempts for failed captures.
    After this, capture is marked permanently_failed.
    Environment: MAX_COMMIT_RETRIES
    """

    # ============================================================================
    # CIRCUIT BREAKER CONFIGURATION
    # ============================================================================
    CIRCUIT_BREAKER_FAILURE_THRESHOLD: int = int(
        os.environ.get("CIRCUIT_BREAKER_FAILURE_THRESHOLD", "5")
    )
    """
    Number of failures before circuit breaker opens (stops making requests).
    Environment: CIRCUIT_BREAKER_FAILURE_THRESHOLD
    """

    CIRCUIT_BREAKER_TIMEOUT: int = int(os.environ.get("CIRCUIT_BREAKER_TIMEOUT", "60"))
    """
    Seconds to wait in OPEN state before attempting recovery (HALF_OPEN state).
    Environment: CIRCUIT_BREAKER_TIMEOUT
    """

    # ============================================================================
    # POLLING INTERVALS (seconds)
    # ============================================================================
    EMAIL_POLL_INTERVAL: int = int(os.environ.get("EMAIL_POLL_INTERVAL", "60"))
    """
    Interval for checking IMAP for new emails in seconds.
    Environment: EMAIL_POLL_INTERVAL
    """

    CLARIFY_POLL_INTERVAL: int = int(os.environ.get("CLARIFY_POLL_INTERVAL", "30"))
    """
    Interval for checking for captures that need clarification in seconds.
    Environment: CLARIFY_POLL_INTERVAL
    """

    COMMIT_POLL_INTERVAL: int = int(os.environ.get("COMMIT_POLL_INTERVAL", "30"))
    """
    Interval for checking for captures that need RTM commit in seconds.
    Environment: COMMIT_POLL_INTERVAL
    """

    # ============================================================================
    # CLARIFICATION RETRY BACKOFF SCHEDULE
    # ============================================================================
    CLARIFY_RETRY_DELAYS: dict = {
        1: 0,           # 1st attempt: immediate
        2: 5 * 60,      # 2nd attempt: 5 minutes
        3: 30 * 60,     # 3rd attempt: 30 minutes
        4: 2 * 60 * 60, # 4th attempt: 2 hours
        # 5th+ attempt: permanently failed
    }
    """
    Exponential backoff schedule for failed clarifications.
    Keys are attempt numbers, values are delay in seconds before that attempt.
    """

    # ============================================================================
    # HELPER PROPERTIES
    # ============================================================================
    @property
    def http_retry_delays(self) -> dict:
        """
        Generate HTTP retry backoff schedule: 1s, 2s, 4s, 8s, 16s, max 30s.
        """
        return {
            1: 1,     # 1st retry: 1s
            2: 2,     # 2nd retry: 2s
            3: 4,     # 3rd retry: 4s
            4: 8,     # 4th retry: 8s
            5: 16,    # 5th retry: 16s
            6: 30,    # 6th+ retry: 30s (max)
        }

    @classmethod
    def get(cls, name: str, default: Optional[str] = None) -> str:
        """
        Get a configuration value by name, with environment override.

        Usage:
            config.get("LOG_FORMAT")  # Returns from LOG_FORMAT attribute
            config.get("CUSTOM_VAR")  # Returns from environment or default

        Args:
            name: Configuration key name (case-insensitive)
            default: Default value if not found

        Returns:
            Configuration value as string
        """
        # First try as attribute
        if hasattr(cls, name.upper()):
            return str(getattr(cls, name.upper()))

        # Then try environment variable
        return os.environ.get(name.upper(), default or "")

    @classmethod
    def to_dict(cls) -> dict:
        """
        Export all configuration as dictionary (redacted for logging).

        Returns:
            Dict with all configuration values, redacting sensitive keys.
        """
        config = {}
        sensitive_keys = ["API_KEY", "SECRET", "PASSWORD", "TOKEN", "AUTH"]

        for key in dir(cls):
            if key.startswith("_") or key[0].islower():
                continue
            value = getattr(cls, key)
            if callable(value):
                continue

            # Redact sensitive values
            if any(sensitive in key for sensitive in sensitive_keys):
                config[key] = "***REDACTED***"
            else:
                config[key] = value

        return config


# Create a singleton instance
config = Config()
