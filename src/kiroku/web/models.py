"""The browser contract; raw prompts, provider outputs and credentials stay server-side."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator


class Model(BaseModel):
    model_config = ConfigDict(from_attributes=True, extra="forbid")


class Session(Model):
    id: int
    channel: str
    title: str
    category: str | None
    started_at: datetime
    ended_at: datetime | None
    mode: str | None
    alert_count: int
    note_count: int


class Utterance(Model):
    id: int
    audio_start_ms: int
    audio_end_ms: int
    spoken_at: datetime
    text: str
    transcriber: str


class TranscriptCursor(Model):
    id: int


class Note(Model):
    id: int
    start_ms: int
    end_ms: int
    pack: str | None
    mode: str | None
    status: Literal["pending", "ok", "empty", "invalid", "failed", "over_budget"]
    body: dict[str, JsonValue] | None
    schema_name: str
    schema_version: int


class Alert(Model):
    id: int
    decision_id: int
    rule: str
    outcome: str
    said_at: datetime | None
    start_ms: int
    text: str
    label: bool | None


class Rule(Model):
    id: int
    name: str
    instructions: str
    threshold: Decimal
    pack: str | None
    cooldown_s: int
    enabled: bool


class RuleCreate(Model):
    name: str
    instructions: str
    threshold: Decimal = Decimal("0.7")
    cooldown_s: int = 600
    pack: str | None = None
    enabled: bool = True


class RuleEdit(Model):
    instructions: str | None = None
    threshold: Decimal | None = None
    cooldown_s: int | None = None
    pack: str | None = None
    enabled: bool | None = None


class RuleTest(Model):
    instructions: str | None = None
    threshold: Decimal | None = None
    session: Annotated[int, Field(gt=0)] | None = None
    last: Annotated[int, Field(ge=1, le=100)] | None = None

    @model_validator(mode="after")
    def selectors(self) -> RuleTest:
        if self.session is not None and self.last is not None:
            raise ValueError("choose session or last, not both")
        return self


class Trial(Model):
    session_id: int
    start_ms: int
    new: str
    probability: float | None
    error: str | None


class TestReport(Model):
    question: str
    threshold: Decimal
    trials: list[Trial]
    scored: int
    passed: int
    failed: int
    pass_rate: float
    histogram: list[int]
    highest: list[Trial]
    lowest: list[Trial]
    report: str


class TestProgress(Model):
    id: str
    completed: int
    total: int


class RunningTest(TestProgress):
    status: Literal["running"] = "running"


class CompletedTest(TestProgress):
    status: Literal["completed"] = "completed"
    report: TestReport


class FailedTest(TestProgress):
    status: Literal["failed"] = "failed"
    error: str


type TestJob = Annotated[RunningTest | CompletedTest | FailedTest, Field(discriminator="status")]


class Review(Model):
    id: int
    rule: str
    question: str
    probability: Decimal
    threshold: Decimal
    passed: bool
    session_id: int
    channel: str
    start_ms: int
    new: str
    earlier: str


class Verdict(Model):
    applies: Annotated[bool, Field(strict=True)]


class GatePass(Model):
    id: int
    rule: str
    question: str
    probability: Decimal
    threshold: Decimal
    start_ms: int
