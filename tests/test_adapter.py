import asyncio
import json
import logging

import pytest
from aiohttp.test_utils import TestClient, TestServer

import adapter
from tests.test_media import FakeResponse, FakeSession
from tests.test_outbound import FakePostSession
from tests.test_payload import text_msg, wa_payload
from tests.test_signature import SECRET, sign

CONFIG = {
    "META_GRAPH_API_URL": "https://gw.example/graph",
    "WHATSAPP_ACCESS_TOKEN": "hmat_test",
    "WHATSAPP_PHONE_NUMBER_ID": "10001",
    "WEBHOOK_HMAC_SECRET": SECRET,
    "VERIFY_TOKEN": "vt_test",
    "HOOKMYAPP_HOST": "127.0.0.1",
    "HOOKMYAPP_PORT": "0",
    # E.164 form on purpose: senders arrive digits-only, so admission of the
    # test sender doubles as the digits/E.164 equivalence check (spec D8).
    "HOOKMYAPP_ALLOWED_USERS": "+972545000000",
}


class FakePlatformConfig:
    """Real-shaped PlatformConfig stand-in: platform values live under .extra."""

    def __init__(self, extra):
        self.extra = dict(extra)


@pytest.fixture
async def client():
    a = adapter.HookMyAppAdapter(FakePlatformConfig(CONFIG))
    a.received = []

    async def capture(event):
        a.received.append(event)

    a.handle_message = capture
    app = a._build_app()
    test_client = TestClient(TestServer(app))
    await test_client.start_server()
    yield test_client, a
    await test_client.close()


async def post_signed(client, payload, secret=SECRET):
    body = json.dumps(payload).encode()
    return await client.post("/hookmyapp/webhook", data=body,
                             headers={"X-HookMyApp-Signature-256": sign(body, secret)})


class FakeRequest:
    """Minimal stand-in for aiohttp's Request — _handle_webhook only touches
    .method, .headers, and .read(). Lets a test drive _handle_webhook as a
    plain task (so it can be cancelled) without the TestServer/TestClient
    transport getting in the way."""

    def __init__(self, method, headers, body):
        self.method = method
        self.headers = headers
        self._body = body

    async def read(self):
        return self._body


def image_msg(wamid="wamid.img", sender="972545000000", media_id="media1"):
    return {"id": wamid, "from": sender, "type": "image", "image": {"id": media_id}}


def audio_msg(wamid="wamid.audio", sender="972545000000", media_id="media1", voice=False):
    return {"id": wamid, "from": sender, "type": "audio",
            "audio": {"id": media_id, "voice": voice}}


async def test_probe_get_echoes_verify_token(client):
    c, _ = client
    resp = await c.get("/hookmyapp/webhook", headers={"X-HookMyApp-Probe": "webhook-verification"})
    assert resp.status == 200
    assert await resp.text() == "vt_test"


async def test_probe_empty_post_ok_and_not_dispatched(client):
    c, a = client
    resp = await c.post("/hookmyapp/webhook", data=b"",
                        headers={"X-HookMyApp-Probe": "webhook-verification"})
    assert resp.status == 200
    assert a.received == []


async def test_signed_text_message_dispatched_before_200(client):
    c, a = client
    # Sender "972545000000" is allowlisted as "+972545000000" — equivalence.
    resp = await post_signed(c, wa_payload([text_msg()]))
    assert resp.status == 200
    # Handoff happens BEFORE the 200 (D6): no sleep needed.
    assert len(a.received) == 1
    event = a.received[0]
    assert event.text == "hi"
    assert event.source["chat_id"] == adapter.encode_chat_id("10001", "972545000000")


async def test_bad_signature_rejected_401(client):
    c, a = client
    resp = await post_signed(c, wa_payload([text_msg()]), secret="wrong")
    assert resp.status == 401
    assert a.received == []


async def test_missing_signature_rejected_401(client):
    c, _ = client
    resp = await c.post("/hookmyapp/webhook", data=b"{}")
    assert resp.status == 401


async def test_duplicate_wamid_dispatched_once(client):
    c, a = client
    await post_signed(c, wa_payload([text_msg("wamid.dup")]))
    await post_signed(c, wa_payload([text_msg("wamid.dup")]))
    assert len(a.received) == 1


async def test_non_allowlisted_sender_no_dispatch_no_media_download(client, monkeypatch):
    c, a = client

    async def boom(*args, **kwargs):
        raise AssertionError("media resolution must never run for non-allowlisted senders")

    monkeypatch.setattr(adapter, "resolve_media", boom)
    stranger_img = {"id": "wamid.img", "from": "15559990000", "type": "image",
                    "image": {"id": "media1"}}
    resp = await post_signed(c, wa_payload([stranger_img]))
    assert resp.status == 200  # still ack — we just drop the stranger's event
    assert a.received == []


