import hashlib
import logging
import os
from typing import Any, Dict, Optional

import requests

from .config import config
from .http_utils import retry_with_backoff

logger = logging.getLogger(__name__)


"""
Remember The Milk (RTM) API client.

This is intentionally small and explicit:
- signed REST calls (api_sig)
- JSON responses

Auth model:
- RTM_API_KEY
- RTM_SHARED_SECRET
- RTM_AUTH_TOKEN
"""


RTM_API_BASE_URL = "https://api.rememberthemilk.com/services/rest/"


def _get_env(name: str) -> Optional[str]:
    value = os.environ.get(name)
    return value

def _redact(value: str, keep: int = 4) -> str:
    """
    Redact a sensitive value but keep length and last chars.
    Example: abcdefgh -> ******efgh (len=8)
    """
    if value is None:
        return "<None>"
    if not isinstance(value, str):
        return f"<{type(value).__name__}>"
    if len(value) <= keep:
        return "*" * len(value)
    return "*" * (len(value) - keep) + value[-keep:]


def _safe_params_view(params: Dict[str, str]) -> Dict[str, str]:
    """
    Return a logging-safe view of RTM params.
    """
    out = {}
    for k, v in params.items():
        if k in {"api_key", "auth_token", "api_sig"}:
            out[k] = f"{_redact(v)} (len={len(v)})"
        else:
            out[k] = f"{v!r} (len={len(str(v))})"
    return out


def is_configured() -> bool:
    # Check if RTM API credentials are configured.
    # Auth token is now stored in database (after bootstrap from .env), not required here.
    return bool(_get_env("RTM_API_KEY") and _get_env("RTM_SHARED_SECRET"))


def _sign_params(shared_secret: str, params: Dict[str, str]) -> str:
    """
    RTM signing: md5(shared_secret + concatenated key/value pairs sorted by key).
    """
    pieces = [shared_secret]
    for key in sorted(params.keys()):
        pieces.append(f"{key}{params[key]}")
    raw = "".join(pieces).encode("utf-8")
    return hashlib.md5(raw).hexdigest()


@retry_with_backoff(max_retries=3, circuit_breaker="rtm_api")
def _call_rtm_api(
    method: str,
    params: Dict[str, str],
    timeout_seconds: int = 20,
) -> str:
    """
    Internal function that makes the actual HTTP request to RTM API.
    This is separated to allow the decorator to wrap only the network call.
    """
    api_key = _get_env("RTM_API_KEY")
    shared_secret = _get_env("RTM_SHARED_SECRET")

    base_params: Dict[str, str] = {
        "method": method,
        "api_key": api_key,
        **params,
    }

    api_sig = _sign_params(shared_secret, base_params)

    request_params = {
        **base_params,
        "api_sig": api_sig,
    }

    response = requests.get(
        RTM_API_BASE_URL,
        params=request_params,
        timeout=timeout_seconds,
    )
    response.raise_for_status()
    return response.text


def call(method: str, params: Dict[str, str], timeout_seconds: Optional[int] = None, auth_token: Optional[str] = None) -> Dict[str, Any]:
    api_key = _get_env("RTM_API_KEY")
    shared_secret = _get_env("RTM_SHARED_SECRET")

    # Use provided token, or fall back to environment variable
    if auth_token is None:
        auth_token = _get_env("RTM_AUTH_TOKEN")

    if not api_key or not shared_secret or not auth_token:
        raise RuntimeError("RTM is not configured")

    # Use provided timeout, or default from config
    if timeout_seconds is None:
        timeout_seconds = config.RTM_API_TIMEOUT

    base_params = {**params, "auth_token": auth_token}

    try:
        text = _call_rtm_api(method, base_params, timeout_seconds)
    except Exception as e:
        logger.error(
            f"RTM API call failed for method {method}",
            extra={
                "component": "rtm",
                "external_service": "rtm",
                "operation": method,
                "error_type": type(e).__name__,
            },
            exc_info=True,
        )
        raise

    # timelines.create returns XML
    if method == "rtm.timelines.create":
        import re
        m = re.search(r"<timeline>(\d+)</timeline>", text)
        if not m:
            raise RuntimeError(f"Failed to parse timeline: {text}")
        return {"timeline": m.group(1)}

    # tasks.add also returns XML
    return {"raw": text}


