"""HTTP boundary. All data access lives in the existing table owners."""

import asyncio
import contextlib
import ipaddress
import logging
from collections.abc import AsyncGenerator, Awaitable, Callable
from dataclasses import replace
from pathlib import Path
from typing import Annotated
from urllib.parse import urlsplit

import httpx
from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StaticHTTPException
from starlette.types import Scope

from kiroku import alerting, dry_run, gating, labels, notes, rules, segments, sessions, transcript
from kiroku.budget import Budget
from kiroku.config import SecretName, Settings
from kiroku.db import Pool, open_pool
from kiroku.errors import KirokuError
from kiroku.gate import rule_question
from kiroku.gate.jev import JevGate
from kiroku.web import models as m
from kiroku.web.stream import TranscriptHub, events

STATIC = Path(__file__).with_name("static")
Limit = Annotated[int, Query(ge=1, le=100)]
After = Annotated[int, Query(ge=-1, le=2_147_483_647)]


def warn_bind(host: str) -> None:
    try:
        loopback = ipaddress.ip_address(host).is_loopback
    except ValueError:
        loopback = host == "localhost"
    if not loopback:
        logging.getLogger(__name__).warning(
            "D18: non-loopback bind %s; expose only through Tailscale or Cloudflare Access", host
        )


class SPA(StaticFiles):
    async def get_response(self, path: str, scope: Scope) -> Response:
        try:
            return await super().get_response(path, scope)
        except StaticHTTPException as error:
            if error.status_code != 404 or path.startswith("_app/") or Path(path).suffix:
                raise
            return FileResponse(STATIC / "index.html")


