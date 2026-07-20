# End-to-end smoke (HookMyApp sandbox)

Acceptance: fresh install receives one WhatsApp text and the agent reply
arrives (AIT-222). Run before every release tag.

1. Fresh machine or clean `~/.hermes`: `hermes plugins install hookmyapp/hermes-whatsapp`
2. `pip install aiohttp`
3. `npm install -g @gethookmyapp/cli && hookmyapp login`
4. `hermes hookmyapp setup` — pick the sandbox channel; confirm
   `~/.hermes/.env` gained the six channel vars (values NOT printed).
5. Add your phone's wa_id: `HOOKMYAPP_ALLOWED_USERS=<your wa_id>` in `~/.hermes/.env`.
6. Terminal A: `hookmyapp channels listen <channel> --port 8649 --path /hookmyapp/webhook`
7. Terminal B: `hermes gateway restart` (or start), then `hermes hookmyapp status`
   — expect `check: ok`.
8. `curl -s http://localhost:8649/health` — expect `{"status": "ok", ...}`.
9. From your phone, WhatsApp the sandbox number: `ping from smoke test`.
10. PASS when: the agent's reply arrives on your phone within ~30s.
11. Negative check: `curl -s -o /dev/null -w '%{http_code}' -X POST
    http://localhost:8649/hookmyapp/webhook -d '{}'` — expect `401`
    (unsigned inbound rejected).
12. Probe check: `curl -s -H 'X-HookMyApp-Probe: webhook-verification'
    http://localhost:8649/hookmyapp/webhook` — expect the VERIFY_TOKEN value
    echoed (this is the one place it prints; it is not a secret like the HMAC).
