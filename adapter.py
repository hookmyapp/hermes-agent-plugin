"""HookMyApp WhatsApp platform adapter for the Hermes Agent framework.

Single-file plugin (Kapso pattern). Imports of aiohttp and hermes internals
are guarded so this module imports without either installed (bare git clone).
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections import OrderedDict
from pathlib import Path

try:
    import aiohttp
    from aiohttp import web
except ImportError:  # bare git clone without deps — surfaced via check_fn/install_hint
    aiohttp = None
    web = None

_log = logging.getLogger("hookmyapp")

# --- config ---------------------------------------------------------------

DEFAULTS = {
    "HOOKMYAPP_HOST": "0.0.0.0",
    "HOOKMYAPP_PORT": "8649",
    "HOOKMYAPP_WEBHOOK_PATH": "/hookmyapp/webhook",
}

REQUIRED_ENV = [
    "META_GRAPH_API_URL",
    "WHATSAPP_ACCESS_TOKEN",
    "WHATSAPP_PHONE_NUMBER_ID",
    "WEBHOOK_HMAC_SECRET",
    "VERIFY_TOKEN",
]

ENV_FILE = Path.home() / ".hermes" / ".env"

_yaml_config: dict[str, str] = {}


def apply_yaml_config(yaml_cfg, platform_cfg):
    """apply_yaml_config_fn contract: TWO args (yaml_cfg, platform_cfg).
    Installs YAML values as env fallbacks; platform_cfg wins over yaml_cfg."""
    _yaml_config.clear()
    for source in (yaml_cfg, platform_cfg):
        for key, value in (source or {}).items():
            if isinstance(value, (str, int, float, bool)):
                _yaml_config[str(key)] = str(value)


def get_setting(name, default=None):
    """Resolve a setting: env > YAML extra > built-in default > caller default."""
    value = os.environ.get(name)
    if not value:
        value = _yaml_config.get(name)
    if not value:
        value = DEFAULTS.get(name, default)
    return value


def _extra_get(config, name):
    """Platform config values live under PlatformConfig.extra (registry
    contract). Also accepts a plain dict (standalone/cron pconfig, tests) or
    None. All hooks and the adapter's _cfg route through here."""
    extra = getattr(config, "extra", None)
    if extra is None and isinstance(config, dict):
        extra = config
    return (extra or {}).get(name)


def _save_env_value(key, value, path=None):
    """Upsert KEY=value into ~/.hermes/.env (or an explicit path)."""
    target = Path(path) if path is not None else ENV_FILE
    target.parent.mkdir(parents=True, exist_ok=True)
    lines = target.read_text().splitlines() if target.exists() else []
    lines = [l for l in lines if not l.startswith(f"{key}=")]
    lines.append(f"{key}={value}")
    target.write_text("\n".join(lines) + "\n")


# --- security -------------------------------------------------------------

SIGNATURE_HEADER = "X-HookMyApp-Signature-256"
PROBE_HEADER = "X-HookMyApp-Probe"
PROBE_VALUE = "webhook-verification"


def verify_signature(raw_body, header, secret):
    """Constant-time HMAC-SHA256 check on raw bytes, before any JSON parse.

    Missing secret or missing header => reject (never fail-open). No runtime
    disable switch exists by design (spec D7).
    """
    if not secret or not header:
        return False
    expected = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    provided = header.removeprefix("sha256=")
    return hmac.compare_digest(expected, provided)


def is_probe(method, headers, body):
    """HookMyApp webhook-verification probe (spec D3): unsigned, handled
    before HMAC, never dispatched. GET, or POST with an empty body, carrying
    the probe header."""
    if headers.get(PROBE_HEADER) != PROBE_VALUE:
        return False
    method = method.upper()
    if method == "GET":
        return True
    return method == "POST" and not body


# --- payload -> events ----------------------------------------------------


def _digits(value):
    return re.sub(r"\D", "", value or "")


def is_sender_allowed(sender, allowed_csv, allow_all):
    """Default-closed allowlist with digits-only equivalence ('+972...' ==
    '972...'). Adapter-level, pre-handoff and pre-media — in ADDITION to the
    framework's allowed_users_env check (spec D8: do not assume framework
    normalization)."""
    if str(allow_all or "").strip().lower() in ("1", "true", "yes"):
        return True
    allowed = {_digits(entry) for entry in (allowed_csv or "").split(",")}
    allowed.discard("")
    sender_digits = _digits(sender)
    return bool(sender_digits) and sender_digits in allowed


