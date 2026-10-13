"""Small League timelines, owned and versioned by the Pack."""

from pydantic import Field

from kiroku.notes_schema import General, NotesModel, NotesSchema, Prediction, Take


class WatchingPro(NotesModel):
    teams: list[str] | None = Field(min_length=2, max_length=2)
    game_number: int | None = Field(ge=1)
    draft: dict[str, list[str]] | None
    predictions: list[Prediction] = Field(
        description=(
            "Every winner forecast, favorite, odds or percentages, including implicit "
            "predictions that one opponent cannot win. Do not put these only in takes."
        )
    )
    takes: list[Take]
    summary: str = Field(min_length=1)


class Moment(NotesModel):
    at_ms: int = Field(ge=0)
    what: str


class Playing(NotesModel):
    predictions: list[Prediction]
    champion: str | None
    role: str | None
    moments: list[Moment]
    takes: list[Take]
    summary: str = Field(min_length=1)


SCHEMAS = {
    "watching_pro": NotesSchema(
        "league.watching_pro",
        2,
        WatchingPro,
        "Carry known teams, game number and draft forward; use null when unknown. "
        "Put every named favorite, likely winner, odds/percentage, and implicit winner forecast "
        "in predictions with winner and the exact quote. 'X cannot beat Y' or 'Y is too much "
        "for X' predicts Y. 'Red should still be favorable' predicts Red, even without 'will win'. "
        "Capture other expressed opinions as concise takes. "
        "Do not infer a winner prediction from play-by-play.",
    ),
    "playing": NotesSchema(
        "league.playing",
        2,
        Playing,
        "Capture the streamer's champion, role, key moments and opinions; use null when unknown.",
    ),
    "chatting": NotesSchema("league.chatting", 2, General, "Capture chat topics and opinions."),
}


def notes_schema(mode: str | None) -> NotesSchema | None:
    return SCHEMAS.get(mode) if mode is not None else None
