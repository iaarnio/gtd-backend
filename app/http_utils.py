"""
HTTP utilities for resilient API calls with retry logic and circuit breaker.

This module provides:
- @retry_with_backoff: Decorator for exponential backoff retries
- CircuitBreaker: Prevents hammering failing services
"""

import functools
import logging
import time
from typing import Any, Callable, Dict, Optional, TypeVar

import requests

logger = logging.getLogger(__name__)

F = TypeVar("F", bound=Callable[..., Any])


class CircuitBreaker:
    """
    Circuit breaker to prevent hammering failing services.

    States:
    - CLOSED: Normal operation, calls allowed
    - OPEN: Service failing, calls blocked for TIMEOUT seconds
    - HALF_OPEN: Testing if service recovered, limited calls allowed
    """

    def __init__(
        self,
        name: str,
        failure_threshold: int = 5,
        recovery_timeout: int = 60,
    ):
        """
        Initialize circuit breaker.

        Args:
            name: Name of the circuit (for logging)
            failure_threshold: Number of failures before opening circuit
            recovery_timeout: Seconds to wait before attempting recovery
        """
        self.name = name
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout

        self.failure_count = 0
        self.last_failure_time: Optional[float] = None
        self.state = "CLOSED"  # CLOSED, OPEN, HALF_OPEN

    def call(self, func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        """
        Execute function through circuit breaker.

        Args:
            func: Function to call
            *args: Positional arguments for func
            **kwargs: Keyword arguments for func

        Returns:
            Result of func() if successful

        Raises:
            RuntimeError: If circuit is OPEN
            Any exception from func: If call fails
        """
        if self.state == "OPEN":
            if self._should_attempt_recovery():
                self.state = "HALF_OPEN"
                logger.warning(
                    f"Circuit breaker '{self.name}' transitioning to HALF_OPEN",
                    extra={"component": "http", "error_type": "circuit_breaker"}
                )
            else:
                raise RuntimeError(
                    f"Circuit breaker '{self.name}' is OPEN. Service unavailable."
                )

        try:
            result = func(*args, **kwargs)
            self._on_success()
            return result
        except Exception as e:
            self._on_failure()
            raise

    def _should_attempt_recovery(self) -> bool:
        """Check if enough time has passed to attempt recovery."""
        if self.last_failure_time is None:
            return False
        return time.time() - self.last_failure_time >= self.recovery_timeout

    def _on_success(self) -> None:
        """Handle successful call."""
        if self.state == "HALF_OPEN":
            logger.info(
                f"Circuit breaker '{self.name}' recovering: call succeeded",
                extra={"component": "http", "error_type": "circuit_breaker"}
            )
        self.failure_count = 0
        self.state = "CLOSED"

    def _on_failure(self) -> None:
        """Handle failed call."""
        self.failure_count += 1
        self.last_failure_time = time.time()

        if self.failure_count >= self.failure_threshold:
            self.state = "OPEN"
            logger.error(
                f"Circuit breaker '{self.name}' OPEN after {self.failure_count} failures",
                extra={
                    "component": "http",
                    "error_type": "circuit_breaker",
                    "failure_count": self.failure_count,
                },
            )
        else:
            logger.warning(
                f"Circuit breaker '{self.name}': failure {self.failure_count}/{self.failure_threshold}",
                extra={
                    "component": "http",
                    "error_type": "circuit_breaker",
                    "failure_count": self.failure_count,
                },
            )


# Global circuit breakers for external services
_circuit_breakers: Dict[str, CircuitBreaker] = {}


def get_circuit_breaker(
    name: str,
    failure_threshold: int = 5,
    recovery_timeout: int = 60,
) -> CircuitBreaker:
    """Get or create a named circuit breaker."""
    if name not in _circuit_breakers:
        _circuit_breakers[name] = CircuitBreaker(
            name,
            failure_threshold=failure_threshold,
            recovery_timeout=recovery_timeout,
        )
    return _circuit_breakers[name]


def retry_with_backoff(
    max_retries: int = 3,
    initial_delay: float = 1.0,
    max_delay: float = 30.0,
    backoff_factor: float = 2.0,
    circuit_breaker: Optional[str] = None,
) -> Callable[[F], F]:
    """
    Decorator for exponential backoff retries on transient failures.

    Retryable errors:
    - Timeout (connection or read timeout)
    - HTTP 429 (rate limit)
    - HTTP 500-503 (server errors)

    Non-retryable errors:
    - HTTP 400-404 (client errors)
    - HTTP 401-403 (auth errors)

    Args:
        max_retries: Maximum number of retry attempts
        initial_delay: Initial delay between retries (seconds)
        max_delay: Maximum delay between retries (seconds)
        backoff_factor: Multiplier for delay after each retry
        circuit_breaker: Optional name of circuit breaker to use

    Returns:
        Decorated function with retry logic
    """

    def decorator(func: F) -> F:
        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            last_exception: Optional[Exception] = None
            cb = None

            if circuit_breaker:
                cb = get_circuit_breaker(circuit_breaker)

            for attempt in range(max_retries + 1):
                try:
                    if cb:
                        return cb.call(func, *args, **kwargs)
                    else:
                        return func(*args, **kwargs)

                except requests.Timeout as e:
                    last_exception = e
                    if attempt < max_retries:
                        delay = min(
                            initial_delay * (backoff_factor ** attempt),
                            max_delay,
                        )
                        logger.warning(
                            f"Request timeout, retrying in {delay:.1f}s (attempt {attempt + 1}/{max_retries})",
                            extra={
                                "component": "http",
                                "error_type": "timeout",
                                "attempt": attempt + 1,
                                "retry_count": max_retries,
                            },
                        )
                        time.sleep(delay)
                    else:
                        logger.error(
                            f"Request timeout after {max_retries} retries",
                            extra={
                                "component": "http",
                                "error_type": "timeout",
                                "attempt": attempt + 1,
                                "retry_count": max_retries,
                            },
                            exc_info=True,
                        )

                except requests.ConnectionError as e:
                    last_exception = e
                    if attempt < max_retries:
                        delay = min(
                            initial_delay * (backoff_factor ** attempt),
                            max_delay,
                        )
                        logger.warning(
                            f"Connection error, retrying in {delay:.1f}s (attempt {attempt + 1}/{max_retries})",
                            extra={
                                "component": "http",
                                "error_type": "connection_error",
                                "attempt": attempt + 1,
                                "retry_count": max_retries,
                            },
                        )
                        time.sleep(delay)
                    else:
                        logger.error(
                            f"Connection error after {max_retries} retries",
                            extra={
                                "component": "http",
                                "error_type": "connection_error",
                                "attempt": attempt + 1,
                                "retry_count": max_retries,
                            },
                            exc_info=True,
                        )

                except requests.HTTPError as e:
                    last_exception = e
                    status_code = e.response.status_code if e.response is not None else None

                    # Non-retryable HTTP errors
                    if status_code and 400 <= status_code < 500 and status_code not in [429]:
                        logger.error(
                            f"Non-retryable HTTP error {status_code}",
                            extra={
                                "component": "http",
                                "error_type": "http_client_error",
                                "http_status": status_code,
                            },
                            exc_info=True,
                        )
                        raise

                    # Retryable errors: 429, 500-503
                    if attempt < max_retries:
                        # Rate limit gets longer backoff
                        if status_code == 429:
                            delay = min(
                                initial_delay * (backoff_factor ** (attempt + 1)),
                                max_delay,
                            )
                            logger.warning(
                                f"Rate limited (HTTP 429), retrying in {delay:.1f}s (attempt {attempt + 1}/{max_retries})",
                                extra={
                                    "component": "http",
                                    "error_type": "rate_limited",
                                    "http_status": 429,
                                    "attempt": attempt + 1,
                                    "retry_count": max_retries,
                                },
                            )
                        else:
                            delay = min(
                                initial_delay * (backoff_factor ** attempt),
                                max_delay,
                            )
                            logger.warning(
                                f"Server error HTTP {status_code}, retrying in {delay:.1f}s (attempt {attempt + 1}/{max_retries})",
                                extra={
                                    "component": "http",
                                    "error_type": "server_error",
                                    "http_status": status_code,
                                    "attempt": attempt + 1,
                                    "retry_count": max_retries,
                                },
                            )
                        time.sleep(delay)
                    else:
                        logger.error(
                            f"HTTP error {status_code} after {max_retries} retries",
                            extra={
                                "component": "http",
                                "error_type": "http_error",
                                "http_status": status_code,
                                "attempt": attempt + 1,
                                "retry_count": max_retries,
                            },
                            exc_info=True,
                        )

                except Exception as e:
                    # Unknown error - don't retry
                    logger.error(
                        f"Unexpected error in HTTP call",
                        extra={
                            "component": "http",
                            "error_type": "unexpected_error",
                        },
                        exc_info=True,
                    )
                    raise

            # All retries exhausted
            if last_exception:
                raise last_exception
            raise RuntimeError("Unknown error in retry loop")

        return wrapper  # type: ignore

    return decorator
