"""Notifikace do Telegramu (volitelné, tiše se přeskočí když není nastaveno)."""
from __future__ import annotations

import logging
import os
import urllib.parse
import urllib.request

from .config import NotifyConfig

log = logging.getLogger(__name__)


class Notifier:
    def __init__(self, cfg: NotifyConfig) -> None:
        self.cfg = cfg
        self.token = os.getenv(cfg.telegram_token_env, "")
        self.chat_id = os.getenv(cfg.telegram_chat_env, "")

    @property
    def enabled(self) -> bool:
        return self.cfg.telegram_enabled and bool(self.token and self.chat_id)

    def send(self, text: str, level: str = "info") -> None:
        if not self.enabled or level not in self.cfg.notify_levels:
            return
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        data = urllib.parse.urlencode({
            "chat_id": self.chat_id, "text": text, "parse_mode": "HTML",
            "disable_web_page_preview": "true",
        }).encode()
        try:
            with urllib.request.urlopen(url, data=data, timeout=10) as resp:
                resp.read()
        except Exception as exc:  # notifikace nikdy nesmí shodit obchodování
            log.warning("Telegram notifikace selhala: %s", exc)
