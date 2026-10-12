"""Notifier-neutral Alerts, delivery outcomes, and human Labels."""

from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Protocol


@dataclass(frozen=True, slots=True)
class AlertMessage:
    alert_id: int
    channel: str
    rule: str
    summary: str
    quote: str | None
    said_at: datetime
    probability: Decimal
    gate_model: str
    degraded: str | None = None


@dataclass(frozen=True, slots=True)
class Delivery:
    external_id: str


@dataclass(frozen=True, slots=True)
class Tap:
    alert_id: int
    applies: bool
    handle: object
    delivery: Delivery
    sent_at: datetime


class NotifierUnavailable(Exception):
    """Definitely not delivered, after retries."""


class NotifierRejected(Exception):
    """Permanent rejection."""


class NotifierUnsure(Exception):
    """The request may have been delivered. Never resend."""


class Notifier(Protocol):
    @property
    def name(self) -> str: ...

    async def send(self, message: AlertMessage, *, give_up_s: float) -> Delivery: ...

    def taps(self) -> AsyncIterator[Tap]: ...

    async def confirm(self, tap: Tap, *, recorded: bool) -> None: ...