def extract_messages(payload):
    """One item per messages[] entry across EVERY entry/change (batched
    envelopes are real). statuses[] are delivery receipts — skipped. Messages
    whose sender is the business number itself (echoes) are skipped."""
    items = []
    for entry in (payload or {}).get("entry") or []:
        for change in entry.get("changes") or []:
            value = change.get("value") or {}
            metadata = value.get("metadata") or {}
            pnid = metadata.get("phone_number_id") or ""
            own_digits = _digits(metadata.get("display_phone_number"))
            names = {}
            for contact in value.get("contacts") or []:
                names[contact.get("wa_id")] = (contact.get("profile") or {}).get("name")
            for message in value.get("messages") or []:
                sender = message.get("from") or ""
                if own_digits and _digits(sender) == own_digits:
                    continue  # echo of our own number
                items.append({
                    "phone_number_id": pnid,
                    "sender": sender,
                    "user_name": names.get(sender),
                    "message": message,
                })
    return items


class SeenIds:
    """Bounded LRU of seen wamids. Dedupe only — recovers nothing (spec D6:
    an event lost after our 200 is lost permanently)."""

    def __init__(self, maxsize=2048):
        self.maxsize = maxsize
        self._seen = OrderedDict()

    def __contains__(self, wamid):
        # Membership WITHOUT registering: the handler registers a wamid only
        # after that event's successful handoff (never ack a lost event).
        return wamid in self._seen

    def check_and_add(self, wamid):
        if wamid in self._seen:
            self._seen.move_to_end(wamid)
            return False
        self._seen[wamid] = True
        while len(self._seen) > self.maxsize:
            self._seen.popitem(last=False)
        return True


# --- chat-id grammar ------------------------------------------------------

CHAT_ID_PREFIX = "hookmyapp"


def _b64u(value):
    return base64.urlsafe_b64encode(value.encode()).decode().rstrip("=")


def _b64u_decode(value):
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)).decode()


def encode_chat_id(phone_number_id, wa_id):
    return f"{CHAT_ID_PREFIX}:{_b64u(phone_number_id)}:{_b64u(wa_id)}"


def decode_chat_id(chat_id):
    prefix, _, rest = chat_id.partition(":")
    if prefix != CHAT_ID_PREFIX or ":" not in rest:
        raise ValueError(f"not a hookmyapp chat id: {chat_id!r}")
    pnid_b64, _, wa_b64 = rest.partition(":")
    return _b64u_decode(pnid_b64), _b64u_decode(wa_b64)


def resolve_chat_id(chat_id, default_pnid):
    """Accepts 'hookmyapp:<b64>:<b64>', '<phone_number_id>:<recipient>', or a
    bare number (uses default_pnid = WHATSAPP_PHONE_NUMBER_ID)."""
    if chat_id.startswith(CHAT_ID_PREFIX + ":"):
        return decode_chat_id(chat_id)
    if ":" in chat_id:
        pnid, _, recipient = chat_id.partition(":")
        return pnid, recipient
    if not default_pnid:
        raise ValueError("bare recipient given but WHATSAPP_PHONE_NUMBER_ID is not set")
    return default_pnid, chat_id


# --- media ----------------------------------------------------------------

try:  # hermes cache helpers when running inside the gateway
    from gateway.platforms.base import (  # type: ignore
        cache_audio_from_bytes,
        cache_document_from_bytes,
        cache_image_from_bytes,
    )
except ImportError:  # local fallback so this module and its tests run standalone

    def _cache_to_tmp(data, ext):
        suffix = ext if ext.startswith(".") else f".{ext}"
        fd, path = tempfile.mkstemp(prefix="hookmyapp-media-", suffix=suffix)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        return path

    def cache_image_from_bytes(data, ext="jpg"):
        return _cache_to_tmp(data, ext)

    def cache_audio_from_bytes(data, ext="ogg"):
        return _cache_to_tmp(data, ext)

    def cache_document_from_bytes(data, ext="bin"):
        return _cache_to_tmp(data, ext)


MAX_MEDIA_BYTES = 16 * 1024 * 1024
MEDIA_TIMEOUT_S = 10


async def resolve_media(session, base_url, token, media_id):
    """media id -> gateway lookup -> signed short-lived URL -> immediate
    download. Returns (bytes, mime) or None on any failure (caller forwards
    the message text-only).

    Hardening: request timeout; no redirect following; the bearer token goes
    ONLY to the gateway metadata call (the signed URL is its own credential);
    Content-Length pre-check + streamed cap at MAX_MEDIA_BYTES."""
    timeout = aiohttp.ClientTimeout(total=MEDIA_TIMEOUT_S) if aiohttp else None
    try:
        async with session.get(f"{base_url.rstrip('/')}/{media_id}",
                               headers={"Authorization": f"Bearer {token}"},
                               allow_redirects=False, timeout=timeout) as resp:
            resp.raise_for_status()
            info = await resp.json(content_type=None)
        url = (info or {}).get("url")
        if not url:
            return None
        async with session.get(url, allow_redirects=False, timeout=timeout) as resp:
            resp.raise_for_status()
            length = resp.headers.get("Content-Length")
            if length and int(length) > MAX_MEDIA_BYTES:
                return None
            chunks, total = [], 0
            async for chunk in resp.content.iter_chunked(64 * 1024):
                total += len(chunk)
                if total > MAX_MEDIA_BYTES:  # Content-Length can lie or be absent
                    return None
                chunks.append(chunk)
        return b"".join(chunks), (info.get("mime_type") or "application/octet-stream")
    except Exception:
        return None


