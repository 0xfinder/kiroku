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
from uuid import uuid4

import httpx
from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StaticHTTPException
from starlette.types import Receive, Scope, Send

from kiroku import alerting, dry_run, gating, labels, notes, rules, sessions, transcript
from kiroku.budget import Budget
from kiroku.config import Secret, SecretName, Settings
from kiroku.db import Pool, open_pool
from kiroku.errors import KirokuError
from kiroku.gate import RuleQuestion, rule_question
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
    test_task: asyncio.Task[None] | None = None
    test_job: m.TestJob | None = None

    @contextlib.asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncGenerator[None]:
        nonlocal pool
        async with open_pool(settings.database_url) as pool:
            listener = asyncio.create_task(hub.run())
            try:
                yield
            finally:
                listener.cancel()
                tasks = [listener]
                if test_task is not None:
                    test_task.cancel()
                    tasks.append(test_task)
                with contextlib.suppress(TimeoutError):
                    async with asyncio.timeout(5):
                        await asyncio.gather(*tasks, return_exceptions=True)

    app = FastAPI(title="Kiroku", lifespan=lifespan)

    @app.middleware("http")
    async def same_origin(
        request: Request, next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            origin = request.headers.get("origin")
            hosts = {
                host
                for host in (request.headers.get("host"), request.headers.get("x-forwarded-host"))
                if host
            }
            if origin and urlsplit(origin).netloc not in hosts:
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

    @app.api_route("/api/sessions", methods=["GET", "HEAD"])
    async def session_list(live: bool | None = None, limit: Limit = 30) -> list[m.Session]:
        return [
            m.Session.model_validate(s) for s in await sessions.browse(pool, live=live, limit=limit)
        ]

    @app.api_route("/api/sessions/{session_id}", methods=["GET", "HEAD"])
    async def session_detail(session_id: int) -> m.Session:
        session = await sessions.summary_by_id(pool, session_id)
        if session is None:
            raise HTTPException(404, "session not found")
        return m.Session.model_validate(session)

    @app.api_route("/api/sessions/{session_id}/transcript", methods=["GET", "HEAD"])
    async def transcript_page(
        session_id: int,
        after_ms: After = -1,
        limit: Annotated[int, Query(ge=1, le=1000)] = 200,
        through_id: Annotated[int | None, Query(ge=0)] = None,
    ) -> list[m.Utterance]:
        await require_session(session_id)
        return [
            m.Utterance.model_validate(row)
            for row in await transcript.page(
                pool, session_id, after_ms=after_ms, limit=limit, through_id=through_id
            )
        ]

    @app.api_route("/api/sessions/{session_id}/transcript/cursor", methods=["GET", "HEAD"])
    async def transcript_cursor(session_id: int) -> m.TranscriptCursor:
        await require_session(session_id)
        return m.TranscriptCursor(id=await transcript.latest_id(pool, session_id))

    @app.api_route("/api/sessions/{session_id}/transcript/stream", methods=["GET", "HEAD"])
    async def transcript_stream(
        request: Request,
        session_id: int,
        after_ms: After = -1,
        after_id: Annotated[int | None, Query(ge=0)] = None,
    ) -> Response:
        await require_session(session_id)
        header = request.headers.get("last-event-id")
        if header is not None:
            # Native reconnects repeat the original URL. The browser rescans history
            # on reopen, so the bootstrap commit cursor must not be replayed too.
            after_id = None
            try:
                after_ms = int(header)
            except ValueError:
                raise HTTPException(400, "Last-Event-ID must be audio_start_ms") from None
            if not -1 <= after_ms <= 2_147_483_647:
                raise HTTPException(400, "Last-Event-ID is outside the audio clock")
        if request.method == "HEAD":
            return Response(media_type="text/event-stream")
        return StreamingResponse(
            events(request, pool, hub, session_id, after_ms, after_id),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.api_route("/api/sessions/{session_id}/notes", methods=["GET", "HEAD"])
    async def session_notes(session_id: int) -> list[m.Note]:
        await require_session(session_id)
        return [m.Note.model_validate(row) for row in await notes.timeline_rows(pool, session_id)]

    @app.api_route("/api/sessions/{session_id}/alerts", methods=["GET", "HEAD"])
    async def session_alerts(session_id: int, limit: Limit = 100) -> list[m.Alert]:
        await require_session(session_id)
        return [
            m.Alert.model_validate(row)
            for row in await alerting.session_rows(pool, session_id, limit)
        ]

    @app.api_route("/api/sessions/{session_id}/passes", methods=["GET", "HEAD"])
    async def session_passes(session_id: int, limit: Limit = 100) -> list[m.GatePass]:
        await require_session(session_id)
        return [
            m.GatePass.model_validate(row)
            for row in await gating.session_passes(pool, session_id, limit)
        ]

    @app.api_route("/api/rules", methods=["GET", "HEAD"])
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

    async def run_test(
        job: m.RunningTest,
        name: str,
        question: RuleQuestion,
        windows: list[dry_run.StoredWindow],
        key: Secret,
    ) -> None:
        nonlocal test_job

        def progress(_: dry_run.Trial) -> None:
            job.completed += 1

        try:
            async with httpx.AsyncClient() as http:
                trials = await dry_run.run(
                    pool,
                    JevGate(http, key),
                    Budget(pool, settings.caps).guard(None),
                    question,
                    windows,
                    progress=progress,
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

            result = m.TestReport(
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

            test_job = m.CompletedTest(
                id=job.id,
                completed=job.completed,
                total=job.total,
                report=result,
            )
        except asyncio.CancelledError:
            test_job = m.FailedTest(
                id=job.id,
                completed=job.completed,
                total=job.total,
                error="server stopped",
            )
            raise
        except Exception as error:
            logging.getLogger(__name__).warning("Rule test failed (%s)", type(error).__name__)
            test_job = m.FailedTest(
                id=job.id,
                completed=job.completed,
                total=job.total,
                error=str(error)
                if isinstance(error, KirokuError)
                else "Rule test failed; see server log",
            )

    @app.post("/api/rules/{name}/test", status_code=202)
    async def rule_test(name: str, body: m.RuleTest) -> m.RunningTest:
        nonlocal test_job, test_task
        if test_lock.locked() or (test_task is not None and not test_task.done()):
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
            job = m.RunningTest(id=str(uuid4()), completed=0, total=len(windows))
            test_job = job
            test_task = asyncio.create_task(run_test(job, name, question, windows, key))
            return job

    @app.api_route("/api/rule-tests/{job_id}", methods=["GET", "HEAD"])
    async def rule_test_status(job_id: str) -> m.TestJob:
        if test_job is None or test_job.id != job_id:
            raise HTTPException(
                404, "Rule test not found; results expire when the next test starts"
            )
        return test_job

    @app.api_route("/api/review", methods=["GET", "HEAD"])
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

    async def api_missing(scope: Scope, receive: Receive, send: Send) -> None:
        await JSONResponse({"detail": "API route not found"}, status_code=404)(scope, receive, send)

    # An ASGI mount accepts every method, including custom HTTP verbs, before the SPA.
    app.mount("/api", api_missing)
    app.mount("/", SPA(directory=STATIC, check_dir=False), name="web")
    return app
