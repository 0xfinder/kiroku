"""The registered mutation check cases.

Every case is evidence-backed from the planning probes: it has a passing control on the
current code, a textual mutation applied to a scratch copy of `src`, and focused tests that
fail with the named mismatch on the mutant. Cases that merely re-run existing checks stay out.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Patch:
    """A replacement applied to exactly one copy of one source file, never the checkout."""

    file: str
    old: str
    new: str


@dataclass(frozen=True)
class Case:
    name: str
    owner: str
    invariant: str
    # pytest node ids. `::test_name` without a parameter matches every parametrized
    # instance of it; `::test_name[param]` matches exactly that instance.
    fail_nodes: tuple[str, ...]
    still_pass_nodes: tuple[str, ...] = field(default=())
    patches: tuple[Patch, ...] = field(default=())
    # A pytest plugin written into the scratch PYTHONPATH instead of textual patches.
    plugin_code: str | None = field(default=None)


DURABLE_PLUGIN = """\
\"\"\"pytest plugin for the historical Notes durable mutation: replay a27ce78's function.\"\"\"\n

import ast
import linecache
import subprocess

import kiroku.notes as notes


def pytest_configure() -> None:
    source = subprocess.check_output(
        ["git", "show", "a27ce78:src/kiroku/notes.py"], text=True
    )
    tree = ast.parse(source)
    function = next(
        n
        for n in tree.body
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "durable"
    )
    isolated = ast.Module(body=[function], type_ignores=[])
    filename = "historical-a27ce78-notes-durable"
    linecache.cache[filename] = (
        len(source),
        None,
        source.splitlines(keepends=True),
        filename,
    )
    exec(compile(isolated, filename, "exec"), notes.__dict__)