# --- outbound -------------------------------------------------------------

MAX_MESSAGE_LENGTH = 4096


def _is_table_separator_row(line):
    """Pipe-table separator row: only pipes/dashes/colons/spaces, e.g.
    '|---|---|'. These carry no content — dropped entirely."""
    stripped = line.strip()
    return bool(stripped) and "-" in stripped and re.fullmatch(r"[|\-:\s]+", stripped) is not None


def _flatten_table_row(line):
    """Table data row -> single flattened line: strip the outer pipes,
    trim each cell, join with an em-dash separator (WhatsApp has no table
    rendering)."""
    trimmed = line.strip()
    if trimmed.startswith("|"):
        trimmed = trimmed[1:]
    if trimmed.endswith("|"):
        trimmed = trimmed[:-1]
    return " — ".join(cell.strip() for cell in trimmed.split("|"))


def markdown_to_whatsapp(text):
    """Best-effort markdown -> WhatsApp: converts *bold*/_italic_/~strike~,
    headers to *bold*, [text](url) links to 'text (url)', and flattens
    pipe tables (separator rows dropped, data rows joined with ' — ')."""
    text = re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+)\)", r"\1 (\2)", text)
    # Strip stray single '*' markers inside a **bold** span before wrapping,
    # so nested emphasis (e.g. "**bold *italic* text**") doesn't misrender.
    text = re.sub(r"\*\*(.+?)\*\*", lambda m: f"*{m.group(1).replace('*', '')}*", text)
    text = re.sub(r"~~(.+?)~~", r"~\1~", text)
    text = re.sub(r"^#{1,6}\s+(.+)$", r"*\1*", text, flags=re.MULTILINE)
    lines = []
    for line in text.split("\n"):
        if _is_table_separator_row(line):
            continue
        if "|" in line:
            line = _flatten_table_row(line)
        lines.append(line)
    return "\n".join(lines)


def split_message(text, limit=MAX_MESSAGE_LENGTH):
    chunks = []
    text = text.lstrip("\n ")
    while len(text) > limit:
        window = text[: limit + 1]
        cut = window.rfind("\n")
        if cut <= 0:
            cut = window.rfind(" ")
        if cut <= 0:
            cut = limit
        chunks.append(text[:cut].rstrip("\n "))
        text = text[cut:].lstrip("\n ")
    if text:
        chunks.append(text)
    return chunks or [""]


async def send_whatsapp_text(session, base_url, token, phone_number_id, to, text):
    """POST chunks to {base_url}/{phone_number_id}/messages via the HookMyApp
    gateway. Retryable on 5xx/network; not on 4xx."""
    url = f"{base_url.rstrip('/')}/{phone_number_id}/messages"
    headers = {"Authorization": f"Bearer {token}"}
    message_ids = []
    for chunk in split_message(markdown_to_whatsapp(text)):
        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": to,
            "type": "text",
            "text": {"body": chunk},
        }
        try:
            async with session.post(url, json=payload, headers=headers) as resp:
                body = await resp.json(content_type=None)
                if resp.status >= 400:
                    error = ((body or {}).get("error") or {}).get("message") or f"HTTP {resp.status}"
                    # Retry policy: only when NOTHING was delivered yet. Once
                    # >=1 chunk has succeeded, a later failure is terminal —
                    # honoring retryable here would re-send already-delivered
                    # chunks and duplicate them for the user. message_ids
                    # tells the caller what got through.
                    return {
                        "success": False,
                        "error": error,
                        "retryable": resp.status >= 500 and not message_ids,
                        "message_ids": message_ids,
                    }
                message_ids.append(body["messages"][0]["id"])
        except Exception as exc:  # network / protocol errors
            return {
                "success": False,
                "error": str(exc),
                "retryable": not message_ids,  # see retry policy note above
                "message_ids": message_ids,
            }
    return {"success": True, "message_ids": message_ids}


# --- hermes contract (defensive import; shims let tests run standalone) ---

