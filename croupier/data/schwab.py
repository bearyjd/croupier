"""Schwab Market Data adapter — READ-ONLY BY CONSTRUCTION (PRP-002).

Register the developer app with the Market Data product ONLY; without the
Accounts & Trading product the credentials cannot place orders. This module
deliberately contains no order-related code paths.

Token lifecycle: access tokens ~30 min, refresh tokens hard-expire at 7
days requiring manual browser re-auth. Staleness is reported, not raised.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

from croupier.data.base import DataHealth, Quote

log = logging.getLogger(__name__)

TOKEN_URL = "https://api.schwabapi.com/v1/oauth/token"
QUOTES_URL = "https://api.schwabapi.com/marketdata/v1/quotes"
REFRESH_TOKEN_LIFETIME = timedelta(days=7)


@dataclass
class TokenState:
    access_token: str
    refresh_token: str
    access_expires_at: datetime
    refresh_issued_at: datetime

    @classmethod
    def load(cls, path: Path) -> TokenState | None:
        try:
            raw = json.loads(path.read_text())
            return cls(
                access_token=raw["access_token"],
                refresh_token=raw["refresh_token"],
                access_expires_at=datetime.fromisoformat(raw["access_expires_at"]),
                refresh_issued_at=datetime.fromisoformat(raw["refresh_issued_at"]),
            )
        except (FileNotFoundError, KeyError, ValueError):
            return None

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "access_expires_at": self.access_expires_at.isoformat(),
            "refresh_issued_at": self.refresh_issued_at.isoformat(),
        }))
        path.chmod(0o600)

    @property
    def refresh_expires_at(self) -> datetime:
        return self.refresh_issued_at + REFRESH_TOKEN_LIFETIME

    def days_until_reauth(self, now: datetime) -> float:
        return (self.refresh_expires_at - now).total_seconds() / 86400


class SchwabMarketData:
    name = "schwab"

    def __init__(self, app_key: str, app_secret: str, token_path: Path):
        self._key, self._secret, self._token_path = app_key, app_secret, token_path
        self._tokens = TokenState.load(token_path)

    def health(self) -> DataHealth:
        now = datetime.now(UTC)
        if self._tokens is None or now >= self._tokens.refresh_expires_at:
            return DataHealth.DEAD  # router falls through to the EOD floor
        return DataHealth.FRESH

    async def _ensure_access(self, client: httpx.AsyncClient) -> bool:
        now = datetime.now(UTC)
        t = self._tokens
        if t is None or now >= t.refresh_expires_at:
            log.warning("schwab refresh token expired; manual re-auth required")
            return False
        if now < t.access_expires_at - timedelta(minutes=2):
            return True
        resp = await client.post(TOKEN_URL, auth=(self._key, self._secret), data={
            "grant_type": "refresh_token", "refresh_token": t.refresh_token})
        if resp.status_code != 200:
            log.warning("schwab token refresh failed: %s", resp.status_code)
            return False
        body = resp.json()
        self._tokens = TokenState(
            access_token=body["access_token"],
            refresh_token=body.get("refresh_token", t.refresh_token),
            access_expires_at=now + timedelta(seconds=body.get("expires_in", 1800)),
            refresh_issued_at=t.refresh_issued_at,  # refresh lifetime does NOT reset
        )
        self._tokens.save(self._token_path)
        return True

    async def quote(self, ticker: str) -> Quote | None:
        async with httpx.AsyncClient(timeout=15) as client:
            if not await self._ensure_access(client):
                return None
            resp = await client.get(
                QUOTES_URL, params={"symbols": ticker},
                headers={"Authorization": f"Bearer {self._tokens.access_token}"})
            if resp.status_code != 200:
                return None
            data = resp.json().get(ticker, {}).get("quote", {})
            price = data.get("lastPrice") or data.get("mark")
            if price is None:
                return None
            return Quote(ticker=ticker, price=float(price),
                         as_of=datetime.now(UTC),
                         source="schwab", health=DataHealth.FRESH)
