from __future__ import annotations

import httpx


async def send_webhook(webhook_url: str, title: str, content: str) -> bool:
    if not webhook_url:
        return False
    payload = {"msg_type": "text", "content": {"text": f"{title}\n{content}"}}
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            response = await client.post(webhook_url, json=payload)
            response.raise_for_status()
        return True
    except Exception:
        return False