try:
    from gateway.platforms.base import (  # type: ignore
        BasePlatformAdapter,
        MessageEvent,
        MessageType,
        Platform,
        SendResult,
    )
    HERMES_AVAILABLE = True
except ImportError:
    HERMES_AVAILABLE = False
    import enum
    from dataclasses import dataclass, field

    class Platform(str):
        """Stand-in: hermes' Platform enum accepts plugin names via _missing_."""

    class MessageType(str, enum.Enum):
        TEXT = "text"
        PHOTO = "photo"
        VOICE = "voice"
        DOCUMENT = "document"

    @dataclass
    class MessageEvent:
        text: str
        message_type: "MessageType"
        source: dict
        message_id: str | None = None
        media_urls: list = field(default_factory=list)
        media_types: list = field(default_factory=list)
        metadata: dict = field(default_factory=dict)

    @dataclass
    class SendResult:
        success: bool
        message_id: str | None = None
        error: str | None = None
        retryable: bool = False
        retry_after: float | None = None
        continuation_message_ids: list = field(default_factory=list)

    class BasePlatformAdapter:
        def __init__(self, config, platform):
            self.config = config or {}
            self.platform = platform
            self.is_connected = False  # matches the real base attribute name
            self.fatal_error = None

        def _mark_connected(self):
            self.is_connected = True

        def _mark_disconnected(self):
            self.is_connected = False

        def _set_fatal_error(self, code, message, *, retryable=False):
            # retryable is keyword-only, matching the real base signature
            self.fatal_error = {"code": code, "message": message, "retryable": retryable}

        def build_source(self, chat_id, chat_type="dm", user_id=None, user_name=None, message_id=None):
            return {
                "chat_id": chat_id,
                "chat_type": chat_type,
                "user_id": user_id,
                "user_name": user_name,
                "message_id": message_id,
            }

        async def handle_message(self, event):  # overridden by tests / real base
            pass


# --- adapter --------------------------------------------------------------

MEDIA_MESSAGE_TYPES = ("image", "video", "sticker", "audio", "voice", "document")


