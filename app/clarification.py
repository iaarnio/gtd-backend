import json
import logging
import os
import threading
import time
from datetime import datetime
from typing import Any, Dict, Optional

import requests
from openai import OpenAI

from .config import config
from .db import SessionLocal
from .db_utils import transactional_session
from .http_utils import retry_with_backoff
from .models import Capture

logger = logging.getLogger(__name__)


"""
LLM-based clarification engine.

Responsibilities:
- Find captures that are still in the proposed state and lack
  clarification.
- Call the OpenAI API once per capture attempt using a conservative
  Finnish GTD clarification prompt.
- Store the returned JSON verbatim in Capture.clarify_json.
"""


# Import polling interval and retry limits from config
POLL_INTERVAL_SECONDS = config.CLARIFY_POLL_INTERVAL
MAX_CLARIFY_ATTEMPTS = config.MAX_CLARIFY_RETRIES
CLARIFY_RETRY_DELAYS = config.CLARIFY_RETRY_DELAYS


SYSTEM_PROMPT = """
You are a conservative GTD clarification assistant.

Your task is to interpret a raw capture and propose a GTD-consistent clarification.

Core principles:
- NEVER invent commitments, tasks, or goals.
- Be conservative. If unsure, lower confidence.
- Prefer "non-actionable" over guessing intent.
- Treat test messages, meta comments, or system checks as trash.

Language rules:
- The raw capture may be in English, Finnish, or mixed language.
- Regardless of input language, ALL generated output MUST be in Finnish.
- This includes:
  - clarified_text
  - project_name
  - next_action
  - suggested_context
  - ambiguities
- Use natural Finnish GTD phrasing.
- For projects, prefer noun-based outcome names (e.g. "Uuden auton hankinta").
- Avoid infinitive verb forms like "ostaa", "tehdä", "selvittää" in clarified_text.
- Use either:
  - noun form for projects, or
  - imperative form for actions.
- Do NOT preserve the original language in the output.

Classification rules:
- Decide whether the capture is:
  - a single standalone action (small, self-contained, no further steps needed)
  - a project (requires more than one step, has a larger goal)
  - or non-actionable
- Key distinction:
  - "Soita äidille" = standalone action (just one step, done)
  - "Osta maali" IN THE CONTEXT OF "remontoi asunto" = project (part of larger goal)
- If it is a project OR the capture is clearly part of a larger goal, you MUST:
  - provide a clear project_name (infer if needed from context)
  - provide a project_shortname: a short, uppercase identifier unique to this project (e.g. "KUVA", "GTD", "AUTO")
    - Should be 2-6 uppercase letters
    - Should be memorable and unique among projects
    - Derived from the project's core theme
  - provide the FIRST concrete next_action for that project
  - his next_action has naming format: PROJECT_SHORTNAME --- Task description (e.g. "KUVA --- Siirrä kuvat Macbookille")
- ONLY classify as standalone action if:
  - It's a single, self-contained action
  - No implicit larger goal or project context
  - Examples: call someone, write an email, file a document, buy one item
- A action must be a physical, visible action.
- If non-actionable, choose the most appropriate category.

When proposing an action:
- It must be the very first doable step.
- It must be specific enough that it could be started immediately without further thinking.
- Actions should start with a clear imperative verb.
- Prefer verbs from this list:
  Selvitä, Listaa, Etsi, Lue, Kirjoita, Soita, Lähetä, Osta, Hae, Vie, Täytä, Päivitä
- Avoid vague verbs like "suunnittele", "pohdi", "hoida".
- If a clear verb cannot be chosen without guessing, lower confidence.
- If you cannot make it concrete without guessing, lower the confidence score.

Rules for assigning a context:
- Assign a context ONLY if the action strictly requires being in that place.
- Do NOT assign a context for thinking, planning, listing, researching, or deciding.
- If an action could be done anywhere, leave suggested_context EMPTY.
- @asioilla is ONLY for actions that require physically going somewhere (e.g. pharmacy, gym, office).
- @kauppa is ONLY for shopping-related actions in stores.
- @pankki is ONLY for actions that must be done with a bank.
- @koti is ONLY for actions that must be done at home.
If unsure, DO NOT assign a context.

Tool selection rules:
- If type is "project" or "action", the suggested_tool MUST be "RTM".
- Obsidian is ONLY for non-actionable reference material or thinking notes.
- Never suggest Obsidian for anything that has an action or project.

Confidence:
- confidence_score MUST be a float between 0.0 and 1.0
- Use high confidence ONLY when intent is very clear.

Return ONLY valid JSON matching this schema exactly:
{
  "type": "project" | "action" | "next_action" | "non_actionable",
  "clarified_text": string,
  "project_name": string | null,
  "project_shortname": string | null,
  "next_action": string | null,
  "suggested_context": string | null,
  "due_date": string | null,
  "notes": string | null,
  "ambiguities": string | null,
  "suggested_tool": "RTM" | "Obsidian" | null,
  "confidence_score": float
}
"""


