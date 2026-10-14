# Public notebook deployment

These templates run as systemd user units, like the existing albedo and kei services.
The implementation PR does not install or start them.

1. Check out the merged code at `~/kiroku`. With the loopback compose Postgres
   available, run `uv sync --locked` and `.venv/bin/kiroku db migrate`.
2. Build the static app: `cd web && npm ci && npm run build`. Production runs Python;
   rebuild after an update and restart serve so its CSP hashes match the HTML.
3. Keep secrets in `~/.config/kiroku/secrets.env`, mode 600. Serve needs
   `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET` and `KIROKU_SESSION_SECRET`
   (a random value of at least 32 characters). Watch uses the existing provider
   and Telegram keys. Kiroku parses the file directly without shell interpolation.
4. The serve unit sets `KIROKU_OWNER_EMAIL=ethanoixe@gmail.com` and
   `KIROKU_PUBLIC_URL=https://kiroku.orulon.com`. Configure the Google client's
   authorized redirect URI as `https://kiroku.orulon.com/auth/callback`.
   Missing login configuration leaves everyone read-only. Login cookies need HTTPS.
5. Copy the three `.service` files into `~/.config/systemd/user/`. Validate with
   `systemd-analyze --user verify ~/.config/systemd/user/kiroku-*.service`, then
   `systemctl --user daemon-reload`. Enable lingering with
   `loginctl enable-linger "$USER"` so the user manager survives logout and starts
   at boot; lingering is already enabled on this host. If enabling it needs
   administrative authorization on a new host, have its administrator do it.
   Use `systemctl --user enable --now kiroku-serve.service kiroku-watch.service kiroku-tunnel.service`.
   Inspect with `systemctl --user status` and `journalctl --user -u kiroku-serve`.
6. Install cloudflared at `~/.local/bin/cloudflared`. Provision a remotely managed
   tunnel in Cloudflare and store its token in `~/.config/kiroku/tunnel-token`, mode
   600 (create the file privately; do not put the token in shell history). The
   tunnel unit reads that file directly, with no credentials JSON or local YAML.
   In Cloudflare, route `kiroku.orulon.com` to `http://127.0.0.1:8737`. Keep both
   Postgres and serve on loopback. Cloudflare Access is unnecessary: the app
   enforces owner writes and permits public reads.

The watch channels are set in one place: `ExecStart` in `kiroku-watch.service`.
One process watches both Caedrel and Gorgc, sharing Groq admission and pollers on
this 2-vCPU host. Watch gets 180 seconds to drain on SIGTERM. Both Kiroku units use
the bare binary so systemd signals reach its graceful shutdown.

Verify public reads signed out, then sign in as owner and check Rules and Review.
Install the PWA, disconnect, and check the offline shell without saved transcripts.
Transient identity errors retain unknown owner state and retry; closed SSE streams
retry with backoff. Offline recovery probes automatically and on returning to the app.
Logout clears this browser's session. Sessions expire after 12 hours. Secret rotation
invalidates sessions and pending logins; restarting serve preserves both. Login state,
nonce, PKCE verifier and ten-minute expiry live in a separate signed, HttpOnly flow
cookie scoped to `/auth`. Callbacks clear it even on failure; codes are single-use.

API admission is 240 requests/minute per IPv4 address or IPv6 /64. SSE permits four
streams per address/prefix and 32 total. NAT clients and IPv6 devices sharing a /64
share limits. The 4096-client LRU expires old entries and evicts the least recently
seen client when full. Only loopback peers may supply `CF-Connecting-IP`; Uvicorn
proxy-header rewriting is disabled. Admission is process-local, so serve stays at
one worker. Login flows are stateless and need no shared storage. Cloudflare edge
protections remain an operator concern.

## Landing page

`site/` is the static landing page for `https://orulon.com`. It needs no build step.
Commit changes under `site/`, then run `deploy/site.sh --dry-run` to inspect the
publish paths. Run `deploy/site.sh` to copy the page into
`/srv/orulon/releases/<UTC timestamp>-<short git sha>` and atomically flip
`/srv/orulon/current`. The script excludes `site/tools/`, makes directories 755
and files 644, and keeps every old release. It refuses missing HTML pages and
uncommitted files under `site/`, including ignored files. A repeat publish of the
same commit in the same second fails rather than overwriting a release.

Set `ORULON_ROOT` to publish elsewhere without sudo:

```sh
ORULON_ROOT=$(mktemp -d) deploy/site.sh
```

To roll back, replace the example release with a previous release path:

```sh
ln -sfn '/srv/orulon/releases/<previous-release>' /srv/orulon/current.next && mv -T /srv/orulon/current.next /srv/orulon/current
```

Caddy configuration is unchanged. It serves `current`, uses `404.html` for
errors, and caches `/assets/*` and `/favicon.svg` for one day. After editing the
CSS, bump its `?v=` query in both HTML pages. The favicon is copied from
`assets/icon.svg`.

For local visual checks, serve `site/` on loopback with `uv run python -m http.server
--bind 127.0.0.1 --directory site <port>`. After `cd web && npm ci`, run
`node site/tools/screenshots.mjs http://127.0.0.1:<port> /tmp/site-shots` from
the repository root. It checks local requests and horizontal overflow, then
saves phone, desktop and 360-pixel screenshots of both pages.