class HookMyAppAdapter(BasePlatformAdapter):
    MAX_MESSAGE_LENGTH = MAX_MESSAGE_LENGTH
    MAX_BODY_BYTES = 2 * 1024 * 1024
    MEDIA_BUDGET_S = 5.0  # hard aggregate media-resolution budget per envelope

    def __init__(self, config):
        super().__init__(config, Platform("hookmyapp"))
        self._runner = None
        self._session = None
        self._seen = SeenIds()
        # In-flight reservation set: wamids handed to _process but not yet
        # promoted to _seen. Guards the await window (media can hold it open
        # for seconds) against a concurrent duplicate delivery double-
        # dispatching the same message. Plain set is race-safe here: the
        # event loop only switches at awaits, and check+add happen between
        # awaits.
        self._inflight = set()

    def _cfg(self, name, default=None):
        # PlatformConfig keeps platform values under .extra; _extra_get also
        # accepts a plain dict (standalone/cron pconfig). Env falls back.
        return _extra_get(self.config, name) or get_setting(name, default)

    # -- lifecycle --

    def _build_app(self):
        app = web.Application(client_max_size=self.MAX_BODY_BYTES + 1024)
        path = self._cfg("HOOKMYAPP_WEBHOOK_PATH")
        app.router.add_route("GET", path, self._handle_webhook)
        app.router.add_route("POST", path, self._handle_webhook)
        app.router.add_get("/health", self._handle_health)
        return app

    async def connect(self, *, is_reconnect=False):
        # _set_fatal_error's retryable is KEYWORD-ONLY in the base contract.
        if aiohttp is None:
            self._set_fatal_error("missing_dependency",
                                  "aiohttp is not installed — pip install aiohttp",
                                  retryable=False)
            return False
        if not self._cfg("WEBHOOK_HMAC_SECRET"):
            self._set_fatal_error("missing_secret",
                                  "WEBHOOK_HMAC_SECRET is required; inbound webhooks are "
                                  "rejected without it (no fail-open, no disable switch)",
                                  retryable=False)
            return False
        self._inflight.clear()  # defensive: stale reservations don't survive reconnect
        self._session = aiohttp.ClientSession()
        self._runner = web.AppRunner(self._build_app())
        await self._runner.setup()
        site = web.TCPSite(self._runner,
                           self._cfg("HOOKMYAPP_HOST"),
                           int(self._cfg("HOOKMYAPP_PORT")))
        try:
            await site.start()
        except OSError as exc:
            self._set_fatal_error("bind_failed", f"cannot bind listener: {exc}",
                                  retryable=True)
            await self.disconnect()
            return False
        self._mark_connected()
        return True

    async def disconnect(self):
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
        if self._session is not None:
            await self._session.close()
            self._session = None
        self._mark_disconnected()

    # -- inbound --

    async def _handle_health(self, request):
        return web.json_response({"status": "ok", "platform": "hookmyapp",
                                  "connected": self.is_connected})

    async def _handle_webhook(self, request):
        body = await request.read()
        if len(body) > self.MAX_BODY_BYTES:
            return web.Response(status=413)
        # Probes are unsigned by contract: answered BEFORE HMAC, never dispatched.
        if is_probe(request.method, request.headers, body):
            if request.method == "GET":
                return web.Response(status=200, text=self._cfg("VERIFY_TOKEN") or "")
            return web.Response(status=200)
        if request.method != "POST":
            return web.Response(status=405)
        if not verify_signature(body, request.headers.get(SIGNATURE_HEADER),
                                self._cfg("WEBHOOK_HMAC_SECRET")):
            return web.Response(status=401)
        try:
            payload = json.loads(body)
        except ValueError:
            return web.Response(status=400)
        deadline = asyncio.get_running_loop().time() + self.MEDIA_BUDGET_S
        handoff_failed = False
        for item in extract_messages(payload):
            wamid = item["message"].get("id")
            if wamid and wamid in self._seen:
                continue  # handed off on an earlier delivery of this envelope
            if wamid and wamid in self._inflight:
                # A concurrent delivery of the same wamid is mid-handoff
                # (media resolution can hold that window open for seconds).
                # Neither has succeeded yet, so 500 this delivery: if the
                # in-flight one succeeds, the retry dedupes on _seen; if it
                # fails, the retry re-processes.
                handoff_failed = True
                continue
            # Adapter-level default-closed allowlist, BEFORE any media
            # download: strangers never trigger network fetches or dispatch.
            if not is_sender_allowed(item["sender"],
                                     self._cfg("HOOKMYAPP_ALLOWED_USERS"),
                                     self._cfg("HOOKMYAPP_ALLOW_ALL_USERS")):
                _log.info("dropped message from non-allowlisted sender %s",
                          _digits(item["sender"]))
                continue
            if wamid:
                self._inflight.add(wamid)
            # D6 handoff point: handle_message CALLED (it schedules the agent
            # turn itself), so awaiting _process stays well inside the
            # forwarder's 10s budget.
            try:
                await self._process(item, deadline)
            except asyncio.CancelledError:
                # Cancellation (task cancel/shutdown) is a BaseException, not
                # an Exception — it must still release the reservation or
                # this wamid 500s forever (connect() doesn't reset the set).
                if wamid:
                    self._inflight.discard(wamid)
                raise
            except Exception:
                # NOT handed off — never ack a lost event. The reservation is
                # released, no dedupe entry is written, and the delivery gets
                # a 500 below, so the forwarder/Meta redeliver. Events already
                # handed off in this batch are shielded by their dedupe
                # entries.
                _log.exception("event handoff failed for wamid=%s", wamid)
                handoff_failed = True
                if wamid:
                    self._inflight.discard(wamid)
                continue
            if wamid:
                # Promote to the durable dedupe BEFORE releasing the
                # reservation — no gap where a concurrent duplicate sees
                # neither set.
                self._seen.check_and_add(wamid)
                self._inflight.discard(wamid)
        if handoff_failed:
            return web.Response(status=500)
        # After this 200 the forwarder relays 200 to Meta — nothing retries.
        # The remaining permanent-loss window is only between this ack and
        # the background agent turn handle_message scheduled (v1 accepted,
        # documented).
        return web.Response(status=200)

    async def _process(self, item, deadline):
        message = item["message"]
        mtype = message.get("type", "text")
        text = ""
        media_urls, media_types = [], []
        message_type = MessageType.TEXT
        if mtype == "text":
            text = (message.get("text") or {}).get("body", "")
        elif mtype in MEDIA_MESSAGE_TYPES:
            media = message.get(mtype) or {}
            text = media.get("caption", "") or ""
            resolved = None
            attempted = False  # only warn below if resolution was actually tried
            if self._session is not None and media.get("id"):
                # Hard aggregate budget across the whole envelope: whatever is
                # left of MEDIA_BUDGET_S. Timeout ⇒ degrade to text-only.
                budget = deadline - asyncio.get_running_loop().time()
                if budget > 0:
                    attempted = True
                    try:
                        resolved = await asyncio.wait_for(
                            resolve_media(self._session,
                                          self._cfg("META_GRAPH_API_URL"),
                                          self._cfg("WHATSAPP_ACCESS_TOKEN"),
                                          media["id"]),
                            timeout=budget)
                    except asyncio.TimeoutError:
                        resolved = None
            if resolved:
                data, mime = resolved
                ext = (mime.split("/")[-1] or "bin").split(";")[0]
                if mtype in ("image", "sticker"):
                    message_type = MessageType.PHOTO
                    media_urls = [cache_image_from_bytes(data, ext)]
                elif mtype in ("audio", "voice"):
                    message_type = MessageType.VOICE
                    media_urls = [cache_audio_from_bytes(data, "ogg")]
                else:
                    message_type = MessageType.DOCUMENT
                    media_urls = [cache_document_from_bytes(data, ext)]
                media_types = [mime]
            else:
                # resolve_media returns None silently by design (spec); the
                # caller (here) is the one that must log the degradation —
                # but only if resolution was actually attempted (an id
                # existed and we tried), not merely because there was no id.
                if attempted:
                    _log.warning("media resolution failed for wamid=%s type=%s — "
                                "degrading to text-only", message.get("id"), mtype)
                text = text or f"[{mtype} message — media unavailable]"
        else:
            text = f"[unsupported {mtype} message]"
        chat_id = encode_chat_id(item["phone_number_id"], item["sender"])
        source = self.build_source(chat_id, chat_type="dm", user_id=item["sender"],
                                   user_name=item.get("user_name"),
                                   message_id=message.get("id"))
        event = MessageEvent(text=text, message_type=message_type, source=source,
                             message_id=message.get("id"),
                             media_urls=media_urls, media_types=media_types,
                             metadata={"wamid": message.get("id"),
                                       "phone_number_id": item["phone_number_id"]})
        await self.handle_message(event)

    # -- outbound --

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        pnid, to = resolve_chat_id(chat_id, self._cfg("WHATSAPP_PHONE_NUMBER_ID"))
        result = await send_whatsapp_text(
            self._session, self._cfg("META_GRAPH_API_URL"),
            self._cfg("WHATSAPP_ACCESS_TOKEN"), pnid, to, content)
        ids = result.get("message_ids") or []
        if result["success"]:
            return SendResult(success=True, message_id=ids[-1] if ids else None,
                              continuation_message_ids=ids[:-1])
        return SendResult(success=False, error=result.get("error"),
                          retryable=bool(result.get("retryable")))

    async def get_chat_info(self, chat_id):
        _, wa_id = resolve_chat_id(chat_id, self._cfg("WHATSAPP_PHONE_NUMBER_ID"))
        return {"name": wa_id, "type": "dm"}

    async def send_typing(self, chat_id, metadata=None):
        """Best-effort typing indicator; needs the inbound wamid. Never raises."""
        wamid = (metadata or {}).get("wamid")
        if not wamid or self._session is None:
            return
        pnid, _ = resolve_chat_id(chat_id, self._cfg("WHATSAPP_PHONE_NUMBER_ID"))
        url = f"{self._cfg('META_GRAPH_API_URL').rstrip('/')}/{pnid}/messages"
        payload = {"messaging_product": "whatsapp", "status": "read",
                   "message_id": wamid, "typing_indicator": {"type": "text"}}
        try:
            async with self._session.post(
                    url, json=payload,
                    headers={"Authorization": f"Bearer {self._cfg('WHATSAPP_ACCESS_TOKEN')}"}) as resp:
                await resp.read()
        except Exception:
            pass


