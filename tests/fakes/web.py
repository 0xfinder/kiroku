"""A synthetic notebook for HTTP and browser acceptance; never touches a live service."""

from datetime import UTC, datetime
from decimal import Decimal

from psycopg.types.json import Jsonb

from kiroku import gating, rules, segments, sessions, transcript
from kiroku.db import Pool
from kiroku.gate import GateAnswer, Mode, rule_question
from kiroku.gate.windows import Bounds, GateState
from kiroku.gating import Answered
from kiroku.source import StreamMeta
from kiroku.transcriber import Result, Utterance

T0 = datetime(2026, 10, 13, 12, tzinfo=UTC)
TRANSCRIBER = "fixture:web"
TEXT = "Blue has the stronger draft. Their range gives them room to control the next fight."


async def seed(pool: Pool, database_url: str) -> tuple[int, int]:
    async with sessions.claim_channels(database_url, "twitch", ["caedrel"]) as channels:
        channel = channels[0]
        meta = StreamMeta("web-fixture", "Worlds · A closer look at the draft", "League of Legends")
        session = await sessions.open_session(pool, channel, meta, T0)
        leg = await sessions.next_leg(pool, channel, session)
        await sessions.record_leg(pool, leg, T0)
        await segments.record(pool, session, 0, meta, "league")
    rule = await rules.add(
        pool,
        "predicts_winner",
        "Does the streamer predict who will win?",
        threshold=Decimal("0.6"),
        pack="league",
    )
    await rules.add(
        pool, "draft_take", "Does the streamer evaluate a draft?", threshold=Decimal("0.7")
    )
    utterances = tuple(
        Utterance(
            i * 5_000,
            i * 5_000 + 4_000,
            TEXT if i == 0 else f"Draft discussion {i}: the next objective matters.",
        )
        for i in range(36)
    )
    await transcript.commit(pool, session, TRANSCRIBER, Result(0, 180_000, utterances, None))
    for i, p in enumerate((0.3, 0.7, 0.2, 0.8)):
        await gating.commit(
            pool,
            session,
            TRANSCRIBER,
            Bounds(i * 45_000, (i + 1) * 45_000),
            GateState({"earlier": "Blue's composition needs time to scale."}, TEXT),
            [rule_question(rule)],
            Answered(GateAnswer("fixture:gate", {rule.id: p}, Mode.WATCHING_PRO, 0.9, ())),
        )
    async with pool.connection() as conn:
        row = await (
            await conn.execute(
                "SELECT d.id FROM gate_decision d JOIN gate_window w ON w.id=d.window_id "
                "WHERE w.session_id=%s AND w.start_ms=45000",
                [session],
            )
        ).fetchone()
        assert row is not None
        decision = row[0]
        await conn.execute(
            "INSERT INTO alert(decision_id,rule_id,session_id,channel_id,said_at,game_key,"
            "outcome,content,notifier,external_id,sent_at) VALUES(%s,%s,%s,%s,%s,'fixture',"
            "'sent',%s,'telegram','fixture:1',%s)",
            [
                decision,
                rule.id,
                session,
                channel.id,
                T0,
                Jsonb({"claim": "Blue has the stronger draft"}),
                T0,
            ],
        )
        for start, status, body in (
            (
                0,
                "ok",
                {
                    "summary": "Blue's range is the draft's defining advantage.",
                    "topics": [],
                    "predictions": [],
                    "takes": [
                        {
                            "at_ms": 0,
                            "subject": "Blue draft",
                            "take": "Range creates room for the next fight.",
                        }
                    ],
                },
            ),
            (180_000, "pending", None),
        ):
            await conn.execute(
                "INSERT INTO note(session_id,start_ms,end_ms,transcriber,pack,mode,status,"
                "schema_name,schema_version,body,input,outputs,model,system,reason) "
                "VALUES(%s,%s,%s,%s,'league','watching_pro',%s,'general',2,%s,'{}','[]',%s,'fixture',%s)",
                [
                    session,
                    start,
                    start + 180_000,
                    TRANSCRIBER,
                    status,
                    Jsonb(body) if body else None,
                    "fixture:notes" if body else None,
                    None if body else "waiting for speech",
                ],
            )
    return session, decision
