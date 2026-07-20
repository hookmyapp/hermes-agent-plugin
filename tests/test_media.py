import adapter


class FakeContent:
    def __init__(self, body):
        self._body = body

    async def iter_chunked(self, size):
        for start in range(0, len(self._body), size):
            yield self._body[start:start + size]


class FakeResponse:
    def __init__(self, status=200, json_data=None, body=b"", headers=None):
        self.status = status
        self._json = json_data
        self._body = body
        self.headers = headers or {}
        self.content = FakeContent(body)

    async def json(self, content_type=None):
        return self._json

    async def read(self):
        return self._body

    def raise_for_status(self):
        if self.status >= 400:
            raise RuntimeError(f"HTTP {self.status}")

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.responses.pop(0)


async def test_media_id_resolves_bearer_only_on_metadata_no_redirects():
    session = FakeSession([
        FakeResponse(json_data={"url": "https://signed.example/x", "mime_type": "image/jpeg"}),
        FakeResponse(body=b"JPEGDATA"),
    ])
    result = await adapter.resolve_media(session, "https://gw.example/graph", "hmat_1", "media123")
    assert result == (b"JPEGDATA", "image/jpeg")
    meta_url, meta_kwargs = session.calls[0]
    signed_url, signed_kwargs = session.calls[1]
    assert meta_url == "https://gw.example/graph/media123"
    assert meta_kwargs["headers"]["Authorization"] == "Bearer hmat_1"
    assert signed_url == "https://signed.example/x"
    # Signed URL is its own credential — the bearer token must NOT go to it.
    assert "Authorization" not in (signed_kwargs.get("headers") or {})
    assert meta_kwargs["allow_redirects"] is False
    assert signed_kwargs["allow_redirects"] is False


async def test_lookup_failure_returns_none():
    session = FakeSession([FakeResponse(status=404, json_data={})])
    assert await adapter.resolve_media(session, "https://gw.example", "t", "gone") is None


async def test_download_failure_returns_none():
    session = FakeSession([
        FakeResponse(json_data={"url": "https://signed.example/x", "mime_type": "image/jpeg"}),
        FakeResponse(status=500),
    ])
    assert await adapter.resolve_media(session, "https://gw.example", "t", "m") is None


async def test_missing_url_returns_none():
    session = FakeSession([FakeResponse(json_data={"mime_type": "image/jpeg"})])
    assert await adapter.resolve_media(session, "https://gw.example", "t", "m") is None


async def test_oversized_content_length_rejected():
    session = FakeSession([
        FakeResponse(json_data={"url": "https://signed.example/x", "mime_type": "image/jpeg"}),
        FakeResponse(body=b"x", headers={"Content-Length": str(adapter.MAX_MEDIA_BYTES + 1)}),
    ])
    assert await adapter.resolve_media(session, "https://gw.example", "t", "m") is None


async def test_streamed_bytes_over_cap_rejected(monkeypatch):
    monkeypatch.setattr(adapter, "MAX_MEDIA_BYTES", 4)
    session = FakeSession([
        FakeResponse(json_data={"url": "https://signed.example/x", "mime_type": "image/jpeg"}),
        FakeResponse(body=b"12345"),  # 5 bytes > patched 4-byte cap, no Content-Length header
    ])
    assert await adapter.resolve_media(session, "https://gw.example", "t", "m") is None


def test_cache_fallback_writes_tmp_file(tmp_path):
    path = adapter.cache_image_from_bytes(b"PNG", "png")
    assert path.endswith(".png")
    with open(path, "rb") as f:
        assert f.read() == b"PNG"