def create_timeline(auth_token: Optional[str] = None) -> str:
    data = call("rtm.timelines.create", {}, auth_token=auth_token)
    if "timeline" in data:
        return data["timeline"]

    raise RuntimeError(f"Unexpected response from rtm.timelines.create: {data}")


def add_task(timeline: str, name: str, auth_token: Optional[str] = None) -> Dict[str, str]:
    """
    Create a new task via rtm.tasks.add.

    Returns dict with list_id, taskseries_id, task_id.
    """
    data = call("rtm.tasks.add", {"timeline": timeline, "name": name, "parse": "1"}, auth_token=auth_token)

    # Parse XML response (call() returns {"raw": xml_text} for rtm.tasks.add)
    if "raw" in data:
        import xml.etree.ElementTree as ET
        root = ET.fromstring(data["raw"])
        if root.get("stat") != "ok":
            err = root.find("err")
            err_msg = err.get("msg") if err is not None else "Unknown error"
            raise RuntimeError(f"RTM task add failed: {err_msg}")

        list_elem = root.find("list")
        if list_elem is None:
            raise RuntimeError(f"RTM task add response missing list element: {data['raw']}")

        taskseries = list_elem.find("taskseries")
        if taskseries is None:
            raise RuntimeError(f"RTM task add response missing taskseries: {data['raw']}")

        task = taskseries.find("task")
        if task is None:
            raise RuntimeError(f"RTM task add response missing task: {data['raw']}")

        return {
            "list_id": list_elem.get("id"),
            "taskseries_id": taskseries.get("id"),
            "task_id": task.get("id"),
        }

    raise RuntimeError(f"Unexpected response from rtm.tasks.add: {data}")


@retry_with_backoff(max_retries=3, circuit_breaker="rtm_api")
def _check_token_http(token: str, api_key: str, shared_secret: str, timeout_seconds: Optional[int] = None) -> str:
    """Make HTTP request to check token."""
    if timeout_seconds is None:
        timeout_seconds = config.RTM_API_TIMEOUT

    base_params: Dict[str, str] = {
        "method": "rtm.auth.checkToken",
        "api_key": api_key,
        "auth_token": token,
    }

    api_sig = _sign_params(shared_secret, base_params)

    request_params = {
        **base_params,
        "api_sig": api_sig,
    }

    response = requests.get(
        RTM_API_BASE_URL,
        params=request_params,
        timeout=timeout_seconds,
    )
    response.raise_for_status()
    return response.text


def auth_check_token(token: str) -> Dict[str, Any]:
    """
    Verify that an auth token is valid and get user info.

    Returns the full RTM response dict.
    """
    api_key = _get_env("RTM_API_KEY")
    shared_secret = _get_env("RTM_SHARED_SECRET")

    if not api_key or not shared_secret:
        raise RuntimeError("RTM is not configured")

    try:
        response_text = _check_token_http(token, api_key, shared_secret)
    except Exception as e:
        logger.error(
            "Failed to check RTM token",
            extra={
                "component": "rtm",
                "external_service": "rtm",
                "operation": "auth_check_token",
                "error_type": type(e).__name__,
            },
            exc_info=True,
        )
        raise

    import xml.etree.ElementTree as ET
    root = ET.fromstring(response_text)

    # Convert XML to dict-like structure (root IS the <rsp> element)
    if root.get("stat") == "ok":
        auth = root.find("auth")
        if auth is not None:
            user = auth.find("user")
            return {
                "stat": "ok",
                "auth": {
                    "token": auth.findtext("token"),
                    "perms": auth.findtext("perms"),
                    "user": {
                        "id": user.get("id") if user is not None else None,
                        "username": user.findtext("username") if user is not None else None,
                    }
                }
            }

    # Extract error if present
    err = root.find("err")
    if err is not None:
        return {
            "stat": "fail",
            "err": {
                "code": err.get("code"),
                "msg": err.get("msg"),
            }
        }

    return {"stat": "fail", "err": {"msg": "Unknown error"}}