def _should_retry_clarification(capture: Capture, now: datetime) -> bool:
    """
    Check if a failed clarification should be retried now.

    Based on exponential backoff schedule:
    - Attempt 1: immediate (attempt_count=0)
    - Attempt 2: 5 minutes after failure
    - Attempt 3: 30 minutes after failure
    - Attempt 4: 2 hours after failure
    - Attempt 5+: permanently failed (no more retries)
    """
    if capture.clarify_status == "pending":
        # Never been attempted, try now
        return True

    if capture.clarify_status != "failed":
        # Not a failed capture, skip
        return False

    if capture.clarify_attempt_count >= MAX_CLARIFY_ATTEMPTS:
        # Max retries exceeded, mark as permanently failed
        return False

    if not capture.last_clarify_attempt_at:
        # Failed but no last attempt time recorded, try again
        return True

    # Get the delay for the next attempt (based on current attempt count)
    next_attempt_number = capture.clarify_attempt_count + 1
    delay_seconds = CLARIFY_RETRY_DELAYS.get(next_attempt_number, 0)

    # Check if enough time has passed
    time_since_failure = (now - capture.last_clarify_attempt_at).total_seconds()
    return time_since_failure >= delay_seconds


def _get_client() -> Optional[OpenAI]:
    """
    Build an OpenAI client if an API key is configured.
    """
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        return None

    # Get optional base URL override (for custom endpoints or proxies).
    # Only use if explicitly set to a non-empty value.
    base_url = os.environ.get("OPENAI_BASE_URL", "").strip()

    if base_url:
        logger.info(f"Using custom OpenAI base URL: {base_url}")
        return OpenAI(api_key=api_key, base_url=base_url)
    else:
        return OpenAI(api_key=api_key)


def _build_user_prompt(raw_text: str) -> str:
    return f"Raaka GTD-tiivistys alla. Analysoi ja palauta vain JSON yllä kuvatun skeeman mukaisesti.\n\n---\n\n{raw_text}"


@retry_with_backoff(max_retries=3, circuit_breaker="llm_api")
def _call_llm_api(api_key: str, base_url: str, model: str, user_prompt: str, capture_id: int) -> Dict[str, Any]:
    """
    Internal function that makes the actual HTTP request to LLM API.
    This is separated to allow the decorator to wrap only the network call.
    Returns the parsed response.
    """
    response = requests.post(
        f"{base_url}/chat/completions",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={
            "model": model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.2,
        },
        timeout=config.LLM_API_TIMEOUT,
    )
    response.raise_for_status()
    return response.json()


