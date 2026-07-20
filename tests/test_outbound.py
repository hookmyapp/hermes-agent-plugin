import adapter
from tests.test_media import FakeResponse


class FakePostSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def post(self, url, json=None, headers=None):
        self.calls.append((url, json, headers))
        return self.responses.pop(0)


def test_markdown_conversion():
    assert adapter.markdown_to_whatsapp("**bold** and ~~gone~~") == "*bold* and ~gone~"
    assert adapter.markdown_to_whatsapp("[docs](https://x.example/a)") == "docs (https://x.example/a)"
    assert adapter.markdown_to_whatsapp("# Title\nbody") == "*Title*\nbody"


def test_markdown_table_is_flattened():
    text = "| A | B |\n|---|---|\n| 1 | 2 |"
    assert adapter.markdown_to_whatsapp(text) == "A — B\n1 — 2"


def test_markdown_nested_bold_strips_inner_stars():
    assert adapter.markdown_to_whatsapp("**bold *italic* text**") == "*bold italic text*"


def test_split_short_message_single_chunk():
    assert adapter.split_message("hello") == ["hello"]


def test_split_strips_leading_whitespace_from_every_chunk():
    chunks = adapter.split_message("\n" + "x" * 5000)
    assert all(not c[:1].isspace() for c in chunks if c)


def test_split_prefers_newline_boundary():
    text = ("a" * 4000) + "\n" + ("b" * 200)
    chunks = adapter.split_message(text)
    assert chunks == ["a" * 4000, "b" * 200]
    assert all(len(c) <= 4096 for c in chunks)


def test_split_hard_cut_without_boundaries():
    chunks = adapter.split_message("x" * 9000)
    assert [len(c) for c in chunks] == [4096, 4096, 808]


async def test_send_success_returns_all_chunk_ids():
    session = FakePostSession([
        FakeResponse(json_data={"messages": [{"id": "wamid.out1"}]}),
        FakeResponse(json_data={"messages": [{"id": "wamid.out2"}]}),
    ])
    result = await adapter.send_whatsapp_text(
        session, "https://gw.example/graph", "hmat_1", "10001", "97254", "y" * 5000)
    assert result == {"success": True, "message_ids": ["wamid.out1", "wamid.out2"]}
    url, payload, headers = session.calls[0]
    assert url == "https://gw.example/graph/10001/messages"
    assert payload["messaging_product"] == "whatsapp"
    assert payload["to"] == "97254"
    assert headers["Authorization"] == "Bearer hmat_1"


async def test_send_5xx_is_retryable():
    session = FakePostSession([FakeResponse(status=503, json_data={"error": {"message": "down"}})])
    result = await adapter.send_whatsapp_text(session, "https://gw", "t", "10001", "97254", "hi")
    assert result["success"] is False
    assert result["retryable"] is True


async def test_send_4xx_is_not_retryable():
    session = FakePostSession([FakeResponse(status=400, json_data={"error": {"message": "bad"}})])
    result = await adapter.send_whatsapp_text(session, "https://gw", "t", "10001", "97254", "hi")
    assert result["success"] is False
    assert result["retryable"] is False


async def test_send_network_error_is_retryable():
    class BoomSession:
        def post(self, url, json=None, headers=None):
            raise OSError("connection refused")

    result = await adapter.send_whatsapp_text(BoomSession(), "https://gw", "t", "10001", "97254", "hi")
    assert result["success"] is False
    assert result["retryable"] is True


async def test_send_5xx_after_partial_delivery_is_not_retryable():
    session = FakePostSession([
        FakeResponse(json_data={"messages": [{"id": "wamid.out1"}]}),
        FakeResponse(status=500, json_data={"error": {"message": "down"}}),
    ])
    text = ("a" * 4096) + "\n" + ("b" * 4096) + "\n" + ("c" * 100)
    result = await adapter.send_whatsapp_text(session, "https://gw", "t", "10001", "97254", text)
    assert result["success"] is False
    assert result["retryable"] is False
    assert result["message_ids"] == ["wamid.out1"]
