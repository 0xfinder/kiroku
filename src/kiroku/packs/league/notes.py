"""Small League timelines, owned and versioned by the Pack."""

from pydantic import Field

from kiroku.notes_schema import General, NotesModel, NotesSchema, Take


class Prediction(NotesModel):
    at_ms: int = Field(ge=0)
    winner: str
    quote: str


class WatchingPro(NotesModel):
    teams: list[str] | None = Field(min_length=2, max_length=2)
    game_number: int | None = Field(ge=1)
    draft: dict[str, list[str]] | None
    predictions: list[Prediction]
    takes: list[Take]
    summary: str = Field(min_length=1)


class Moment(NotesModel):
    at_ms: int = Field(ge=0)
    what: str


class Playing(NotesModel):
    champion: str | None
    role: str | None
    moments: list[Moment]
    takes: list[Take]
    summary: str = Field(min_length=1)


SCHEMAS = {
    "watching_pro": NotesSchema(
        "league.watching_pro",
        1,
        WatchingPro,
        "Carry known teams, game number and draft forward; use null when unknown. "
        "Capture winner predictions, including a named favorite or the team more likely to win, "
        "with the speaker's exact quote. Capture other expressed opinions as concise takes. "
        "Do not infer a winner prediction from play-by-play.",
    ),
    "playing": NotesSchema(
        "league.playing",
        1,
        Playing,
        "Capture the streamer's champion, role, key moments and opinions; use null when unknown.",
    ),
    "chatting": NotesSchema("league.chatting", 1, General, "Capture chat topics and opinions."),
}


def notes_schema(mode: str | None) -> NotesSchema | None:
    return SCHEMAS.get(mode) if mode is not None else None