def _clarify_capture(api_key: str, base_url: str, capture: Capture) -> Optional[str]:
    """
    Call the LLM once for this capture and return the JSON string, or
    None on failure. Uses requests library directly for better OpenRouter compatibility.
    """
    model = os.environ.get("OPENAI_MODEL", "gpt-4.1-mini")
    user_prompt = _build_user_prompt(capture.raw_text)

    try:
        data = _call_llm_api(api_key, base_url, model, user_prompt, capture.id)
    except requests.exceptions.RequestException as e:
        logger.error(
            f"LLM API error for capture {capture.id}",
            extra={
                "component": "clarification",
                "external_service": "llm",
                "operation": "clarify_capture",
                "capture_id": capture.id,
                "error_type": type(e).__name__,
            },
            exc_info=True,
        )
        return None
    except Exception as e:
        logger.error(
            f"Unexpected error calling LLM API for capture {capture.id}",
            extra={
                "component": "clarification",
                "external_service": "llm",
                "operation": "clarify_capture",
                "capture_id": capture.id,
                "error_type": type(e).__name__,
            },
            exc_info=True,
        )
        return None

    try:
        data_dict = data if isinstance(data, dict) else {}
    except Exception as e:
        logger.error(
            f"Failed to parse LLM response for capture {capture.id}",
            extra={
                "component": "clarification",
                "error_type": "response_parse_error",
                "capture_id": capture.id,
            },
            exc_info=True,
        )
        return None

    if "error" in data_dict:
        logger.error(
            f"LLM API returned error for capture {capture.id}",
            extra={
                "component": "clarification",
                "external_service": "llm",
                "error_type": "llm_error",
                "capture_id": capture.id,
            },
        )
        return None

    try:
        content = data_dict["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as e:
        logger.error(
            f"Unexpected response structure from LLM for capture {capture.id}",
            extra={
                "component": "clarification",
                "error_type": "unexpected_structure",
                "capture_id": capture.id,
            },
            exc_info=True,
        )
        return None

    if not content:
        logger.warning(
            f"Empty response from LLM for capture {capture.id}",
            extra={
                "component": "clarification",
                "error_type": "empty_response",
                "capture_id": capture.id,
            },
        )
        return None

    # Validate that the response is valid JSON and roughly matches the expected structure.
    try:
        result_data: Dict[str, Any] = json.loads(content)
    except json.JSONDecodeError as e:
        logger.error(
            f"Invalid JSON in LLM response for capture {capture.id}",
            extra={
                "component": "clarification",
                "error_type": "invalid_json",
                "capture_id": capture.id,
            },
            exc_info=True,
        )
        return None

    if "confidence_score" not in result_data:
        logger.warning(
            f"Missing confidence_score in LLM response for capture {capture.id}",
            extra={
                "component": "clarification",
                "error_type": "missing_field",
                "capture_id": capture.id,
            },
        )
        return None

    return content


def _poll_once() -> None:
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        # Without an API key, clarification is simply disabled.
        logger.debug("OPENAI_API_KEY not configured, clarification disabled")
        return

    base_url = os.environ.get("OPENAI_BASE_URL", "").strip()
    if not base_url:
        logger.warning("OPENAI_BASE_URL not configured, clarification disabled")
        return

    db = SessionLocal()
    try:
        now = datetime.utcnow()

        # Select captures that need clarification:
        # - proposed status AND (pending clarification or failed with retry backoff elapsed)
        pending = (
            db.query(Capture)
            .filter(
                Capture.decision_status == "proposed",
                Capture.clarify_status.in_(["pending", "failed"])
            )
            .order_by(Capture.created_at.asc())
            .all()
        )
        logger.info(f"Clarification poll: found {len(pending)} captures to process")

        for capture in pending:
            # Check if this failed capture should be retried now
            if not _should_retry_clarification(capture, now):
                next_attempt = capture.clarify_attempt_count + 1
                delay = CLARIFY_RETRY_DELAYS.get(next_attempt, 0)
                logger.debug(
                    f"Capture {capture.id}: skipping (next attempt #{next_attempt} in {delay}s)",
                    extra={
                        "component": "clarification",
                        "operation": "poll",
                        "capture_id": capture.id,
                    },
                )
                continue

            # Mark as in_progress before attempting
            capture.clarify_status = "in_progress"
            capture.clarify_attempt_count += 1
            db.add(capture)
            with transactional_session(db):
                pass  # Context manager handles commit

            logger.info(
                f"Clarifying capture {capture.id} (attempt {capture.clarify_attempt_count}/{MAX_CLARIFY_ATTEMPTS}): {capture.raw_text[:50]}...",
                extra={
                    "component": "clarification",
                    "operation": "clarify",
                    "capture_id": capture.id,
                    "attempt": capture.clarify_attempt_count,
                },
            )

            result = _clarify_capture(api_key, base_url, capture)
            capture.last_clarify_attempt_at = now

            if result is None:
                # Clarification failed
                if capture.clarify_attempt_count >= MAX_CLARIFY_ATTEMPTS:
                    # Max retries exceeded
                    capture.clarify_status = "permanently_failed"
                    error_info = {
                        "type": "error",
                        "status": "clarification_permanently_failed",
                        "message": f"Failed to clarify after {MAX_CLARIFY_ATTEMPTS} attempts. Requires manual review.",
                        "requires_user_attention": True,
                        "attempts": capture.clarify_attempt_count,
                    }
                    capture.clarify_json = json.dumps(error_info, ensure_ascii=False)
                    logger.error(
                        f"Clarification permanently failed for capture {capture.id} after {MAX_CLARIFY_ATTEMPTS} attempts",
                        extra={
                            "component": "clarification",
                            "operation": "clarify",
                            "capture_id": capture.id,
                            "error_type": "permanently_failed",
                            "attempt": capture.clarify_attempt_count,
                        },
                    )
                else:
                    # Will retry later
                    capture.clarify_status = "failed"
                    error_info = {
                        "type": "error",
                        "status": "clarification_failed",
                        "message": f"Failed to clarify. Will retry (attempt {capture.clarify_attempt_count}/{MAX_CLARIFY_ATTEMPTS}).",
                        "requires_user_attention": False,
                        "attempts": capture.clarify_attempt_count,
                    }
                    capture.clarify_json = json.dumps(error_info, ensure_ascii=False)
                    logger.warning(
                        f"Clarification failed for capture {capture.id}, will retry later",
                        extra={
                            "component": "clarification",
                            "operation": "clarify",
                            "capture_id": capture.id,
                            "error_type": "clarification_failed",
                            "attempt": capture.clarify_attempt_count,
                            "retry_count": MAX_CLARIFY_ATTEMPTS,
                        },
                    )
            else:
                # Success
                capture.clarify_status = "completed"
                capture.clarify_json = result
                logger.info(
                    f"Successfully clarified capture {capture.id}",
                    extra={
                        "component": "clarification",
                        "operation": "clarify",
                        "capture_id": capture.id,
                        "attempt": capture.clarify_attempt_count,
                    },
                )

            db.add(capture)
            with transactional_session(db):
                pass  # Context manager handles commit

    finally:
        db.close()


def run_clarification_loop() -> None:
    """
    Background loop that periodically attempts to clarify unprocessed
    captures.
    """
    logger.info(f"Clarification loop started, polling every {POLL_INTERVAL_SECONDS} seconds")
    poll_count = 0
    while True:
        try:
            poll_count += 1
            logger.info(f"Clarification poll #{poll_count} starting...")
            _poll_once()
        except Exception as e:
            # Failures should not crash the loop; they will be surfaced
            # by logs in a later hardening step.
            logger.error(f"Error in clarification loop: {e}", exc_info=True)
        time.sleep(POLL_INTERVAL_SECONDS)


def start_background_clarifier() -> None:
    """
    Start the clarification loop in a daemon thread. Intended to be
    called from FastAPI startup.
    """
    logger.info("Starting background clarification loop")
    thread = threading.Thread(target=run_clarification_loop, name="clarification-loop", daemon=True)
    thread.start()
    logger.info("Clarification loop thread started")

