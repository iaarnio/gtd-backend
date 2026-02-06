import json
import os
import threading
import time
from typing import Any, Dict, Optional

from openai import OpenAI

from .db import SessionLocal
from .models import Capture


"""
LLM-based clarification engine.

Responsibilities:
- Find captures that are still in the proposed state and lack
  clarification.
- Call the OpenAI API once per capture attempt using a conservative
  Finnish GTD clarification prompt.
- Store the returned JSON verbatim in Capture.clarify_json.
"""


POLL_INTERVAL_SECONDS = 30


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
  - a single next action
  - a project (requires more than one step)
  - or non-actionable
- If it is a project, you MUST:
  - provide a clear project_name
  - provide the FIRST concrete next_action
  - provide a project_shortname: a short, uppercase identifier unique to this project (e.g. "KUVA", "GTD", "AUTO")
    - Should be 2-6 uppercase letters
    - Should be memorable and unique among projects
    - Derived from the project's core theme
- A next action must be a physical, visible action.
- If non-actionable, choose the most appropriate category.

When proposing a next action:
- It must be the very first doable step.
- It must be specific enough that it could be started immediately without further thinking.
- Next actions should start with a clear imperative verb.
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
- If type is "project" or "next_action", the suggested_tool MUST be "RTM".
- Obsidian is ONLY for non-actionable reference material or thinking notes.
- Never suggest Obsidian for anything that has a next action.

Confidence:
- confidence_score MUST be a float between 0.0 and 1.0
- Use high confidence ONLY when intent is very clear.

Return ONLY valid JSON matching this schema exactly:
{
  "type": "project" | "next_action" | "non_actionable",
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


def _get_client() -> Optional[OpenAI]:
    """
    Build an OpenAI client if an API key is configured.
    """
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        return None
    # The OpenAI client reads the key from environment by default.
    return OpenAI()


def _build_user_prompt(raw_text: str) -> str:
    return f"Raaka GTD-tiivistys alla. Analysoi ja palauta vain JSON yllä kuvatun skeeman mukaisesti.\n\n---\n\n{raw_text}"


def _clarify_capture(client: OpenAI, capture: Capture) -> Optional[str]:
    """
    Call the LLM once for this capture and return the JSON string, or
    None on failure.
    """
    model = os.environ.get("OPENAI_MODEL", "gpt-4.1-mini")
    try:
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": _build_user_prompt(capture.raw_text)},
            ],
            temperature=0.2,
        )
    except Exception:
        return None

    content = response.choices[0].message.content
    if not content:
        return None

    # Validate that the response is valid JSON and roughly matches the
    # expected structure. We store the original string to preserve the
    # model's output verbatim.
    try:
        data: Dict[str, Any] = json.loads(content)
    except json.JSONDecodeError:
        return None

    if "confidence_score" not in data:
        return None

    return content


def _poll_once() -> None:
    client = _get_client()
    if client is None:
        # Without an API key, clarification is simply disabled.
        return

    db = SessionLocal()
    try:
        # Select captures that are still proposed and have no
        # clarification stored yet.
        pending = (
            db.query(Capture)
            .filter(Capture.decision_status == "proposed", Capture.clarify_json.is_(None))
            .order_by(Capture.created_at.asc())
            .all()
        )

        for capture in pending:
            result = _clarify_capture(client, capture)
            if result is None:
                # Failure is acceptable; capture remains without
                # clarification and can be retried later.
                continue
            capture.clarify_json = result
            db.add(capture)
            db.commit()
    finally:
        db.close()


def run_clarification_loop() -> None:
    """
    Background loop that periodically attempts to clarify unprocessed
    captures.
    """
    while True:
        try:
            _poll_once()
        except Exception:
            # Failures should not crash the loop; they will be surfaced
            # by logs in a later hardening step.
            pass
        time.sleep(POLL_INTERVAL_SECONDS)


def start_background_clarifier() -> None:
    """
    Start the clarification loop in a daemon thread. Intended to be
    called from FastAPI startup.
    """
    thread = threading.Thread(target=run_clarification_loop, name="clarification-loop", daemon=True)
    thread.start()

