# HookMyApp WhatsApp — Hermes Platform Plugin

Receive and reply to WhatsApp messages through [HookMyApp](https://hookmyapp.io) within the [Hermes Agent](https://github.com/NousResearch/hermes-agent) framework.

This plugin listens for Meta WhatsApp Cloud API webhooks forwarded by HookMyApp, verifies them with HMAC signatures, and dispatches messages to your Hermes gateway instance. Replies flow through the HookMyApp gateway back to WhatsApp. **One channel per gateway v1** — a single Hermes gateway runs one HookMyApp WhatsApp channel.

## Install

```bash
hermes plugins install hookmyapp/hermes-whatsapp
pip install aiohttp
```

The plugin clones this repository bare; `pip install` is required because Hermes does not install plugin-specific Python dependencies.

## Setup

### Wizard (recommended)

```bash
hermes hookmyapp setup
```

The wizard:
- Prompts for your HookMyApp channel credentials.
- Interactively selects a single channel if you have multiple.
- Writes credentials to `~/.hermes/.env`.
- Optionally installs the HookMyApp CLI (`--install-cli`).

### Manual configuration

Retrieve your channel credentials:

```bash
hookmyapp channels env <channel> --write ~/.hermes/.env
```

Then add these required environment variables to `~/.hermes/.env`:

| Variable | Source | Example |
|----------|--------|---------|
| `META_GRAPH_API_URL` | `hookmyapp channels env <channel>` | `https://api.hookmyapp.io/gateway/v1` |
| `WHATSAPP_ACCESS_TOKEN` | Channel settings (HookMyApp app) | `hmat_0000000000example` |
| `WHATSAPP_PHONE_NUMBER_ID` | Channel settings | `1234567890123` |
| `WEBHOOK_HMAC_SECRET` | Channel settings → Webhook config | `48f2d1e6a5c3b9f7e2k1l0m9n8o7p6q5` |
| `VERIFY_TOKEN` | Channel settings → Webhook config | `your_verify_token_here` |

Optional variables (with defaults):

| Variable | Default | Purpose |
|----------|---------|---------|
| `HOOKMYAPP_HOST` | `0.0.0.0` | Listener bind interface |
| `HOOKMYAPP_PORT` | `8649` | Listener port |
| `HOOKMYAPP_WEBHOOK_PATH` | `/hookmyapp/webhook` | Webhook endpoint path |
| `HOOKMYAPP_CHANNEL_ID` | — | Channel id (diagnostics only) |
| `HOOKMYAPP_ALLOWED_USERS` | — | Comma-separated WhatsApp IDs (wa_ids) allowed to send messages (default-closed) |
| `HOOKMYAPP_ALLOW_ALL_USERS` | — | Set to `true` to allow all senders (dev/testing only) |
| `HOOKMYAPP_HOME_CHANNEL` | — | Default chat id for cron and scheduled deliveries |

## Transports (listen-first)

This plugin supports two webhook delivery modes. **Always start with the local transport** (the default); only use the server transport for production deployments with public HTTPS.

### 1. Local (Cloudflare Tunnel) — No public URL needed

```bash
hookmyapp channels listen <channel> --port 8649 --path /hookmyapp/webhook
```

HookMyApp auto-provisions a Cloudflare Tunnel endpoint and routes webhooks to your local adapter. Headers (including HMAC signatures) are forwarded verbatim, so signature verification works identically to a public server.

**Keep this process running** for the agent to receive messages.

### 2. Server with public HTTPS

For environments where a local tunnel is unavailable:

```bash
hermes hookmyapp setup --webhook-url https://your-host/hookmyapp/webhook
```

Update your channel's webhook URL in HookMyApp to point to your server's public address. The adapter listens on the same local port (default `8649`) and path (default `/hookmyapp/webhook`).

**Firewall:** Ensure `HOOKMYAPP_PORT` (default 8649) is accessible from the internet, or proxy through a reverse proxy (nginx, Caddy, etc.) on port 443.

## Security

### HMAC verification (X-HookMyApp-Signature-256)

Every webhook includes an `X-HookMyApp-Signature-256` header containing an HMAC-SHA256 signature of the request body, signed with your `WEBHOOK_HMAC_SECRET`. The adapter verifies this signature before dispatching the message to the agent loop.

- **Verification is mandatory** — the adapter refuses to start if `WEBHOOK_HMAC_SECRET` is missing.
- **Constant-time comparison** prevents timing-based attacks.
- **No disable switch** — HMAC verification cannot be disabled.

### Verify Token vs. HMAC

**These are two distinct concepts; do not conflate them:**

- **Verify Token** — A subscription-handshake value sent by Meta during webhook subscription confirmation (the GET probe). It is not a signing key.
- **HMAC Secret** — The key used to sign every delivered webhook payload (the POST body). It is only for signature verification.

Both are stored in your HookMyApp channel settings. Rotation of either requires updating the env var and restarting the adapter.

### Verification probes

Meta sends unsigned GET requests to your webhook endpoint during subscription verification. These requests echo the `hub.challenge` query parameter and are never dispatched to the agent — they only validate that your endpoint is alive and listening.

### Allowlist (default-closed)

By default, the adapter accepts webhooks from any sender and queues them for the agent. **To restrict inbound senders:**

Set `HOOKMYAPP_ALLOWED_USERS` to a comma-separated list of WhatsApp IDs:

```bash
HOOKMYAPP_ALLOWED_USERS=441234567890,441234567891
```

Or allow all senders (dev/testing only):

```bash
HOOKMYAPP_ALLOW_ALL_USERS=true
```

## Delivery Semantics (Important)

**Read this to understand what happens if your adapter or Hermes crashes.**

The adapter replies with HTTP 200 only after every event in a delivery has been:
1. Verified with the HMAC signature.
2. Successfully handed off to the agent loop.

If verification fails or handoff fails, the adapter returns HTTP 500, and the delivery is retried by the forwarder.

Once the adapter sends a 200 to the forwarder, the forwarder relays that acknowledgement to Meta, and **the message is not retried**.

**Critical:** If the process crashes between the adapter sending a 200 and the agent loop processing the message, the message is **lost permanently**. v1 has no durable queue; in-memory deduplication prevents *duplicate* deliveries within a session, but it cannot recover messages lost to a crash.

For production deployments, plan for this. Options include:
- Monitor Hermes uptime and restart on crash.
- Periodically poll the HookMyApp Deliveries API to audit message flow.
- Accept the loss window as part of the reliability model (acceptable for many use cases).

## Media

Inbound media (images, video, audio, documents) arrives as a media ID. The adapter:

1. **Resolves the media ID** via `GET {META_GRAPH_API_URL}/{media_id}` to fetch metadata and a signed download URL.
2. **Downloads the media immediately** while the signed URL is valid (short-lived, ~15 minutes).
3. **Caches bytes locally** for Hermes vision, STT (speech-to-text), and document tooling.

**Audio/Voice notes:**
- Downloaded as `.ogg` (Opus codec).
- Hermes STT runs on the cached file.
- v1 does not include Meta's transcript.

**Download failure:**
- If the media cannot be downloaded, the message is forwarded to the agent **as text only** (the media ID is included in the metadata; the agent can attempt to fetch it manually if needed).
- Transient network errors are retried up to 2 times.

**Supported media types:** images, video, audio, documents (PDF, DOCX, etc.), stickers.

## Formatting and Limits

WhatsApp text messages support a limited formatting subset:

- Bold: `*text*`
- Italic: `_text_`
- Strikethrough: `~text~`
- Monospace: `` `text` ``

**Message length:** 4096 characters. Longer replies are split into multiple messages.

**Session window:** The 24-hour session window applies. Replies outside this window require re-initiating the conversation through WhatsApp (the user must send a new message first).

## Diagnostics

### Status summary

```bash
hermes hookmyapp status
```

Displays:
- Listener address and port
- Webhook path
- Active allowed senders (if restricted)
- Hermes gateway connection status

### Health check

```bash
curl http://localhost:8649/health
```

Returns HTTP 200 with a JSON summary if the adapter is running.

### Common failures

| Error | Cause | Fix |
|-------|-------|-----|
| `401 Unauthorized on webhook` | Wrong or rotated HMAC secret | Run `hookmyapp channels env <channel> --write ~/.hermes/.env` and restart |
| `Verification probe fails` | Missing `VERIFY_TOKEN` | Set `VERIFY_TOKEN` in `~/.hermes/.env` and restart |
| `Agent loop queue full` | Hermes is overloaded or hung | Check Hermes logs and restart gateway |
| `Connection refused on META_GRAPH_API_URL` | Wrong gateway URL or network issue | Verify `META_GRAPH_API_URL` and firewall settings |

### Logs

The adapter logs to stderr. Enable debug logging:

```bash
LOGLEVEL=DEBUG hermes run
```

## Tested Against

- Hermes commit: `abc123def456` (pinned from SDD Task 1)
- Python 3.11+
- aiohttp 3.9+