async def test_failed_handoff_returns_500_and_retry_reprocesses_only_failed(client):
    c, a = client
    calls = []

    async def flaky(event):
        calls.append(event.message_id)
        if event.message_id == "wamid.bad" and calls.count("wamid.bad") == 1:
            raise RuntimeError("boom")
        a.received.append(event)

    a.handle_message = flaky
    payload = wa_payload([text_msg("wamid.good"), text_msg("wamid.bad")])
    resp = await post_signed(c, payload)
    # Second event never handed off — never ack a lost event: 500 forces a
    # redelivery instead of losing it beyond the approved crash window.
    assert resp.status == 500
    assert [e.message_id for e in a.received] == ["wamid.good"]
    # Redelivery of the same envelope: the first event is shielded by its
    # dedupe entry (registered only after successful handoff); only the
    # failed event is re-processed.
    resp = await post_signed(c, payload)
    assert resp.status == 200
    assert [e.message_id for e in a.received] == ["wamid.good", "wamid.bad"]
    assert calls == ["wamid.good", "wamid.bad", "wamid.bad"]


async def test_concurrent_duplicate_delivery_dispatches_once(client):
    c, a = client
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow(event):
        started.set()
        await release.wait()   # hold the handoff window open (media-like)
        a.received.append(event)

    a.handle_message = slow
    payload = wa_payload([text_msg("wamid.race")])
    # First delivery enters _process and parks; wamid.race is now in-flight.
    first = asyncio.create_task(post_signed(c, payload))
    await started.wait()
    # Concurrent duplicate delivery: sees the in-flight reservation, must NOT
    # dispatch a second agent turn — 500 so the sender retries after the
    # winner settles.
    dup = await post_signed(c, payload)
    assert dup.status == 500
    release.set()
    resp = await first
    assert resp.status == 200
    assert [e.message_id for e in a.received] == ["wamid.race"]
    # Retry after the winner: deduped on _seen, no second dispatch.
    retry = await post_signed(c, payload)
    assert retry.status == 200
    assert [e.message_id for e in a.received] == ["wamid.race"]


async def test_oversized_body_413(client):
    c, _ = client
    body = b"x" * (adapter.HookMyAppAdapter.MAX_BODY_BYTES + 1)
    resp = await c.post("/hookmyapp/webhook", data=body,
                        headers={"X-HookMyApp-Signature-256": sign(body)})
    assert resp.status == 413


async def test_bad_json_400(client):
    c, _ = client
    body = b"not-json"
    resp = await c.post("/hookmyapp/webhook", data=body,
                        headers={"X-HookMyApp-Signature-256": sign(body)})
    assert resp.status == 400


async def test_non_probe_get_405(client):
    c, _ = client
    resp = await c.get("/hookmyapp/webhook")
    assert resp.status == 405


async def test_health(client):
    c, _ = client
    resp = await c.get("/health")
    assert resp.status == 200
    assert (await resp.json())["status"] == "ok"


async def test_connect_refuses_without_hmac_secret(monkeypatch):
    monkeypatch.delenv("WEBHOOK_HMAC_SECRET", raising=False)
    cfg = dict(CONFIG)
    cfg["WEBHOOK_HMAC_SECRET"] = ""
    a = adapter.HookMyAppAdapter(FakePlatformConfig(cfg))
    assert await a.connect() is False
    assert a.fatal_error["code"] == "missing_secret"


async def test_connect_disconnect_real_listener():
    a = adapter.HookMyAppAdapter(FakePlatformConfig(CONFIG))
    assert await a.connect() is True
    assert a.is_connected is True  # base attribute is .is_connected, not .connected
    await a.disconnect()
    assert a.is_connected is False


async def test_get_chat_info():
    a = adapter.HookMyAppAdapter(FakePlatformConfig(CONFIG))
    info = await a.get_chat_info(adapter.encode_chat_id("10001", "97254"))
    assert info == {"name": "97254", "type": "dm"}


# -- IMPORTANT-1: cancellation must release the in-flight reservation ------


async def test_cancellation_releases_inflight_reservation(client):
    c, a = client
    parked = asyncio.Event()

    async def parking(event):
        parked.set()
        await asyncio.Event().wait()  # never resolves on its own — must be cancelled

    a.handle_message = parking
    payload = wa_payload([text_msg("wamid.cancel")])
    body = json.dumps(payload).encode()
    request = FakeRequest("POST", {"X-HookMyApp-Signature-256": sign(body)}, body)
    task = asyncio.create_task(a._handle_webhook(request))
    await parked.wait()
    assert "wamid.cancel" in a._inflight
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert "wamid.cancel" not in a._inflight


async def test_connect_clears_stale_inflight_reservations():
    a = adapter.HookMyAppAdapter(FakePlatformConfig(CONFIG))
    a._inflight.add("stale.wamid")
    assert await a.connect() is True
    assert a._inflight == set()
    await a.disconnect()


# -- IMPORTANT-2: media path through the adapter, with a fake session ------


