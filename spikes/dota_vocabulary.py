"""Runtime and oracle arms over immutable archived legs; production chunker, Groq and ledger.

uv run python spikes/dota_vocabulary.py <archive-dir> <out-dir>
Only kiroku_spike_28 is created/migrated. Source JSON and prompt choices are recorded.
No live APIs other than Groq are called by this runner. Select unfinished arms with --arms.
Use --verify-only to check existing PCM hashes, prompt continuity and scratch ledger without calls.
"""

import argparse
import asyncio
import hashlib
import json
from decimal import Decimal
from pathlib import Path

import httpx
import psycopg

from kiroku import ledger
from kiroku.archive import decoded_ms
from kiroku.audio import AudioBlock, LegEnded
from kiroku.budget import Budget
from kiroku.config import SecretName, Settings
from kiroku.db import connect, open_pool
from kiroku.db.migrate import migrate
from kiroku.packs.dota import vocabulary
from kiroku.packs.dota.data import players, teams, title_teams
from kiroku.transcriber import Context
from kiroku.transcriber.groq import GroqTranscriber, build_prompt

TITLE = "(EN) Spirit vs Yandex - GRAND FINALS! - Blast Slam VIII - Official co-stream w/ PLD"
DATABASE = "postgresql://kiroku:kiroku@127.0.0.1:5432/kiroku_spike_28"
# Offline clip context from the stored transcript, not a runtime match feed.
HEROES = (
    "Bane",
    "Windranger",
    "Winter Wyvern",
    "Earth Spirit",
    "Kez",
    "Ember Spirit",
    "Brewmaster",
    "Rubick",
    "Timbersaw",
    "Nyx Assassin",
)
ITEMS = (
    "Black King Bar",
    "Nullifier",
    "Wind Waker",
    "Octarine Core",
    "Clarity",
    "Hydra's Breath",
    "Hurricane Pike",
)
# Tiny identity table researched via Liquipedia, see sources/liquipedia-extracts.json.
ALIASES = {183719386: ("Ammar", "ATF"), 171262902: ("watson",), 93817671: ("Malady",)}


def prompt(terms):
    return vocabulary(TITLE, tuple(terms), ())