# --- registration hooks ---------------------------------------------------

PLATFORM_HINT = (
    "WhatsApp via HookMyApp. Formatting: *bold*, _italic_, ~strikethrough~, "
    "``` monospace ```; NO markdown headers, tables, or [label](url) links — "
    "write the URL in plain text. Keep messages under 4096 chars (longer "
    "output is split automatically). Business-initiated messages are only "
    "deliverable inside the 24h customer service window after the user's "
    "last message; outside it, sends may fail until the user writes again."
)


def _missing_required(lookup):
    return [name for name in REQUIRED_ENV if not lookup(name)]


def check_fn():
    """Registry contract: PLAIN bool — a (False, msg) tuple is truthy and
    would enable a broken platform. Reasons are logged, never returned."""
    if aiohttp is None:
        _log.warning("hookmyapp disabled: aiohttp is not installed — pip install aiohttp")
        return False
    missing = _missing_required(get_setting)
    if missing:
        _log.warning("hookmyapp disabled: missing env %s "
                     "(run `hermes hookmyapp setup` or `hookmyapp channels env <channel>`)",
                     ", ".join(missing))
        return False
    return True


def validate_config(config):
    """Plain bool per registry contract. Receives a PlatformConfig — values
    live under .extra (read via _extra_get, env fallback). Reasons logged."""
    lookup = lambda name: _extra_get(config, name) or get_setting(name)
    missing = _missing_required(lookup)
    if missing:
        _log.warning("hookmyapp config invalid: missing %s", ", ".join(missing))
        return False
    return True


