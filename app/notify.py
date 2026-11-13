"""Discord Webhook 通知。"""
from __future__ import annotations

import logging

import httpx

from .config import settings

log = logging.getLogger("patchdeck.notify")

COLORS = {"success": 0x2ECC71, "partial": 0xF1C40F, "failed": 0xE74C3C, "info": 0x7C5CFF}
STATUS_JA = {"success": "成功", "partial": "一部失敗", "failed": "失敗"}


async def send(title: str, description: str, color_key: str = "info", mention: bool = False) -> None:
    if not settings.discord_webhook_url:
        return
    if len(description) > 3900:
        description = description[:3900] + "\n…(省略)"
    embed = {"title": title[:250], "description": description, "color": COLORS.get(color_key, COLORS["info"])}
    if settings.base_url:
        embed["url"] = settings.base_url
    payload: dict = {"username": "PatchDeck", "embeds": [embed]}
    if mention and settings.discord_mention_user_id:
        payload["content"] = f"<@{settings.discord_mention_user_id}>"
        payload["allowed_mentions"] = {"users": [settings.discord_mention_user_id]}
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(settings.discord_webhook_url, json=payload)
            if r.status_code >= 300:
                log.warning("Discord 通知失敗: %s %s", r.status_code, r.text[:200])
    except httpx.HTTPError as e:
        log.warning("Discord 通知失敗: %s", e)


async def job_finished(title: str, status: str, summary: str) -> None:
    await send(f"[{STATUS_JA.get(status, status)}] {title}", summary or "(対象なし)", status,
               mention=status != "success")