"""

# Recorded source snippets, hoisted to module level for exactness and line length.
JEV_TERMINAL_OLD = (
    "                raise GateBadResponse(\n"
    '                    f"jev HTTP {status}: {response.text[:200]}",\n'
    "                    self._lost_costs(request.purpose, estimate, maybe_billed),\n"
    "                )"
)
JEV_TERMINAL_NEW = (
    "                raise GateBadResponse(\n"
    '                    f"jev HTTP {status}: {response.text[:200]}",\n'
    "                    (),\n"
    "                )"
)
WHISPER_400_OLD = (
    "                lost = uncertain_costs(start_ms, end_ms, maybe_billed)\n"
    '                return Result(start_ms, end_ms, (), lost[0] if lost else None), ""'
)
WHISPER_400_NEW = '                return Result(start_ms, end_ms, (), None), ""'
WHISPER_AUTH_OLD = (
    "                raise TranscriberAuthError(\n"
    '                    f"Groq rejected the API key (HTTP {status})",\n'
    "                    uncertain_costs(start_ms, end_ms, maybe_billed),\n"
    "                )"
)
WHISPER_AUTH_NEW = (
    "                raise TranscriberAuthError(\n"
    '                    f"Groq rejected the API key (HTTP {status})",\n'
    "                    uncertain_costs(start_ms, end_ms, 0),\n"
    "                )"
)
GATE_AUTH_OLD = (
    "            except (GateAuthError, LLMDatabaseError) as e:\n"
    "                if e.costs:\n"
    "                    await ledger.record_session_costs(self._pool, self._session_id, e.costs)\n"
    "                raise"
)
GATE_AUTH_NEW = "            except (GateAuthError, LLMDatabaseError) as e:\n                raise"
CONTEXT_GUARD_OLD = (
    '                "AND pack IS NOT DISTINCT FROM %s AND mode IS NOT DISTINCT FROM %s "'
)
CONTEXT_GUARD_NEW = '                "AND mode IS NOT DISTINCT FROM %s "'
CONTEXT_PARAMS_OLD = (
    "                [span.session_id, start, start - 600_000, pack, mode, game_key,"
    " game_key, start],"
)
CONTEXT_PARAMS_NEW = (
    "                [span.session_id, start, start - 600_000, mode, game_key, game_key, start],"
)

CASES: tuple[Case, ...] = (
    Case(
        name="notes-durable-cancellation",
        owner="kiroku.notes.durable (a27ce78)",
        invariant=(
            "A crash or cancellation during the notified write pays nothing for spans "
            "the process never confirmed: the terminal span ends at the durable result."
        ),
        fail_nodes=(
            "tests/test_notes.py::"
            "test_failed_write_during_cancellation_does_not_pay_for_later_spans",
        ),
        plugin_code=DURABLE_PLUGIN,
    ),
    Case(
        name="jev-terminal-4xx-keeps-prior-estimates",
        owner="kiroku.gate.jev JevGate.evaluate",
        invariant=(
            "Dispatches lost earlier in one evaluate (sent, response never seen) keep "
            "their conservative estimate when a terminal 4xx ends the request."
        ),
        fail_nodes=("tests/test_paid_exits.py::test_jev_a_lost_dispatch_keeps_one_estimate",),
        patches=(
            Patch(
                file="src/kiroku/gate/jev.py",
                old=JEV_TERMINAL_OLD,
                new=JEV_TERMINAL_NEW,
            ),
        ),
    ),
    Case(
        name="whisper-terminal-400-keeps-prior-estimates",
        owner="kiroku.transcriber.groq Whispers._call",
        invariant=(
            "A lost dispatch followed by a terminal 4xx returns an empty Result that "
            "still carries the lost chunk's billing estimate, committed exactly once."
        ),
        fail_nodes=(
            "tests/test_paid_exits.py::test_whisper_a_lost_dispatch_keeps_one_estimate_on_400",
        ),
        patches=(
            Patch(
                file="src/kiroku/transcriber/groq.py",
                old=WHISPER_400_OLD,
                new=WHISPER_400_NEW,
            ),
        ),
    ),
    Case(
        name="whisper-terminal-auth-keeps-prior-estimates",
        owner="kiroku.transcriber.groq Whispers._call",
        invariant=(
            "A lost dispatch followed by a terminal auth rejection raises "
            "TranscriberAuthError with the lost chunk's estimate for the consumer ledger."
        ),
        fail_nodes=(
            "tests/test_paid_exits.py::test_whisper_a_lost_dispatch_keeps_one_estimate_on_auth",
        ),
        patches=(
            Patch(
                file="src/kiroku/transcriber/groq.py",
                old=WHISPER_AUTH_OLD,
                new=WHISPER_AUTH_NEW,
            ),
        ),
    ),
    Case(
        name="gate-auth-error-still-records-its-costs",
        owner="kiroku.gating GateLoop._pass (primary auth)",
        invariant=(
            "When the primary Gate rejects with auth mid-pass, the terminal failed "
            "Window's estimate still reaches the ledger transaction and stays unstored."
        ),
        fail_nodes=(
            "tests/test_gating.py::"
            "test_an_auth_error_through_a_terminal_window_still_records_its_costs",
        ),
        patches=(Patch(file="src/kiroku/gating.py", old=GATE_AUTH_OLD, new=GATE_AUTH_NEW),),
    ),
    Case(
        name="historical-backlog-does-not-inherit-live-context",
        owner="kiroku.notes.prepare (previous-note scoping)",
        invariant=(
            "Previous-note selection never crosses historical Pack, Mode or Game key "
            "boundaries; a later note cannot draft its context from stale pack speech."
        ),
        fail_nodes=("tests/test_notes.py::test_previous_is_recent_and_has_same_context[pack]",),
        still_pass_nodes=(
            "tests/test_notes.py::test_previous_is_recent_and_has_same_context[none]",
        ),
        patches=(
            Patch(
                file="src/kiroku/notes.py",
                old=CONTEXT_GUARD_OLD,
                new=CONTEXT_GUARD_NEW,
            ),
            Patch(
                file="src/kiroku/notes.py",
                old=CONTEXT_PARAMS_OLD,
                new=CONTEXT_PARAMS_NEW,
            ),
        ),
    ),
)

# Self-check experiments (no control of their own): the runner must reject these shapes
# instead of counting them as detections.
INVALID_EXPERIMENT = Patch(
    file="src/kiroku/transcriber/groq.py",
    old="    async def _call(self, chunk: Chunk, prompt: str, background: bool)",
    new="    def _broken_experiment_callback",  # a filename, not Python: collection error
)
STALE_PATCH = Patch(
    file="src/kiroku/transcriber/groq.py",
    old="RETIRED-HISTORICAL-SNIPPET-THAT-NO-LONGER-EXISTS",
    new="anything",
)

SELF_CHECK_NODES = (
    "tests/test_paid_exits.py::test_whisper_a_lost_dispatch_keeps_one_estimate_on_400",
)
