"""
Single-use Discord invites for invited users.

A shared discord.gg link in a wizard step can be copied and passed around. With a
bot token and a channel configured under Settings → General, Wizarr asks Discord
for a fresh invite that works once and expires, for each user who finishes an
invitation.
"""

import logging
import time

import requests

from app.models import Settings

API_BASE = "https://discord.com/api/v10"
TIMEOUT = 10
# Long enough for someone to install Discord and sign up, short enough that a
# forwarded link soon stops working.
INVITE_MAX_AGE = 24 * 60 * 60

__all__ = ["DiscordInviteError", "create_invite", "is_configured"]


class DiscordInviteError(Exception):
    """Raised when Discord refuses or cannot create an invite."""


def _config() -> tuple[str, str]:
    rows = {
        s.key: s.value
        for s in Settings.query.filter(
            Settings.key.in_(["discord_bot_token", "discord_invite_channel_id"])
        ).all()
    }
    return (
        (rows.get("discord_bot_token") or "").strip(),
        (rows.get("discord_invite_channel_id") or "").strip(),
    )


def is_configured() -> bool:
    token, channel_id = _config()
    return bool(token and channel_id)


def create_invite() -> tuple[str, float]:
    """Create a single-use invite and return its URL and expiry timestamp."""
    token, channel_id = _config()
    if not token or not channel_id:
        raise DiscordInviteError("Discord bot token or invite channel is not set")

    try:
        resp = requests.post(
            f"{API_BASE}/channels/{channel_id}/invites",
            headers={"Authorization": f"Bot {token}"},
            json={"max_age": INVITE_MAX_AGE, "max_uses": 1, "unique": True},
            timeout=TIMEOUT,
        )
    except requests.RequestException as exc:
        raise DiscordInviteError(f"Cannot reach Discord: {exc}") from exc

    if resp.status_code == 401:
        raise DiscordInviteError("Discord rejected the bot token")
    if resp.status_code == 403:
        raise DiscordInviteError(
            "The bot lacks the Create Invite permission on the invite channel"
        )
    if resp.status_code == 404:
        raise DiscordInviteError("Discord could not find the invite channel")
    if resp.status_code not in (200, 201):
        raise DiscordInviteError(f"Discord returned HTTP {resp.status_code}")

    code = resp.json().get("code")
    if not code:
        raise DiscordInviteError("Discord returned no invite code")

    logging.info("Created single-use Discord invite %s", code)
    return f"https://discord.gg/{code}", time.time() + INVITE_MAX_AGE
