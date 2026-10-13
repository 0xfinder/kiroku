# Web app boundaries

`kiroku serve` starts one Uvicorn worker. FastAPI owns one query pool and a notification
hub with one dedicated LISTEN connection. SvelteKit builds an SPA into the Python package;
production runs no Node process. The API calls table owners for every read and write.

Session cards use `sessions.browse`; the latest Segment, latest Mode and counts arrive
in one SQL query. Session detail uses the same summary query. Transcript pages use
`transcript.page`; Notes and alerts use focused timeline reads. Gate owns the borderline
Review query and session passes. Labels owns Review upsert and undo, scoped by source.
Rules keep their existing validation. Dry runs add a bounded selection and structured
report shared with the CLI; the web process admits one paid test at a time.

The stream subscribes before reading. Notifications wake readers after commit; a five-second
poll covers listener outages. Forward transcript events use the audio clock as their SSE ID.
An audio offset represents a group of utterances, including comparisons by multiple
transcribers. An indexed Utterance-ID cursor detects new commits without counting historical rows.
Transcript commits lock their session row before allocating IDs, so visibility follows ID
order even with simultaneous live capture and Backfill. Each SSE pass bounds its forward
pages to the observed commit ID, avoiding an append being mistaken for late Backfill.
A `reconcile` event carries `{after_ms: earliest_affected_audio_start_ms - 1}`.

The browser gets `GET /api/sessions/{id}/transcript/cursor`, then pages the initial
transcript with `through_id` set to that ID. It opens SSE with `after_ms` and `after_id`,
covering commits between the initial scan and subscription. The first connection open
needs no second scan. Subsequent opens reconcile the full history; hints scan only from
the affected offset. Overlapping hints retain the earliest pending offset. Rows merge
by Utterance ID, retaining comparison transcribers.

The design comparison favored the shared hub for its fixed database connection count.
The per-request listener candidate supplied subscribe-before-read and historical
reconciliation. Both candidates identified unbounded session-specific dry runs, pending
Notes omitted by the CLI read, and the need for a source-scoped undo operation.


## Rule-test API

`POST /api/rules/{name}/test` validates the Rule, selectors (at most 100 Windows), stored
speech and lazy credentials before starting a background job. It returns HTTP 202 with
`{id, status: "running", completed, total}`. A second active test returns JSON 409.
`GET /api/rule-tests/{id}` returns the same progress fields, then either
`{status: "completed", report: {...}}` with the existing trials, histogram, extremes and
text report, or `{status: "failed", error: "..."}`. Progress counts completed trials after
their costs are stored. Request disconnects do not cancel the job. The UI polls once a
second and lets the user resume fetching results after a failed poll without starting
another paid test. Server shutdown cancels the job alongside the listener within five
seconds. Only the latest job is retained in memory; starting the next valid test discards
its predecessor, and a process restart discards all jobs. Unknown IDs return JSON 404.

A Rule wording save includes `enabled` only if the checkbox changed since loading;
new Rules always send it. This preserves concurrent CLI disablement.

The write guard accepts the configured public HTTPS origin. Forwarded Host headers
do not authorize writes. The app serves no CORS headers, including on preflights. Every unknown `/api/*` method
lands in a JSON 404 handler before the SPA. Real GET routes support HEAD; the SSE HEAD
returns headers without starting a subscription.


## Public access and offline shell (D35)

The public navigation shows Live, Notes and Rules. Rules appear as readable cards,
with their instructions, threshold, Cooldown, Pack and enabled state. Editing, new
Rule controls, paid tests and Review appear only after `/api/me` returns owner.
Direct public navigation to Review shows an owner-login message with no voting
controls. `/api/auth` advertises whether login is configured, keeping the owner-login
link absent when it cannot work. Server owner dependencies enforce the same boundary.

The header offers Owner login or Log out. Google OIDC returns to `/auth/callback`;
logout posts with the existing Origin guard and clears the email cookie. Public
read access needs no credentials. Owner sessions last 12 hours. All API/auth responses
use `Cache-Control: no-store`, including errors and `/api/me`.

The manifest and generated icons support standalone installation. The service worker
precaches build assets and `/`, removes obsolete version caches on activation, and
uses network-first navigation with a cached-shell fallback. It never intercepts API,
auth or SSE. Offline mode unmounts data views, clears owner UI state and displays
“You’re offline” with a reconnect instruction. Failed network requests and gateway
errors also select the offline view when the device still has internet access.
Retry connection reloads identity before restoring data views. Delayed responses
from earlier requests cannot clear the offline state.
Online mode fetches identity again and
remounts views, which reload current database data rather than showing stale speech.

The CSP allows only same-origin scripts and hashes of installed bootstrap scripts;
inline styles support dynamic meters and layout properties. Chromium acceptance
checks public/owner controls at phone width, actual local OIDC login and logout,
manifest/icons, worker installation and obsolete-cache cleanup, offline navigation,
network-only API/auth access and absence of CSP violations.
