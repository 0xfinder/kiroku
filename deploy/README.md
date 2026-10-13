# Public notebook deployment

These are templates for the operator. Installing them changes host services and routing;
the implementation PR does not install or start them.

1. Check out the merged code at `/home/finder/kiroku` (or adjust `User`,
   `WorkingDirectory` and `ExecStart` in both units). Start the compose Postgres,
   run `uv sync --locked`, and run `.venv/bin/kiroku db migrate`.
2. Build the static app: `cd web && npm ci && npm run build`. Production needs
   only Python; rebuild the shell after an update and restart serve so its CSP
   hashes match the installed HTML.
3. Keep secrets in `~/.config/kiroku/secrets.env`, mode 600. Serve needs
   `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET` and `KIROKU_SESSION_SECRET`
   (a random value of at least 32 characters). Watch uses the existing provider
   and Telegram keys from the same file. No systemd `EnvironmentFile` is needed:
   Kiroku parses that file directly, without shell interpolation.
4. The serve unit sets `KIROKU_OWNER_EMAIL=ethanoixe@gmail.com` and
   `KIROKU_PUBLIC_URL=https://kiroku.orulon.com`. The Google client's authorized
   redirect URI must be `https://kiroku.orulon.com/auth/callback`. Missing any
   login setting leaves everyone read-only. Do not change the URL to HTTP;
   login cookies require HTTPS.
5. Copy the units into `/etc/systemd/system/`, reload systemd, and enable/start
   `kiroku-serve` and the desired watch instances, for example
   `kiroku-watch@caedrel` and `kiroku-watch@gorgc`. Serve uses one worker;
   watch gets 180 seconds to drain on SIGTERM. Use the bare binary in both units.
6. Provision the Cloudflare Tunnel separately, substitute its UUID and credential
   file path in `cloudflared.yml.example`, and configure cloudflared to read it.
   Route `kiroku.orulon.com` to that tunnel. The ingress sends HTTP to the
   loopback server on port 8737. Keep Postgres and serve bound to loopback.
   Cloudflare Access is not required: reads are public and the app enforces owner
   writes. Do not expose any other origin port.

Verify the public notebook in a signed-out browser, then log in with the owner
Google account and check Rule editing and Review. Install the PWA and disconnect
network access: the shell should show an offline message, without saved transcripts.
Logout clears this browser's session. Rotating the session secret invalidates all
sessions; restarting serve invalidates pending logins. Sessions expire after 12 hours.

The API admits 240 requests/minute per IP. SSE is limited to four streams per IP
and 32 total; clients behind the same NAT share the limits. Only a loopback peer
may supply `CF-Connecting-IP`, so local processes are part of the trusted tunnel
boundary. Uvicorn proxy-header rewriting is disabled. Pending logins are limited to
four per IP and 128 total, with ten-minute expiry. Limits and pending logins are
process-local; multiple serve workers require shared admission/session-flow
storage. Cloudflare's own edge protections remain an operator concern.
