"""Telegram delivery of the daily overdue summary.

Credentials come from the environment (set as GitHub Actions secrets):
``TELEGRAM_BOT_TOKEN`` and ``TELEGRAM_CHAT_ID``. If either is missing, or when
``dry_run`` is set, the message is printed instead of sent — so local runs and
CI smoke tests never require secrets.
"""

from __future__ import annotations

import logging
import os

from .config import MODEL
from .report import Report

logger = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"


def build_message(report: Report, site_url: str | None = None) -> str:
    """Compose the Telegram message body (HTML parse mode)."""
    if not report.rows:
        lines = [
            "<b>⚽ Overdue — Yellow Card Watch</b>",
            f"<i>{report.target_day}</i>",
            "",
            "No overdue players featuring today.",
        ]
        if site_url:
            lines += ["", f'<a href="{site_url}">View the page</a>']
        return "\n".join(lines)

    lines = [
        "<b>⚽ Overdue — Yellow Card Watch</b>",
        f"<i>{report.target_day}</i>",
        f"{len(report.rows)} overdue player(s) playing today:",
        "",
    ]
    for i, r in enumerate(report.rows[: MODEL.telegram_top_n], start=1):
        lines.append(
            f"{i}. <b>{r.player}</b> ({r.league_short}) — {r.team} v {r.opponent}\n"
            f"    {r.overdue_ratio:.1f}× overdue · {r.streak} games since last · "
            f"1 per {r.expected_interval:.1f}"
        )
    remaining = len(report.rows) - MODEL.telegram_top_n
    if remaining > 0:
        lines.append(f"\n…and {remaining} more.")
    lines.append("\n<i>Check Sky Bet / Bet365 for prices.</i>")
    if site_url:
        lines.append(f'<a href="{site_url}">Full list →</a>')
    return "\n".join(lines)


def send(report: Report, site_url: str | None = None, dry_run: bool = False) -> bool:
    """Send the summary to Telegram. Returns True if actually sent."""
    message = build_message(report, site_url)
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")

    if dry_run or not token or not chat_id:
        reason = "dry-run" if dry_run else "missing TELEGRAM_BOT_TOKEN/CHAT_ID"
        logger.info("Not sending Telegram message (%s). Preview:\n%s", reason, message)
        print(f"[telegram: {reason}]\n{message}")
        return False

    import requests

    resp = requests.post(
        TELEGRAM_API.format(token=token),
        json={
            "chat_id": chat_id,
            "text": message,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        },
        timeout=30,
    )
    if resp.status_code != 200:
        logger.error("Telegram send failed: %s %s", resp.status_code, resp.text)
        resp.raise_for_status()
    logger.info("Telegram message sent to chat %s.", chat_id)
    return True