def create_app(settings: Settings) -> FastAPI:
    pool: Pool
    hub = TranscriptHub(settings.database_url)
    test_lock = asyncio.Lock()

    @contextlib.asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncGenerator[None]:
        nonlocal pool
        async with open_pool(settings.database_url) as pool:
            listener = asyncio.create_task(hub.run())
            try:
                yield
            finally:
                listener.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await listener

    app = FastAPI(title="Kiroku", lifespan=lifespan)

    @app.middleware("http")
    async def same_origin(
        request: Request, next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            origin = request.headers.get("origin")
            if origin and urlsplit(origin).netloc != request.headers.get("host"):
                return JSONResponse({"detail": "cross-origin writes are refused"}, status_code=403)
        return await next(request)

    @app.exception_handler(KirokuError)
    async def user_error(_: Request, error: KirokuError) -> JSONResponse:
        return JSONResponse({"detail": str(error)}, status_code=400)

    async def require_session(session_id: int) -> sessions.Session:
        session = await sessions.by_id(pool, session_id)
        if session is None:
            raise HTTPException(404, "session not found")
        return session

    async def describe(session: sessions.Session) -> m.Session:
        segment = await segments.latest(pool, session.id)
        return m.Session(
            id=session.id,
            channel=session.channel,
            title=segment.title if segment else session.title,
            category=segment.category if segment else None,
            started_at=session.started_at,
            ended_at=session.ended_at,
            mode=await gating.latest_mode(pool, session.id),
            alert_count=await alerting.count_for_session(pool, session.id),
            note_count=await notes.count_for_session(pool, session.id),
        )

    @app.get("/api/sessions")
    async def session_list(live: bool | None = None, limit: Limit = 30) -> list[m.Session]:
        return [await describe(s) for s in await sessions.browse(pool, live=live, limit=limit)]

    @app.get("/api/sessions/{session_id}")
    async def session_detail(session_id: int) -> m.Session:
        return await describe(await require_session(session_id))

    @app.get("/api/sessions/{session_id}/transcript")
    async def transcript_page(
        session_id: int, after_ms: After = -1, limit: Annotated[int, Query(ge=1, le=1000)] = 200
    ) -> list[m.Utterance]:
        await require_session(session_id)
        return [
            m.Utterance.model_validate(row)
            for row in await transcript.page(pool, session_id, after_ms=after_ms, limit=limit)
        ]

    @app.get("/api/sessions/{session_id}/transcript/stream")
    async def transcript_stream(
        request: Request, session_id: int, after_ms: After = -1
    ) -> StreamingResponse:
        await require_session(session_id)
        header = request.headers.get("last-event-id")
        if header is not None:
            try:
                after_ms = int(header)
            except ValueError:
                raise HTTPException(400, "Last-Event-ID must be audio_start_ms") from None
            if not -1 <= after_ms <= 2_147_483_647:
                raise HTTPException(400, "Last-Event-ID is outside the audio clock")
        return StreamingResponse(
            events(request, pool, hub, session_id, after_ms),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.get("/api/sessions/{session_id}/notes")
    async def session_notes(session_id: int) -> list[m.Note]:
        await require_session(session_id)
        return [m.Note.model_validate(row) for row in await notes.timeline_rows(pool, session_id)]

    @app.get("/api/sessions/{session_id}/alerts")
    async def session_alerts(session_id: int, limit: Limit = 100) -> list[m.Alert]:
        await require_session(session_id)
        return [
            m.Alert.model_validate(row)
            for row in await alerting.session_rows(pool, session_id, limit)
        ]

    @app.get("/api/sessions/{session_id}/passes")
    async def session_passes(session_id: int, limit: Limit = 100) -> list[m.GatePass]:
        await require_session(session_id)
        return [
            m.GatePass.model_validate(row)
            for row in await gating.session_passes(pool, session_id, limit)
        ]

    @app.get("/api/rules")
    async def rule_list() -> list[m.Rule]:
        return [m.Rule.model_validate(rule) for rule in await rules.all_rules(pool)]

    @app.post("/api/rules", status_code=201)
    async def rule_create(body: m.RuleCreate) -> m.Rule:
        rule = await rules.add(
            pool,
            body.name,
            body.instructions,
            threshold=body.threshold,
            cooldown_s=body.cooldown_s,
            pack=body.pack,
            enabled=body.enabled,
        )
        return m.Rule.model_validate(rule)

    @app.patch("/api/rules/{name}")
    async def rule_edit(name: str, body: m.RuleEdit) -> m.Rule:
        rule = await rules.edit(
            pool,
            name,
            instructions=body.instructions,
            threshold=body.threshold,
            cooldown_s=body.cooldown_s,
            pack=body.pack,
            clear_pack="pack" in body.model_fields_set and body.pack is None,
            enabled=body.enabled,
        )
        return m.Rule.model_validate(rule)

    @app.post("/api/rules/{name}/enable")
    async def rule_enable(name: str) -> m.Rule:
        return m.Rule.model_validate(await rules.set_enabled(pool, name, True))

    @app.post("/api/rules/{name}/disable")
    async def rule_disable(name: str) -> m.Rule:
        return m.Rule.model_validate(await rules.set_enabled(pool, name, False))

    @app.post("/api/rules/{name}/test")
    async def rule_test(name: str, body: m.RuleTest) -> m.TestReport:
        if test_lock.locked():
            raise HTTPException(409, "a Rule test is already running")
        async with test_lock:
            saved = await rules.get(pool, name)
            candidate = replace(
                saved,
                id=0 if body.instructions is not None else saved.id,
                instructions=body.instructions.strip()
                if body.instructions is not None
                else saved.instructions,
                threshold=body.threshold if body.threshold is not None else saved.threshold,
            )
            rules.check(candidate.name, candidate.instructions, candidate.threshold)
            windows = await dry_run.stored_windows(
                pool,
                session_id=body.session,
                last=100 if body.session is not None else body.last or 40,
            )
            if not windows:
                raise HTTPException(400, "no stored Windows with speech yet")
            # No key is required when serving reads or editing Rules.
            key = settings.secrets.require(SecretName.JEV)
            question = rule_question(candidate)
            async with httpx.AsyncClient() as http:
                trials = await dry_run.run(
                    pool,
                    JevGate(http, key),
                    Budget(pool, settings.caps).guard(None),
                    question,
                    windows,
                )
            summary = dry_run.summarize(question, trials)

            def trial(t: dry_run.Trial) -> m.Trial:
                return m.Trial(
                    session_id=t.window.session_id,
                    start_ms=t.window.start_ms,
                    new=t.window.state.new,
                    probability=t.probability,
                    error=t.error,
                )

            return m.TestReport(
                question=question.question,
                threshold=question.threshold,
                trials=[trial(t) for t in trials],
                scored=summary.scored,
                passed=summary.passed,
                failed=summary.failed,
                pass_rate=summary.pass_rate,
                histogram=list(summary.histogram),
                highest=[trial(t) for t in summary.highest],
                lowest=[trial(t) for t in summary.lowest],
                report=dry_run.report(name, question, trials),
            )

    @app.get("/api/review")
    async def review_queue(limit: Limit = 30) -> list[m.Review]:
        return [m.Review.model_validate(row) for row in await gating.review_queue(pool, limit)]

    @app.post("/api/review/{decision_id}", status_code=204)
    async def review_label(decision_id: int, body: m.Verdict) -> Response:
        if not await labels.record_review(pool, decision_id, body.applies):
            raise HTTPException(404, "Gate decision not found")
        return Response(status_code=204)

    @app.delete("/api/review/{decision_id}", status_code=204)
    async def review_undo(decision_id: int) -> Response:
        await labels.undo_review(pool, decision_id)
        return Response(status_code=204)

    @app.api_route("/api/{path:path}", methods=["GET", "POST", "PATCH", "DELETE"])
    async def api_missing(path: str) -> Response:
        raise HTTPException(404, "API route not found")

    app.mount("/", SPA(directory=STATIC, check_dir=False), name="web")
    return app
