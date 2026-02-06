import hashlib
import os
from typing import Any, Dict, Optional

import requests


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


def is_configured() -> bool:
    return bool(_get_env("RTM_API_KEY") and _get_env("RTM_SHARED_SECRET") and _get_env("RTM_AUTH_TOKEN"))


def _sign_params(shared_secret: str, params: Dict[str, str]) -> str:
    """
    RTM signing: md5(shared_secret + concatenated key/value pairs sorted by key).
    """
    pieces = [shared_secret]
    for key in sorted(params.keys()):
        pieces.append(f"{key}{params[key]}")
    raw = "".join(pieces).encode("utf-8")
    return hashlib.md5(raw).hexdigest()


def call(method: str, params: Dict[str, str], timeout_seconds: int = 20) -> Dict[str, Any]:
    api_key = _get_env("RTM_API_KEY")
    shared_secret = _get_env("RTM_SHARED_SECRET")
    auth_token = _get_env("RTM_AUTH_TOKEN")

    if not api_key or not shared_secret or not auth_token:
        raise RuntimeError("RTM is not configured (missing RTM_API_KEY / RTM_SHARED_SECRET / RTM_AUTH_TOKEN)")

    full: Dict[str, str] = {
        "method": method,
        "api_key": api_key,
        "auth_token": auth_token,
        "format": "json",
        **params,
    }
    full["api_sig"] = _sign_params(shared_secret, full)

    response = requests.get(RTM_API_BASE_URL, params=full, timeout=timeout_seconds)
    response.raise_for_status()
    return response.json()


def create_timeline() -> str:
    data = call("rtm.timelines.create", {})
    if data.get("rsp", {}).get("stat") != "ok":
        raise RuntimeError(f"RTM timeline creation failed: {data}")
    return data["rsp"]["timeline"]


def add_task(timeline: str, name: str) -> Dict[str, str]:
    """
    Create a new task via rtm.tasks.add.

    Returns dict with list_id, taskseries_id, task_id.
    """
    data = call("rtm.tasks.add", {"timeline": timeline, "name": name, "parse": "1"})
    rsp = data.get("rsp", {})
    if rsp.get("stat") != "ok":
        raise RuntimeError(f"RTM task add failed: {data}")

    task = rsp["list"]["taskseries"]["task"]
    return {
        "list_id": rsp["list"]["id"],
        "taskseries_id": rsp["list"]["taskseries"]["id"],
        "task_id": task["id"],
    }

