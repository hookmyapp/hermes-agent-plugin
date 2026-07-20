import pytest

import adapter


def wa_payload(messages, statuses=None, pnid="10001", display="+1 555 000 1111", contacts=None):
    value = {"metadata": {"phone_number_id": pnid, "display_phone_number": display}}
    if messages:
        value["messages"] = messages
    if statuses:
        value["statuses"] = statuses
    if contacts:
        value["contacts"] = contacts
    return {"object": "whatsapp_business_account",
            "entry": [{"id": "waba1", "changes": [{"field": "messages", "value": value}]}]}


def text_msg(wamid="wamid.1", sender="972545000000", body="hi"):
    return {"id": wamid, "from": sender, "type": "text", "text": {"body": body}}


def test_single_text_message_extracted():
    items = adapter.extract_messages(wa_payload([text_msg()], contacts=[{"wa_id": "972545000000", "profile": {"name": "Or"}}]))
    assert len(items) == 1
    assert items[0]["phone_number_id"] == "10001"
    assert items[0]["sender"] == "972545000000"
    assert items[0]["user_name"] == "Or"
    assert items[0]["message"]["text"]["body"] == "hi"


def test_batched_payload_two_messages_two_events():
    payload = wa_payload([text_msg("wamid.a"), text_msg("wamid.b")])
    payload["entry"].append({"id": "waba1", "changes": [{"field": "messages", "value": {
        "metadata": {"phone_number_id": "10001", "display_phone_number": "+1 555 000 1111"},
        "messages": [text_msg("wamid.c")]}}]})
    items = adapter.extract_messages(payload)
    assert [i["message"]["id"] for i in items] == ["wamid.a", "wamid.b", "wamid.c"]


def test_statuses_only_payload_produces_no_events():
    payload = wa_payload([], statuses=[{"id": "wamid.x", "status": "delivered"}])
    assert adapter.extract_messages(payload) == []


def test_echo_from_business_number_skipped():
    payload = wa_payload([text_msg(sender="15550001111")])
    assert adapter.extract_messages(payload) == []


def test_empty_and_malformed_shapes_tolerated():
    assert adapter.extract_messages({}) == []
    assert adapter.extract_messages({"entry": [{}]}) == []
    assert adapter.extract_messages({"entry": [{"changes": [{"value": {}}]}]}) == []


def test_seen_ids_dedupe_and_bound():
    seen = adapter.SeenIds(maxsize=3)
    assert "a" not in seen  # membership check must not register
    assert seen.check_and_add("a") is True
    assert "a" in seen
    assert seen.check_and_add("a") is False
    for wamid in ("b", "c", "d"):
        assert seen.check_and_add(wamid) is True
    assert seen.check_and_add("a") is True  # evicted, bounded


def test_sender_allowlist_digits_e164_equivalence():
    assert adapter.is_sender_allowed("14155552671", "+14155552671", None) is True
    assert adapter.is_sender_allowed("+14155552671", "14155552671", None) is True
    assert adapter.is_sender_allowed("15550001111", "+14155552671", None) is False


def test_sender_allowlist_default_closed_and_allow_all():
    assert adapter.is_sender_allowed("14155552671", None, None) is False
    assert adapter.is_sender_allowed("14155552671", "", None) is False
    assert adapter.is_sender_allowed("14155552671", None, "true") is True


def test_chat_id_roundtrip():
    chat_id = adapter.encode_chat_id("10001", "972545000000")
    assert chat_id.startswith("hookmyapp:")
    assert adapter.decode_chat_id(chat_id) == ("10001", "972545000000")


def test_resolve_chat_id_grammars():
    full = adapter.encode_chat_id("10001", "972545000000")
    assert adapter.resolve_chat_id(full, "999") == ("10001", "972545000000")
    assert adapter.resolve_chat_id("10001:972545000000", "999") == ("10001", "972545000000")
    assert adapter.resolve_chat_id("972545000000", "999") == ("999", "972545000000")


def test_resolve_bare_number_without_default_raises():
    with pytest.raises(ValueError):
        adapter.resolve_chat_id("972545000000", None)
