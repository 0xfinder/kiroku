# Web app verification

Run checks one at a time on the shared 2-vCPU, 3.7-GB host:

```sh
uv run ruff check
uv run ruff format --check
uv run pyright
uv run pytest -q
cd web
npm ci
npm run check
npm run test
npm run build
npx playwright install chromium
npm run e2e
```

Python tests require Postgres and an existing static build. For a fresh checkout, build
the web app before pytest, as CI does. Production starts with `uv run kiroku serve`
and binds `127.0.0.1:8737`. Use Tailscale or Cloudflare Access for remote access under D18.

The Chromium acceptance test starts the production CLI against its own migrated database.
It checks live updates without navigation or history refetch, two overlapping historical
Backfill notifications, Notes timestamp links, Rule edits and paid-test confirmation,
Review persistence and undo, and the absence of horizontal scrolling on all four views.
Backend tests also verify that Review and Telegram labels coexist for the same decision.

Eight screenshots cover Live, Notes, Rules and Review at 390x844 and 1280x900. The test
writes them to `docs/web/screenshots/{view}-{phone|desktop}.png` and attaches them to its
Playwright report. CI uploads both as the `web-acceptance` artifact. These generated files,
the static build, `.svelte-kit`, and `.web-e2e.json` stay out of Git.

## Type-check stall diagnosis on 2026-10-13

The cancelled run left both type checkers running for hours. On resumption, the original
configurations passed unchanged when run separately: verbose Pyright took 22.61 seconds
with 465,708 KB peak RSS; `npm run check` took 16.66 seconds with 550,796 KB peak RSS.
Pyright selected 79 Python files under `src` and `tests`. TypeScript selected 14 frontend
source, test and config files, with no generated output or dependency trees as root files.

SvelteKit 3's `$app/tsconfig` sets `types` to `["$app/types"]`. That generated module imports
the framework and environment declarations. The explicit `include` and `exclude` in
`web/tsconfig.json` preserve these types. The old `.svelte-kit/tsconfig.json` is a stale
SvelteKit 2 artifact and is not the config this project extends. See the
[SvelteKit configuration reference](https://svelte.dev/docs/kit/$app-tsconfig).

The host has no swap. At resumption, its five-minute memory-pressure averages were
62.52% for some stalled tasks and 51.63% for all active tasks stalled. The kernel counters
also recorded direct page reclaim. These observations and the isolated passes support
memory contention as the cause. The killed processes' stacks and memory history are
unavailable, so the exact earlier stall cannot be proved retrospectively. Sequential
execution fixes the observed operating condition. CI already uses sequential steps;
AGENTS.md now requires the same locally. No timeouts were increased or types weakened.

## Final local results

All commands ran sequentially on 2026-10-13. Wall times include command startup:

- `uv run ruff check`: passed, 0.08 seconds.
- `uv run ruff format --check`: 90 files formatted, 0.06 seconds.
- `uv run pyright`: no errors or warnings, 19.10 seconds.
- `uv run pytest -q`: 380 passed, 270.46 seconds.
- `npm run check`: no errors or warnings, 12.11 seconds, after moving the old
  `.svelte-kit` output aside and regenerating metadata from a fresh `npm ci` install.
- `npm run test`: five passed, 3.39 seconds.
- `npm run build`: static build passed, 7.14 seconds.
- `npm run e2e`: Chromium acceptance passed with eight screenshots, 19.24 seconds.

The first full pytest run exposed an existing channel-lock handoff race: after the claim
ended, immediate Backfill still saw the watch lock. Closing a client socket does not wait
for PostgreSQL to release its session locks. A regression keeps the real connection open
through claim cleanup and failed with "channel claim kept its advisory lock until socket
disconnect". Channel claims now await `pg_advisory_unlock_all()` on their dedicated
connection before closing it. Both the new regression and the original test passed, as
did the final full suite. The second Backfill fixture now emits distinct text so the
browser test can verify both insertions.

Final code review found a lost-update race in Rule edits: an omitted `enabled` wrote a
prior read's value over a concurrent enable or disable. The update now uses
`coalesce(parameter, enabled)`, preserving the current database value when the field is
omitted. Two HTTP regressions change enablement between the read and update. Before the
fix, the response incorrectly reported the original value in both cases; afterward,
both regressions and Rule CRUD passed. The full suite then passed with 380 tests.
The follow-up code review confirmed the fix and found no new actionable issues.

The read-only dev-database smoke attempt could not start: its schema predates the merged
Notes migrations, and `kiroku serve` refused with `SchemaOutOfDate`. No dev migration or
write was performed. T3 preview also refused browser startup because the host's AppArmor
policy blocks its sandbox. Local Playwright ran successfully. The eight acceptance
screenshots show synthetic data, not a live streamer session.
