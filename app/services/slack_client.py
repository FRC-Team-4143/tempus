"""
Slack client helpers — DMs, group DMs, message updates.
"""
from typing import Optional
from slack_sdk.web.async_client import AsyncWebClient

from app.config import settings

_client: Optional[AsyncWebClient] = None


def get_slack_client() -> AsyncWebClient:
    global _client
    if _client is None:
        _client = AsyncWebClient(token=settings.slack_bot_token)
    return _client


async def send_dm(slack_user_id: str, text: str, blocks=None) -> Optional[str]:
    """
    Open a DM with a user and post a message.
    Returns the message ts or None on failure.
    """
    client = get_slack_client()
    try:
        conv = await client.conversations_open(users=slack_user_id)
        channel_id = conv["channel"]["id"]
        result = await client.chat_postMessage(
            channel=channel_id,
            text=text,
            blocks=blocks,
        )
        return result["ts"]
    except Exception:
        return None


async def send_group_dm(user_ids: list[str], text: str, blocks=None) -> Optional[str]:
    """
    Open a group DM with multiple users and post a message.
    Returns the message ts or None on failure.
    """
    client = get_slack_client()
    try:
        conv = await client.conversations_open(users=",".join(user_ids))
        channel_id = conv["channel"]["id"]
        result = await client.chat_postMessage(
            channel=channel_id,
            text=text,
            blocks=blocks,
        )
        return result["ts"]
    except Exception:
        return None


async def send_qr_dm(slack_user_id: str, code: str, name: str) -> bool:
    """Generate a QR code PNG for `code` and send it as a file DM to the user."""
    import io as _io
    import logging
    import qrcode
    log = logging.getLogger(__name__)

    img = qrcode.make(code)
    buf = _io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)

    client = get_slack_client()
    try:
        conv = await client.conversations_open(users=slack_user_id)
        channel_id = conv["channel"]["id"]
        await client.files_upload_v2(
            channel=channel_id,
            content=buf.read(),
            filename=f"{name.replace(' ', '_')}_qr.png",
            title=f"QR Badge — {name}",
            initial_comment=(
                f"Hi {name.split()[0]}! Here's your QR badge for the shop kiosk. "
                "Screenshot or save this and scan it to sign in and out."
            ),
        )
        return True
    except Exception as e:
        log.error("send_qr_dm failed for %s (%s): %s", name, slack_user_id, e)
        return False
