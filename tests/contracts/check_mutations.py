#!/usr/bin/env python3
"""Run the registered mutation checks: historical regressions a supported path must reject.

Each registered case replays a proven historical defect from the planning probes. The
runner proves, for every case, that the control passes on the current code and that the
case's exact mutation makes its focused tests fail with the named mismatch. Mutations
apply to a fresh scratch copy of `src`, run in fresh subprocesses, and are cleaned up:
the active checkout is never touched, and a stale patch, an invalid experiment or an
unavailable tool fails the check instead of appearing green. CI runs the identical
command.

Usage:
    uv run python tests/contracts/check_mutations.py          # self-checks + all cases
    uv run python tests/contracts/check_mutations.py --case N # one case
    uv run python tests/contracts/check_mutations.py --list   # print the cases
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn, cast

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))  # a script run finds the tests package at the repo root

from tests.contracts.cases import (  # noqa: E402 - after the path bootstrap
    CASES,
    INVALID_EXPERIMENT,
    SELF_CHECK_NODES,
    STALE_PATCH,
    Case,
    Patch,
)

# Distinct classifications; nonzero is a named failure, never a silent pass.
OK, TOOLS, STALE, INVALID, UNDETECTED, VIOLATION = range(6)
_NAMES = {
    OK: "pass",
    TOOLS: "tool-unavailable",
    STALE: "stale-mutation",
    INVALID: "invalid-experiment",
    UNDETECTED: "undetected-regression",
    VIOLATION: "checkout-violation",
}

_CHILDREN: set[subprocess.Popen[bytes]] = set()
_SCRATCHES: list[Path] = []


class CheckFailed(Exception):
    def __init__(self, kind: int, message: str) -> None:
        super().__init__(message)
        self.kind = kind


@dataclass(frozen=True)
class RunResult:
    code: int
    output: str

    @property
    def failed_nodes(self) -> list[str]:
        return re.findall(r"^FAILED (\S+)", self.output, re.MULTILINE)

    @property
    def passed_count(self) -> int:
        match = re.search(r"(\d+) passed", self.output)
        return int(match.group(1)) if match else 0

    @property
    def failed_count(self) -> int:
        match = re.search(r"(\d+) failed", self.output)
        return int(match.group(1)) if match else 0


def _scratch(name: str) -> Path:
    scratch = Path(tempfile.mkdtemp(prefix=f"kiroku-contract-{name}-"))
    _SCRATCHES.append(scratch)
    shutil.copytree(SRC, scratch / "src", ignore=shutil.ignore_patterns("__pycache__"))
    return scratch


def _release(scratch: Path) -> None:
    shutil.rmtree(scratch, ignore_errors=True)
    if scratch in _SCRATCHES:
        _SCRATCHES.remove(scratch)


def _apply(patch: Patch, scratch: Path) -> None:
    """Substitute the recorded source exactly once inside one scratch copy."""
    target = scratch / patch.file
    text = target.read_text()
    count = text.count(patch.old)
    if count != 1:
        raise CheckFailed(
            STALE,
            f"patch no longer applies to {patch.file}: expected exactly one occurrence,"
            f" found {count}",
        )
    target.write_text(text.replace(patch.old, patch.new))


def _plugin(case: Case) -> tuple[Path, str] | None:
    if case.plugin_code is None:
        return None
    plugins = _scratch(f"{case.name}-plugin")
    module = f"contract_{case.name.replace('-', '_')}"
    (plugins / f"{module}.py").write_text(case.plugin_code)
    return plugins, module


def _pythonpath(entries: list[Path]) -> dict[str, str]:
    env = os.environ.copy()
    existing = [env["PYTHONPATH"]] if env.get("PYTHONPATH") else []
    env["PYTHONPATH"] = os.pathsep.join([str(entry) for entry in entries] + existing)
    return env


def _pytest(
    nodes: tuple[str, ...],
    *,
    scratch: Path | None = None,
    plugin: tuple[Path, str] | None = None,
) -> RunResult:
    command = [
        sys.executable,
        "-m",
        "pytest",
        *nodes,
        "-q",
        "--no-header",
        "--tb=short",
        "-rf",
        "-p",
        "no:cacheprovider",
    ]
    if plugin is not None:
        command += ["-p", plugin[1]]
    env = _pythonpath(
        ([] if scratch is None else [scratch / "src"]) + ([] if plugin is None else [plugin[0]])
    )
    proc = subprocess.Popen(
        command,
        cwd=ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    _CHILDREN.add(proc)
    try:
        try:
            assert proc.stdout is not None
            output = proc.communicate(timeout=900)[0].decode(errors="replace")
            return RunResult(proc.returncode, output)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            raise CheckFailed(TOOLS, f"the focused run hung past its deadline: {nodes}") from None
    finally:
        _CHILDREN.discard(proc)


def _interrupted(signum: int, _frame: object) -> None:
    """Release owned children and scratch resources, then die by that signal."""
    print("interrupted: releasing owned children and scratch resources", file=sys.stderr)
    for proc in list(_CHILDREN):
        proc.kill()
    for scratch in list(_SCRATCHES):
        shutil.rmtree(scratch, ignore_errors=True)
    signal.signal(signum, signal.SIG_DFL)
    os.kill(os.getpid(), signum)


STATUS_CMD = ("git", "status", "--porcelain")


def _status_snapshot() -> str:
    return subprocess.run(
        STATUS_CMD, cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout.strip()


def _checkout_clean(before: str | None = None) -> None:
    """No new local change since the run started (the tree may carry the branch's own)."""
    status = _status_snapshot()
    if before is not None and status != before:
        raise CheckFailed(
            VIOLATION,
            f"the checkout changed during the run:\nbefore:\n{before}\nafter:\n{status}",
        )
    if before is None and status:
        # Recorded once at startup; the run itself must add nothing.
        raise CheckFailed(VIOLATION, f"unexpected checkout state:\n{status}")


def _split_node(node: str) -> tuple[str, str | None]:
    """`tests/x.py::test_name[param]` -> (`test_name`, `param`); no param -> (`test_name`, None)."""
    test, _, rest = node.rsplit("::", 1)[1].partition("[")
    return test, (rest[:-1] if rest else None)


def run_case(case: Case) -> None:
    """Control on the checkout, then the mutant in a scratch copy: mismatch or fail."""
    scratch = _scratch(case.name)
    plugin = _plugin(case)
    try:
        for patch in case.patches:
            _apply(patch, scratch)
        control = _pytest(case.fail_nodes + case.still_pass_nodes)
        if control.code != 0 or control.failed_count:
            raise CheckFailed(TOOLS, f"control did not pass:\n{control.output[-2000:]}")
        mutant = _pytest(
            case.fail_nodes + case.still_pass_nodes,
            scratch=scratch,
            plugin=plugin,
        )
        requirements: list[tuple[str, str | None]] = [_split_node(n) for n in case.fail_nodes]
        pass_ids = set(case.still_pass_nodes)
        observed: list[str] = []
        extra: list[str] = []
        for node in mutant.failed_nodes:
            if node in pass_ids:
                extra.append(node)
                continue
            base, param = _split_node(node)
            for req in requirements:
                if req[0] == base and (req[1] is None or req[1] == param):
                    requirements.remove(req)
                    observed.append(node)
                    break
            else:
                extra.append(node)
        if requirements or extra:
            detail: list[str] = []
            if requirements:
                detail.append(f"expected failures absent: {requirements}")
            if extra:
                detail.append(f"unexpected failures: {extra}")
            what = "; ".join(detail)
            if mutant.code == 0:
                raise CheckFailed(UNDETECTED, f"{what}; the mutant ran silently green")
            raise CheckFailed(UNDETECTED, f"{what}\nmutant output:\n{mutant.output[-1500:]}")
    finally:
        _release(scratch)
    print(
        f"case {case.name}: detected\n"
        f"  owner     {case.owner}\n"
        f"  invariant {case.invariant}\n"
        f"  control   {control.passed_count} passed on the current code\n"
        f"  observed  {', '.join(mutant.failed_nodes)}"
    )


def _invalid_experiment() -> None:
    """Broken source must classify as an invalid experiment, not count as a detection."""
    being = _scratch("selfcheck-invalid")
    try:
        _apply(INVALID_EXPERIMENT, being)
        probe = _pytest(SELF_CHECK_NODES, scratch=being)
        if probe.code in (0, 1) or probe.failed_nodes:
            raise CheckFailed(
                INVALID,
                f"an invalid experiment must not run any test (pytest exit {probe.code},"
                f" observed failed nodes {probe.failed_nodes})",
            )
        print(f"self-check invalid-experiment: rejected (pytest exit {probe.code})")
    finally:
        _release(being)


def _stale_patch() -> None:
    """A patch with no current match must fail the run, naming case and source file."""
    ghost = _scratch("selfcheck-stale")
    try:
        try:
            _apply(STALE_PATCH, ghost)
            raise CheckFailed(STALE, "the stale probe unexpectedly applied")
        except CheckFailed as failure:
            if failure.kind != STALE:
                raise
            print(f"self-check stale-mutation: rejected, naming {STALE_PATCH.file}")
    finally:
        _release(ghost)


def _run(only: set[str]) -> int:
    status = OK
    before = _status_snapshot()
    if not only:
        for check in (_invalid_experiment, _stale_patch):
            try:
                check()
            except CheckFailed as failure:
                print(f"FAILED [{_NAMES[failure.kind]}]: {failure}")
                status = failure.kind
    for case in CASES:
        if only and case.name not in only:
            continue
        try:
            run_case(case)
        except CheckFailed as failure:
            print(f"FAILED [{_NAMES[failure.kind]}] {case.name}: {failure}")
            status = max(status, failure.kind)
    try:
        _checkout_clean(before)
    except CheckFailed as failure:
        print(f"FAILED [{_NAMES[failure.kind]}]: {failure}")
        status = failure.kind
    return status


def main() -> NoReturn:
    signal.signal(signal.SIGINT, _interrupted)
    signal.signal(signal.SIGTERM, _interrupted)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--case",
        action="append",
        help="run only this case (repeatable); default: self-checks and all cases",
    )
    parser.add_argument("--list", action="store_true", help="print the cases and exit")
    args = parser.parse_args()
    only = set(cast("list[str] | None", args.case) or [])
    unknown = only - {case.name for case in CASES}
    if unknown:
        print(f"unknown case: {sorted(unknown)}", file=sys.stderr)
        sys.exit(INVALID)
    if args.list:
        for case in CASES:
            print(f"{case.name}: {case.invariant}")
        sys.exit(OK)
    status = _run(only)
    for scratch in list(_SCRATCHES):
        shutil.rmtree(scratch, ignore_errors=True)
    if status:
        sys.exit(status)
    print("all checks passed: every registered regression is a supported rejection")
    sys.exit(OK)


if __name__ == "__main__":
    main()
