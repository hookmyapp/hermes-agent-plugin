# HookMyApp WhatsApp — next steps

1. Install the runtime dependency (hermes does not install plugin deps):
   ```bash
   pip install aiohttp
   ```

2. Run the wizard: `hermes hookmyapp setup`
   (installs nothing without asking; add `--install-cli` to auto-install
   `@gethookmyapp/cli` via npm). It pulls your channel credentials into
   `~/.hermes/.env`.

3. Start the transport — pick one:
   - **Local (default, no public URL):**
     ```bash
     hookmyapp channels listen <channel> --port 8649 --path /hookmyapp/webhook
     ```
     (HookMyApp provisions a Cloudflare Tunnel to this adapter; keep it running)
   - **Server with public HTTPS:**
     ```bash
     hermes hookmyapp setup --webhook-url https://your-host/hookmyapp/webhook
     ```

4. Allow senders (default-closed — the agent replies to nobody until you do):
   Set `HOOKMYAPP_ALLOWED_USERS=<wa_id,...>` in `~/.hermes/.env`
   (or `HOOKMYAPP_ALLOW_ALL_USERS=true` for dev only).

5. `hermes gateway restart`, then send your WhatsApp number a message.

Check anytime with `hermes hookmyapp status`.

## Releases

You installed the `release` branch (the repo default) — that's production.
`main` is staging. Production updates ship via `v*` tags, which promote
tested commits from `main` into `release`.
