"""
ops_alert.py -- post a short alert to a PRIVATE ops Discord webhook.

Reads OPS_DISCORD_WEBHOOK. Never raises: an alert failure must not mask
the real failure. Never point this at a subscriber channel.
"""

import os

import requests


def post_ops_alert(message: str) -> bool:
    url = os.environ.get("OPS_DISCORD_WEBHOOK", "")
    if not url:
        print("[OPS ALERT] OPS_DISCORD_WEBHOOK not set, alert not sent: " + message.encode("ascii", "replace").decode())
        return False
    try:
        r = requests.post(url, json={"content": message[:1900]}, timeout=15)
        print(f"[OPS ALERT] posted: {r.status_code}")
        return r.status_code < 300
    except Exception as e:
        print(f"[OPS ALERT] failed to post: {e}")
        return False