def is_connected(config):
    """Receives a PlatformConfig — and runs during env auto-enablement BEFORE
    any adapter exists, so adapter/socket liveness would return False and
    block startup. Config-presence semantics (same basis as check_fn),
    mirroring the built-in platforms."""
    lookup = lambda name: _extra_get(config, name) or get_setting(name)
    return not _missing_required(lookup)


def env_enablement_fn():
    """Registry contract: Optional[dict], NOT bool. The registry merges the
    returned keys into PlatformConfig.extra ITSELF — return the FLAT mapping
    (a nested {"extra": ...} would land as extra["extra"] and hide the
    credentials). None => not enableable from env. Required in practice:
    env-only setups are otherwise invisible and cron delivery breaks.

    HOOKMYAPP_HOME_CHANNEL is emitted TWICE, deliberately: once flat (so
    cron_deliver_env_var / any code reading extra["HOOKMYAPP_HOME_CHANNEL"]
    sees it) and once nested under the top-level "home_channel" key. The
    registry's _apply_env_overrides pops "home_channel" out specially and
    builds a real PlatformConfig.home_channel from it — a plain flat entry
    would NOT do that (Task 1 verified contract fact #3)."""
    if _missing_required(get_setting):
        return None
    enablement = {name: get_setting(name) for name in REQUIRED_ENV}
    home = get_setting("HOOKMYAPP_HOME_CHANNEL")
    if home:
        enablement["HOOKMYAPP_HOME_CHANNEL"] = home
        enablement["home_channel"] = {"chat_id": home}
    return enablement


async def standalone_sender(pconfig, chat_id, message, *, thread_id=None,
                            media_files=None, force_document=False):
    """Out-of-process cron delivery (no running gateway/adapter)."""
    if aiohttp is None:
        return {"error": "aiohttp is not installed — pip install aiohttp"}
    lookup = lambda name: _extra_get(pconfig, name) or get_setting(name)
    missing = _missing_required(lookup)
    if missing:
        return {"error": "missing config: " + ", ".join(missing)}
    try:
        pnid, to = resolve_chat_id(chat_id, lookup("WHATSAPP_PHONE_NUMBER_ID"))
    except ValueError as exc:
        return {"error": str(exc)}
    async with aiohttp.ClientSession() as session:
        result = await send_whatsapp_text(session, lookup("META_GRAPH_API_URL"),
                                          lookup("WHATSAPP_ACCESS_TOKEN"), pnid, to, message)
    if result["success"]:
        ids = result["message_ids"]
        return {"success": True, "message_id": ids[-1] if ids else None}
    return {"error": result.get("error") or "send failed"}


# --- CLI: hermes hookmyapp setup|status -----------------------------------


def _run_hookmyapp(*args):
    """Run the @gethookmyapp/cli binary. Never raises; (rc, stdout, stderr)."""
    try:
        proc = subprocess.run(["hookmyapp", *args], capture_output=True,
                              text=True, timeout=60)
        return proc.returncode, proc.stdout, proc.stderr
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, "", str(exc)


def _print_manual_instructions():
    print("Manual setup (fail-open path):")
    print("  1. npm install -g @gethookmyapp/cli && hookmyapp login")
    print("  2. hookmyapp channels list")
    print("  3. hookmyapp channels env <channel> --write ~/.hermes/.env")
    print("  4. hookmyapp channels listen <channel> --port 8649 --path /hookmyapp/webhook")
    print("  5. hermes gateway restart")


def _pick_channel(explicit):
    if explicit:
        return explicit
    rc, out, err = _run_hookmyapp("channels", "list", "--json")
    if rc != 0:
        print(f"channels list failed: {err or out}".strip())
        return None
    try:
        channels = json.loads(out)
    except ValueError:
        print("channels list returned unparseable output")
        return None
    if not channels:
        print("No channels found — create one at https://app.hookmyapp.com first.")
        return None
    if len(channels) == 1:
        return channels[0]["id"]
    print("Channels:")
    for index, channel in enumerate(channels, 1):
        print(f"  {index}. {channel.get('name', '?')} ({channel['id']})")
    choice = input("Pick a channel number: ").strip()
    try:
        return channels[int(choice) - 1]["id"]
    except (ValueError, IndexError):
        print("Invalid choice.")
        return None


