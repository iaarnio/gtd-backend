"""
Email notification module for RTM commit failures.

Uses SMTP to send notifications when RTM syncs fail after extended retries.

Configuration (environment variables):
  SMTP_HOST     SMTP server hostname (default: smtp.gmail.com)
  SMTP_PORT     SMTP port (default: 587, uses STARTTLS)
  SMTP_USER     SMTP login username (default: IMAP_USERNAME)
  SMTP_PASSWORD SMTP login password (default: IMAP_PASSWORD)
  NOTIFY_EMAIL  Destination address for notifications (default: SMTP_USER)
"""

import logging
import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import List, Optional

logger = logging.getLogger(__name__)


def _smtp_config():
    host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    port = int(os.environ.get("SMTP_PORT", "587"))
    user = os.environ.get("SMTP_USER") or os.environ.get("IMAP_USERNAME")
    password = os.environ.get("SMTP_PASSWORD") or os.environ.get("IMAP_PASSWORD")
    notify_to = os.environ.get("NOTIFY_EMAIL") or user
    return host, port, user, password, notify_to


def is_configured() -> bool:
    _, _, user, password, notify_to = _smtp_config()
    return bool(user and password and notify_to)


def send_rtm_failure_notification(captures, *, is_final: bool = False) -> bool:
    """
    Send an email listing all RTM-failed captures with full details.

    Args:
        captures: List of Capture ORM objects that failed RTM sync.
        is_final:  True when this is the final "gave up" notification
                   (24h with no success); False for the 2h warning.

    Returns:
        True if the email was sent successfully, False otherwise.
    """
    if not captures:
        return True

    host, port, user, password, notify_to = _smtp_config()
    if not (user and password and notify_to):
        logger.warning(
            "SMTP not configured — cannot send RTM failure notification. "
            "Set SMTP_USER/SMTP_PASSWORD (or IMAP_USERNAME/IMAP_PASSWORD) and NOTIFY_EMAIL.",
            extra={"component": "email_notify"},
        )
        return False

    count = len(captures)
    if is_final:
        subject = f"❌ RTM sync gave up on {count} task(s) — manual action needed"
    else:
        subject = f"⚠️ RTM sync failing for {count} task(s) — still retrying"

    # ── Plain-text body ───────────────────────────────────────────────────────
    lines: List[str] = []

    if is_final:
        lines += [
            f"RTM sync has been failing for over 24 hours on {count} task(s).",
            "Automatic retries have been exhausted. You can copy the tasks below",
            "directly into RTM if needed.",
        ]
    else:
        lines += [
            f"RTM sync has been failing for over 2 hours on {count} task(s).",
            "Retries are continuing automatically in the background.",
            "Tasks are listed below so you can paste them into RTM manually if urgent.",
        ]

    lines += ["", "=" * 60, ""]

    for c in captures:
        task_text = c.clarified_text or c.raw_text or "(no text)"
        lines.append(f"#{c.id}  {task_text}")
        lines.append(f"  Status   : {c.commit_status}")
        lines.append(f"  Attempts : {c.commit_attempt_count}")
        if c.commit_error_message:
            lines.append(f"  Error    : {c.commit_error_message}")
        if c.first_failed_at:
            lines.append(f"  First failed : {c.first_failed_at.strftime('%Y-%m-%d %H:%M')} UTC")
        if c.last_commit_attempt_at:
            lines.append(f"  Last tried   : {c.last_commit_attempt_at.strftime('%Y-%m-%d %H:%M')} UTC")
        lines.append("")

    lines += [
        "=" * 60,
        "",
        "Audit log (filter by failed status):",
        "  /audit-log?commit_status=failed",
        "  /audit-log?commit_status=permanently_failed",
    ]

    body = "\n".join(lines)

    # ── HTML body ─────────────────────────────────────────────────────────────
    html_rows = ""
    for c in captures:
        task_text = c.clarified_text or c.raw_text or "(no text)"
        error = c.commit_error_message or ""
        first_failed = (
            c.first_failed_at.strftime("%Y-%m-%d %H:%M UTC") if c.first_failed_at else "—"
        )
        last_tried = (
            c.last_commit_attempt_at.strftime("%Y-%m-%d %H:%M UTC")
            if c.last_commit_attempt_at
            else "—"
        )
        html_rows += f"""
        <tr>
          <td style="padding:6px 10px;border-bottom:1px solid #eee;font-weight:bold;">#{c.id}</td>
          <td style="padding:6px 10px;border-bottom:1px solid #eee;">{task_text}</td>
          <td style="padding:6px 10px;border-bottom:1px solid #eee;">{c.commit_status}</td>
          <td style="padding:6px 10px;border-bottom:1px solid #eee;">{c.commit_attempt_count}</td>
          <td style="padding:6px 10px;border-bottom:1px solid #eee;color:#721c24;">{error}</td>
          <td style="padding:6px 10px;border-bottom:1px solid #eee;font-size:0.85em;">{first_failed}</td>
          <td style="padding:6px 10px;border-bottom:1px solid #eee;font-size:0.85em;">{last_tried}</td>
        </tr>"""

    if is_final:
        header_color = "#dc3545"
        header_text = f"❌ RTM sync gave up — {count} task(s) need manual action"
        sub_text = "Automatic retries exhausted after 24 h. Copy tasks to RTM manually if needed."
    else:
        header_color = "#e0a800"
        header_text = f"⚠️ RTM sync failing — {count} task(s) affected"
        sub_text = "Retries are continuing automatically. Tasks listed for manual fallback."

    html = f"""
    <html><body style="font-family:sans-serif;color:#333;max-width:800px;margin:0 auto;">
      <h2 style="color:{header_color};">{header_text}</h2>
      <p>{sub_text}</p>
      <table style="border-collapse:collapse;width:100%;">
        <thead>
          <tr style="background:#f5f5f5;">
            <th style="padding:6px 10px;text-align:left;">ID</th>
            <th style="padding:6px 10px;text-align:left;">Task</th>
            <th style="padding:6px 10px;text-align:left;">Status</th>
            <th style="padding:6px 10px;text-align:left;">Attempts</th>
            <th style="padding:6px 10px;text-align:left;">Error</th>
            <th style="padding:6px 10px;text-align:left;">First failed</th>
            <th style="padding:6px 10px;text-align:left;">Last tried</th>
          </tr>
        </thead>
        <tbody>{html_rows}</tbody>
      </table>
      <p style="margin-top:1.5rem;font-size:0.85em;color:#666;">
        View in audit log:
        <a href="/audit-log?commit_status=failed">/audit-log?commit_status=failed</a> ·
        <a href="/audit-log?commit_status=permanently_failed">/audit-log?commit_status=permanently_failed</a>
      </p>
    </body></html>"""

    # ── Send ──────────────────────────────────────────────────────────────────
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = notify_to
    msg.attach(MIMEText(body, "plain", "utf-8"))
    msg.attach(MIMEText(html, "html", "utf-8"))

    try:
        with smtplib.SMTP(host, port, timeout=30) as smtp:
            smtp.ehlo()
            smtp.starttls()
            smtp.login(user, password)
            smtp.sendmail(user, [notify_to], msg.as_string())
        logger.info(
            "Sent RTM failure notification",
            extra={
                "component": "email_notify",
                "capture_count": count,
                "is_final": is_final,
                "notify_to": notify_to,
            },
        )
        return True
    except Exception as exc:
        logger.error(
            f"Failed to send RTM failure notification: {exc}",
            extra={
                "component": "email_notify",
                "error_type": type(exc).__name__,
            },
            exc_info=True,
        )
        return False