async def run(args):
    settings = Settings.load()
    if args.verify_only:
        args.arms = []
    out = args.output
    sources = out / "sources"
    previous_method = (
        json.loads((out / "method.json").read_text())
        if (out / "method.json").exists()
        else None
    )
    for arm in args.arms:
        assert not (out / f"{arm}.jsonl").exists(), (
            f"{arm} already recorded; select unfinished arms only"
        )
    chosen = title_teams(
        teams(json.loads((sources / "teams.json").read_text())), TITLE, 1791936000
    )
    roster = [
        p
        for team in chosen
        for p in players(json.loads((sources / f"players-{team.id}.json").read_text()))
    ]
    handles = [p.name for p in roster]
    aliases = [a for p in roster for a in ALIASES.get(p.account_id, ())]
    catalogue_heroes = {
        v["localized_name"]
        for v in json.loads((sources / "heroes.json").read_text()).values()
    }
    catalogue_items = {
        v.get("dname")
        for v in json.loads((sources / "items.json").read_text()).values()
    }
    assert set(HEROES) <= catalogue_heroes and set(ITEMS) <= catalogue_items
    words = {
        "title": f"Dota 2 stream. {TITLE}",
        "handles": prompt([*handles, *HEROES, *ITEMS]),
        "aliases": prompt([*handles, *aliases, *HEROES, *ITEMS]),
        "rosters": prompt(handles),
        "runtime_aliases": prompt([*handles, *aliases]),
    }
    legs = []
    for path in sorted(args.archive.glob("*.opus")):
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg",
            "-nostdin",
            "-v",
            "error",
            "-i",
            str(path),
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-f",
            "s16le",
            "pipe:1",
            stdout=asyncio.subprocess.PIPE,
        )
        pcm, _ = await proc.communicate()
        assert proc.returncode == 0
        duration = await decoded_ms(path)
        assert duration == len(pcm) // 32
        legs.append((path.name, pcm))
    manifest = {
        "database": "kiroku_spike_28",
        "title": TITLE,
        "model": "whisper-large-v3-turbo",
        "temperature": 0,
        "vocabulary": words,
        "heroes": HEROES,
        "items": ITEMS,
        "aliases": ALIASES,
        "legs": [
            {
                "name": n,
                "ms": len(p) // 32,
                "samples": len(p) // 2,
                "pcm_sha256": hashlib.sha256(p).hexdigest(),
            }
            for n, p in legs
        ],
    }
    manifest["arm_scope"] = {
        "title": "Runtime control: category + title",
        "handles": "Oracle upper bound: roster handles + hindsight heroes/items",
        "aliases": "Oracle upper bound: handles + aliases + hindsight heroes/items",
        "rosters": "R1 runtime: title teams current roster handles only",
        "runtime_aliases": "R2 runtime: R1 + committed account-ID aliases; no heroes/items unless in title",
    }
    manifest["pacing_s"] = {
        "title": 4,
        "handles": 4,
        "aliases": 4,
        "rosters": 7,
        "runtime_aliases": 7,
    }
    manifest["pacing_note"] = (
        "Original arms slept in the result consumer; R1/R2 pace HTTP dispatch."
    )
    manifest["runtime_selection_time"] = "2026-10-14T00:00:00Z"
    manifest["runtime_team_ids"] = [team.id for team in chosen]
    manifest["runtime_selection"] = "No fixed hero/item set; no live match data"
    if previous_method is not None:
        for old, new in zip(previous_method["legs"], manifest["legs"], strict=True):
            assert all(old[key] == new[key] for key in ("name", "ms", "pcm_sha256")), (
                "archive changed"
            )
        for arm, value in previous_method["vocabulary"].items():
            assert words[arm] == value, f"{arm} Vocabulary changed"
    (out / "method.json").write_text(json.dumps(manifest, indent=2) + "\n")
    # Administrative connection creates the scratch DB only; never migrates dev.
    async with await psycopg.AsyncConnection.connect(
        "postgresql://kiroku:kiroku@127.0.0.1:5432/postgres", autocommit=True
    ) as conn:
        if not await (
            await conn.execute(
                "SELECT 1 FROM pg_database WHERE datname='kiroku_spike_28'"
            )
        ).fetchone():
            await conn.execute("CREATE DATABASE kiroku_spike_28")
    async with connect(DATABASE) as conn:
        await migrate(conn)

    async def pace(request):
        await asyncio.sleep(7)  # pace dispatch, not the independent result consumer

    async with (
        open_pool(DATABASE) as pool,
        httpx.AsyncClient(timeout=60, event_hooks={"request": [pace]}) as http,
    ):
        groq = (
            GroqTranscriber(http, settings.secrets.require(SecretName.GROQ))
            if args.arms
            else None
        )
        for arm in args.arms:
            assert groq is not None
            target = out / f"{arm}.jsonl"
            assert not target.exists(), (
                f"{target} already exists; select unfinished arms only"
            )

            async def events():
                offset = 0
                for _, pcm in legs:
                    for i in range(0, len(pcm), 32000):
                        yield AudioBlock(offset + i // 2, pcm[i : i + 32000])
                    offset += len(pcm) // 2
                    yield LegEnded()

            tail = ""
            with target.open("w") as f:
                async for result in groq.transcribe(
                    events(),
                    Context(
                        lambda: words[arm], Budget(pool, settings.caps).guard(None)
                    ),
                ):
                    text = " ".join(u.text for u in result.utterances)
                    if result.cost:
                        async with pool.connection() as conn:
                            await ledger.record(conn, result.cost)
                    row = {
                        "start_ms": result.start_ms,
                        "end_ms": result.end_ms,
                        "prompt": build_prompt(words[arm], tail),
                        "text": text,
                        "usd": str(result.cost.usd) if result.cost else "0",
                    }
                    f.write(json.dumps(row) + "\n")
                    f.flush()
                    tail = (tail + " " + text).strip() if text else tail
                    print(arm, result.start_ms, text[:100], flush=True)
        async with pool.connection() as conn:
            totals = await (
                await conn.execute("SELECT count(*), sum(usd) FROM cost_ledger")
            ).fetchone()
        rows = {
            a: [json.loads(s) for s in (out / f"{a}.jsonl").read_text().splitlines()]
            for a in words
            if (out / f"{a}.jsonl").exists()
        }
        if all(a in rows for a in args.arms) and len(rows) >= 3:
            boundaries = [
                [(r["start_ms"], r["end_ms"]) for r in rs] for rs in rows.values()
            ]
            assert all(b == boundaries[0] for b in boundaries)
            assert (
                boundaries[0][0][0] == 0
                and boundaries[0][-1][1] == sum(len(p) for _, p in legs) // 32
            )
            assert all(a[1] == b[0] for a, b in zip(boundaries[0], boundaries[0][1:]))
            billed = sum(Decimal(r["usd"]) for rs in rows.values() for r in rs)
            assert totals[1] == billed
            assert totals[0] == sum(
                Decimal(r["usd"]) > 0 for rs in rows.values() for r in rs
            )
            for arm, recordings in rows.items():
                tail = ""
                for row in recordings:
                    assert row["prompt"] == build_prompt(words[arm], tail), (
                        arm,
                        row["start_ms"],
                    )
                    if row["text"]:
                        tail = (tail + " " + row["text"]).strip()
        (out / "ledger.json").write_text(
            json.dumps(
                {
                    "database": "kiroku_spike_28",
                    "calls": totals[0],
                    "usd": str(totals[1]),
                },
                indent=2,
            )
            + "\n"
        )


p = argparse.ArgumentParser()
p.add_argument("archive", type=Path)
p.add_argument("output", type=Path)
p.add_argument(
    "--verify-only",
    action="store_true",
    help="Verify existing arms/ledger without paid calls",
)
p.add_argument(
    "--arms",
    nargs="+",
    choices=("title", "handles", "aliases", "rosters", "runtime_aliases"),
    default=["title", "handles", "aliases"],
)
asyncio.run(run(p.parse_args()))