def cmd_setup(args=None):
    channel = getattr(args, "channel", None)
    webhook_url = getattr(args, "webhook_url", None)
    port = getattr(args, "port", None) or get_setting("HOOKMYAPP_PORT")
    path = get_setting("HOOKMYAPP_WEBHOOK_PATH")

    if shutil.which("hookmyapp") is None:
        if getattr(args, "install_cli", False):
            print("Installing @gethookmyapp/cli ...")
            rc = subprocess.run(["npm", "install", "-g", "@gethookmyapp/cli"]).returncode
            if rc != 0 or shutil.which("hookmyapp") is None:
                print("Install failed.")
                _print_manual_instructions()
                return
        else:
            print("The `hookmyapp` CLI is not installed "
                  "(npm install -g @gethookmyapp/cli, or rerun with --install-cli).")
            _print_manual_instructions()
            return

    channel = _pick_channel(channel)
    if channel is None:
        _print_manual_instructions()
        return

    rc, out, err = _run_hookmyapp("channels", "env", channel, "--json")
    if rc != 0:
        print(f"channels env failed: {err or out}".strip())
        _print_manual_instructions()
        return
    try:
        env_values = json.loads(out)
    except ValueError:
        print("channels env returned unparseable output")
        _print_manual_instructions()
        return
    for key, value in env_values.items():
        _save_env_value(key, value, path=ENV_FILE)
        print(f"  wrote {key}=***set***")
    _save_env_value("HOOKMYAPP_PORT", str(port), path=ENV_FILE)

    if webhook_url:
        rc, out, err = _run_hookmyapp("channels", "webhook", "set", channel, "--url", webhook_url)
        if rc == 0:
            print(f"Webhook registered: {webhook_url}")
        else:
            print(f"webhook set failed: {err or out}".strip())
            print(f"Run manually: hookmyapp channels webhook set {channel} --url {webhook_url}")
    else:
        print("\nStart the tunnel transport (keep it running):")
        print(f"  hookmyapp channels listen {channel} --port {port} --path {path}")

    print("\nThen restart the gateway: hermes gateway restart")


def cmd_status(args=None):
    print(f"aiohttp installed: {'yes' if aiohttp is not None else 'NO — pip install aiohttp'}")
    print(f"listener: {get_setting('HOOKMYAPP_HOST')}:{get_setting('HOOKMYAPP_PORT')}"
          f"{get_setting('HOOKMYAPP_WEBHOOK_PATH')}")
    for name in REQUIRED_ENV + ["HOOKMYAPP_CHANNEL_ID", "HOOKMYAPP_ALLOWED_USERS",
                                "HOOKMYAPP_ALLOW_ALL_USERS", "HOOKMYAPP_HOME_CHANNEL"]:
        print(f"  {name}: {'***set***' if get_setting(name) else 'missing'}")
    # check_fn returns a plain bool (registry contract); missing pieces are
    # already visible in the per-variable lines above.
    print(f"check: {'ok' if check_fn() else 'failing (missing items above)'}")


def register_cli_subcommands(parser):
    subparsers = parser.add_subparsers(dest="subcommand", required=True)
    setup = subparsers.add_parser("setup", help="Connect a HookMyApp WhatsApp channel")
    setup.add_argument("--channel", help="Channel id (ch_...); auto-picked when you have one")
    setup.add_argument("--webhook-url", help="Public HTTPS URL — registers it instead of tunnel mode")
    setup.add_argument("--port", help="Adapter listener port (default 8649)")
    setup.add_argument("--install-cli", action="store_true",
                       help="Install @gethookmyapp/cli via npm if missing")
    setup.set_defaults(func=cmd_setup)
    status = subparsers.add_parser("status", help="Show adapter configuration status")
    status.set_defaults(func=cmd_status)


def register(ctx):
    ctx.register_platform(
        name="hookmyapp",
        label="HookMyApp WhatsApp",
        adapter_factory=lambda cfg: HookMyAppAdapter(cfg),
        check_fn=check_fn,
        validate_config=validate_config,
        is_connected=is_connected,
        required_env=list(REQUIRED_ENV),
        install_hint="pip install aiohttp",
        setup_fn=cmd_setup,
        env_enablement_fn=env_enablement_fn,
        apply_yaml_config_fn=apply_yaml_config,
        cron_deliver_env_var="HOOKMYAPP_HOME_CHANNEL",
        standalone_sender_fn=standalone_sender,
        allowed_users_env="HOOKMYAPP_ALLOWED_USERS",
        allow_all_env="HOOKMYAPP_ALLOW_ALL_USERS",
        max_message_length=MAX_MESSAGE_LENGTH,
        emoji="📱",
        pii_safe=False,
        platform_hint=PLATFORM_HINT,
    )
    ctx.register_cli_command(
        name="hookmyapp",
        help="HookMyApp WhatsApp channel setup and status",
        description="Connect a HookMyApp WhatsApp channel to Hermes",
        setup_fn=register_cli_subcommands,
    )
