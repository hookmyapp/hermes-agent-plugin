import hashlib
import hmac as hmac_mod

import adapter

SECRET = "test-secret"


def sign(body: bytes, secret: str = SECRET) -> str:
    return "sha256=" + hmac_mod.new(secret.encode(), body, hashlib.sha256).hexdigest()


def test_valid_signature_accepted():
    body = b'{"entry":[]}'
    assert adapter.verify_signature(body, sign(body), SECRET) is True


def test_prefixless_header_accepted():
    body = b'{"entry":[]}'
    assert adapter.verify_signature(body, sign(body).removeprefix("sha256="), SECRET) is True


def test_wrong_secret_rejected():
    body = b'{"entry":[]}'
    assert adapter.verify_signature(body, sign(body, "other-secret"), SECRET) is False


def test_tampered_body_rejected():
    assert adapter.verify_signature(b"tampered", sign(b"original"), SECRET) is False


def test_missing_header_rejected():
    assert adapter.verify_signature(b"x", None, SECRET) is False


def test_missing_secret_rejected_not_fail_open():
    body = b"x"
    assert adapter.verify_signature(body, sign(body), None) is False
    assert adapter.verify_signature(body, sign(body), "") is False


def test_probe_get_detected():
    assert adapter.is_probe("GET", {"X-HookMyApp-Probe": "webhook-verification"}, b"") is True


def test_probe_empty_post_detected():
    assert adapter.is_probe("POST", {"X-HookMyApp-Probe": "webhook-verification"}, b"") is True


def test_post_with_body_is_not_probe_even_with_header():
    assert adapter.is_probe("POST", {"X-HookMyApp-Probe": "webhook-verification"}, b"{}") is False


def test_no_probe_header_is_not_probe():
    assert adapter.is_probe("GET", {}, b"") is False