async def test_allowlisted_image_message_resolves_media_and_dispatches_photo(client):
    c, a = client
    a._session = FakeSession([
        FakeResponse(json_data={"url": "https://signed.example/x", "mime_type": "image/jpeg"}),
        FakeResponse(body=b"JPEGDATA"),
    ])
    resp = await post_signed(c, wa_payload([image_msg()]))
    assert resp.status == 200
    event = a.received[0]
    assert event.message_type == adapter.MessageType.PHOTO
    assert event.media_urls
    assert event.media_types == ["image/jpeg"]
    # Cache helpers expect a dotted extension (real Hermes helpers
    # concatenate it directly as a filename suffix) — a bare mime subtype
    # like "jpeg" would produce an unrecognizable "img_<id>jpeg" filename.
    assert event.media_urls[0].endswith(".jpeg")


async def test_regular_audio_message_dispatches_audio_not_voice(client):
    """WhatsApp sends type=audio for BOTH voice notes and plain audio file
    attachments; only the audio object's `voice` flag distinguishes them.
    Hermes treats VOICE as auto-transcribed STT input and AUDIO as a plain
    attachment, so a real audio file must not come through as VOICE."""
    c, a = client
    a._session = FakeSession([
        FakeResponse(json_data={"url": "https://signed.example/x", "mime_type": "audio/mpeg"}),
        FakeResponse(body=b"MP3DATA"),
    ])
    resp = await post_signed(c, wa_payload([audio_msg(voice=False)]))
    assert resp.status == 200
    event = a.received[0]
    assert event.message_type == adapter.MessageType.AUDIO


async def test_voice_note_message_dispatches_voice(client):
    c, a = client
    a._session = FakeSession([
        FakeResponse(json_data={"url": "https://signed.example/x", "mime_type": "audio/ogg"}),
        FakeResponse(body=b"OGGDATA"),
    ])
    resp = await post_signed(c, wa_payload([audio_msg(voice=True)]))
    assert resp.status == 200
    event = a.received[0]
    assert event.message_type == adapter.MessageType.VOICE


async def test_media_resolution_failure_warns_and_dispatches_text_only(client, caplog):
    c, a = client
    a._session = FakeSession([FakeResponse(status=404, json_data={})])
    with caplog.at_level(logging.WARNING, logger="hookmyapp"):
        resp = await post_signed(c, wa_payload([image_msg()]))
    assert resp.status == 200
    assert "media resolution failed" in caplog.text
    event = a.received[0]
    assert event.message_type == adapter.MessageType.TEXT
    assert event.media_urls == []


async def test_media_resolution_over_budget_degrades_to_text_only(client, monkeypatch):
    c, a = client
    monkeypatch.setattr(a, "MEDIA_BUDGET_S", 0.01)

    class SlowCtx:
        async def __aenter__(self):
            await asyncio.sleep(1)
            raise AssertionError("should have timed out before this")

        async def __aexit__(self, *exc):
            return False

    class SlowSession:
        def get(self, url, **kwargs):
            return SlowCtx()

    a._session = SlowSession()
    resp = await post_signed(c, wa_payload([image_msg()]))
    assert resp.status == 200
    event = a.received[0]
    assert event.message_type == adapter.MessageType.TEXT
    assert event.media_urls == []


async def test_no_media_id_does_not_warn(client, caplog):
    c, a = client
    a._session = None  # no session — resolution never attempted
    with caplog.at_level(logging.WARNING, logger="hookmyapp"):
        resp = await post_signed(c, wa_payload([image_msg()]))
    assert resp.status == 200
    assert "media resolution failed" not in caplog.text
    assert a.received[0].media_urls == []


# -- IMPORTANT-3: send() and send_typing() through the adapter -------------


async def test_send_posts_bearer_and_returns_send_result():
    a = adapter.HookMyAppAdapter(FakePlatformConfig(CONFIG))
    a._session = FakePostSession([FakeResponse(json_data={"messages": [{"id": "wamid.out1"}]})])
    result = await a.send(adapter.encode_chat_id("10001", "97254"), "hi")
    assert result.success is True
    assert result.message_id == "wamid.out1"
    url, payload, headers = a._session.calls[0]
    assert url == "https://gw.example/graph/10001/messages"
    assert payload["to"] == "97254"
    assert headers["Authorization"] == "Bearer hmat_test"


async def test_send_typing_posts_with_wamid():
    a = adapter.HookMyAppAdapter(FakePlatformConfig(CONFIG))
    a._session = FakePostSession([FakeResponse(json_data={})])
    await a.send_typing(adapter.encode_chat_id("10001", "97254"), metadata={"wamid": "wamid.1"})
    url, payload, headers = a._session.calls[0]
    assert url == "https://gw.example/graph/10001/messages"
    assert payload["message_id"] == "wamid.1"
    assert headers["Authorization"] == "Bearer hmat_test"


async def test_send_typing_without_message_id_is_a_noop_never_raises():
    a = adapter.HookMyAppAdapter(FakePlatformConfig(CONFIG))
    a._session = FakePostSession([])  # would raise IndexError if a call were made
    await a.send_typing(adapter.encode_chat_id("10001", "97254"))  # metadata=None
    assert a._session.calls == []