@retry_with_backoff(max_retries=3, circuit_breaker="rtm_api")
def _get_frob_http(api_key: str, shared_secret: str, timeout_seconds: Optional[int] = None) -> str:
    """Make HTTP request to get frob."""
    if timeout_seconds is None:
        timeout_seconds = config.RTM_API_TIMEOUT

    base_params: Dict[str, str] = {
        "method": "rtm.auth.getFrob",
        "api_key": api_key,
    }

    api_sig = _sign_params(shared_secret, base_params)

    request_params = {
        **base_params,
        "api_sig": api_sig,
    }

    response = requests.get(
        RTM_API_BASE_URL,
        params=request_params,
        timeout=timeout_seconds,
    )
    response.raise_for_status()
    return response.text


def auth_get_frob() -> str:
    """
    Get a frob for the RTM auth flow.

    Returns the frob string.
    """
    api_key = _get_env("RTM_API_KEY")
    shared_secret = _get_env("RTM_SHARED_SECRET")

    if not api_key or not shared_secret:
        raise RuntimeError("RTM is not configured")

    try:
        response_text = _get_frob_http(api_key, shared_secret)
    except Exception as e:
        logger.error(
            "Failed to get RTM frob",
            extra={
                "component": "rtm",
                "external_service": "rtm",
                "operation": "auth_get_frob",
                "error_type": type(e).__name__,
            },
            exc_info=True,
        )
        raise

    import xml.etree.ElementTree as ET
    root = ET.fromstring(response_text)
    frob = root.findtext("frob")

    if not frob:
        raise RuntimeError(f"Failed to get frob: {response_text}")

    return frob


@retry_with_backoff(max_retries=3, circuit_breaker="rtm_api")
def _get_token_http(frob: str, api_key: str, shared_secret: str, timeout_seconds: Optional[int] = None) -> str:
    """Make HTTP request to get token."""
    if timeout_seconds is None:
        timeout_seconds = config.RTM_API_TIMEOUT

    base_params: Dict[str, str] = {
        "method": "rtm.auth.getToken",
        "api_key": api_key,
        "frob": frob,
    }

    api_sig = _sign_params(shared_secret, base_params)

    request_params = {
        **base_params,
        "api_sig": api_sig,
    }

    response = requests.get(
        RTM_API_BASE_URL,
        params=request_params,
        timeout=timeout_seconds,
    )
    response.raise_for_status()
    return response.text


def auth_get_token(frob: str) -> Dict[str, Any]:
    """
    Exchange a frob for an auth token.

    Returns dict with token, perms, and user info.
    """
    api_key = _get_env("RTM_API_KEY")
    shared_secret = _get_env("RTM_SHARED_SECRET")

    if not api_key or not shared_secret:
        raise RuntimeError("RTM is not configured")

    try:
        response_text = _get_token_http(frob, api_key, shared_secret)
    except Exception as e:
        logger.error(
            "Failed to get RTM token",
            extra={
                "component": "rtm",
                "external_service": "rtm",
                "operation": "auth_get_token",
                "error_type": type(e).__name__,
            },
            exc_info=True,
        )
        raise

    import xml.etree.ElementTree as ET
    root = ET.fromstring(response_text)

    # Root IS the <rsp> element
    if root.get("stat") == "ok":
        auth = root.find("auth")
        if auth is not None:
            user = auth.find("user")
            return {
                "stat": "ok",
                "token": auth.findtext("token"),
                "perms": auth.findtext("perms"),
                "user": {
                    "id": user.get("id") if user is not None else None,
                    "username": user.findtext("username") if user is not None else None,
                }
            }

    # Extract error if present
    err = root.find("err")
    if err is not None:
        return {
            "stat": "fail",
            "err": {
                "code": err.get("code"),
                "msg": err.get("msg"),
            }
        }

    return {"stat": "fail", "err": {"msg": "Unknown error"}}

