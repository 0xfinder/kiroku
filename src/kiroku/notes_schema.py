"""Core Notes schema and the small descriptor Packs may provide."""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, cast

from pydantic import BaseModel, ConfigDict, Field

from kiroku.gate.windows import Bounds


class NotesModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Take(NotesModel):
    at_ms: int = Field(ge=0)
    subject: str
    take: str


class Topic(NotesModel):
    at_ms: int = Field(ge=0)
    topic: str


class Prediction(NotesModel):
    at_ms: int = Field(
        ge=0, description="Copy at_ms of the current turn containing this prediction."
    )
    winner: str = Field(
        min_length=1,
        description=(
            "Competing TEAM or player favored to win; NEVER a hero/champion/item. "
            "For X cannot beat Y, name Y's team, resolving from current/previous turns."
        ),
    )
    quote: str = Field(
        min_length=1,
        description=(
            "Exact words in the current turns expressing the forecast, favorite, odds "
            "or inability to beat the opponent. Must express a forecast or favorite; "
            "criticism of past fights, hero matchup statistics and GG results are takes."
        ),
    )


class General(NotesModel):
    predictions: list[Prediction] = Field(
        description=(
            "Every expressed winner forecast, favorite, odds/percentages, or implicit "
            "prediction that one side cannot beat the other. Use [] only when none "
            "appear in current turns."
        )
    )
    topics: list[Topic]
    takes: list[Take]
    summary: str = Field(min_length=1)


@dataclass(frozen=True, slots=True)
class NotesSchema:
    name: str
    version: int
    model: type[BaseModel]
    instructions: str

    def parse(self, raw: Mapping[str, Any], bounds: Bounds) -> dict[str, Any]:
        body = self.model.model_validate(raw).model_dump(mode="json")
        check_timestamps(body, bounds)
        return body


def check_timestamps(value: Any, bounds: Bounds) -> None:
    if isinstance(value, dict):
        value = cast(dict[str, Any], value)
        if "at_ms" in value:
            at = value["at_ms"]
            if type(at) is not int or not bounds.start_ms <= at < bounds.end_ms:
                raise ValueError("at_ms outside source span")
        for child in value.values():
            check_timestamps(child, bounds)
    elif isinstance(value, list):
        for child in cast(list[Any], value):
            check_timestamps(child, bounds)


GENERAL = NotesSchema(
    "general",
    2,
    General,
    "Keep topics and opinions concise. Put winner forecasts in predictions, not only in takes.",
)
