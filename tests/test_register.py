import adapter
from tests.test_adapter import FakePlatformConfig

REQUIRED = dict(
    META_GRAPH_API_URL="https://gw.example/graph",
    WHATSAPP_ACCESS_TOKEN="hmat_x",
    WHATSAPP_PHONE_NUMBER_ID="10001",
    WEBHOOK_HMAC_SECRET="s",
    VERIFY_TOKEN="v",
)


class FakeCtx:
    def __init__(self):
        self.platforms = {}
        self.cli = {}

    def register_platform(self, name, **kwargs):
        self.platforms[name] = kwargs

    def register_cli_command(self, name, **kwargs):
        self.cli[name] = kwargs


def set_required(monkeypatch):
    for key, value in REQUIRED.items():
        monkeypatch.setenv(key, value)


def clear_required(monkeypatch):
    for key in REQUIRED:
        monkeypatch.delenv(key, raising=False)


def test_register_wires_platform_and_cli():
    ctx = FakeCtx()
    adapter.register(ctx)
    p = ctx.platforms["hookmyapp"]
    assert p["cron_deliver_env_var"] == "HOOKMYAPP_HOME_CHANNEL"
    assert p["allowed_users_env"] == "HOOKMYAPP_ALLOWED_USERS"
    assert p["allow_all_env"] == "HOOKMYAPP_ALLOW_ALL_USERS"
    assert p["max_message_length"] == 4096
    assert p["install_hint"] == "pip install aiohttp"
    assert "24" in p["platform_hint"]  # 24h session window documented
    assert isinstance(p["adapter_factory"]({}), adapter.HookMyAppAdapter)
    assert "hookmyapp" in ctx.cli


def test_check_fn_returns_plain_bool_false_on_missing_env(monkeypatch):
    clear_required(monkeypatch)
    # MUST be the bool False — a (False, "msg") tuple is truthy and would
    # enable a broken platform.
    assert adapter.check_fn() is False


def test_check_fn_true_when_env_present(monkeypatch):
    set_required(monkeypatch)
    assert adapter.check_fn() is True


def test_env_enablement_fn_returns_flat_extra_dict(monkeypatch):
    clear_required(monkeypatch)
    monkeypatch.delenv("HOOKMYAPP_HOME_CHANNEL", raising=False)
    assert adapter.env_enablement_fn() is None
    set_required(monkeypatch)
    enablement = adapter.env_enablement_fn()
    # FLAT: the registry merges these keys into PlatformConfig.extra itself.
    # A nested {"extra": {...}} would land as extra["extra"] and hide creds.
    assert enablement["WHATSAPP_PHONE_NUMBER_ID"] == "10001"
    assert "extra" not in enablement
    assert "HOOKMYAPP_HOME_CHANNEL" not in enablement
    monkeypatch.setenv("HOOKMYAPP_HOME_CHANNEL", "97254")
    enablement = adapter.env_enablement_fn()
    assert enablement["HOOKMYAPP_HOME_CHANNEL"] == "97254"
    # Nested shape the registry's _apply_env_overrides pops specially to
    # build a real PlatformConfig.home_channel (Task 1 contract fact #3).
    assert enablement["home_channel"] == {"chat_id": "97254"}


def test_validate_config_receives_platform_config_with_extra(monkeypatch):
    clear_required(monkeypatch)
    assert adapter.validate_config(FakePlatformConfig(REQUIRED)) is True
    assert adapter.validate_config(FakePlatformConfig({})) is False


def test_is_connected_is_config_presence_not_adapter_liveness(monkeypatch):
    # Called during env auto-enablement BEFORE any adapter exists — deriving
    # it from a live adapter/socket would return False there and block
    # startup. Config-presence semantics, mirroring the built-in platforms.
    clear_required(monkeypatch)
    assert adapter.is_connected(FakePlatformConfig({})) is False
    assert adapter.is_connected(FakePlatformConfig(REQUIRED)) is True
    set_required(monkeypatch)
    assert adapter.is_connected(FakePlatformConfig({})) is True  # env fallback counts


async def test_standalone_sender_success(monkeypatch):
    async def fake_send(session, base_url, token, pnid, to, text):
        assert (pnid, to, text) == ("10001", "97254", "cron hello")
        return {"success": True, "message_ids": ["wamid.cron"]}

    monkeypatch.setattr(adapter, "send_whatsapp_text", fake_send)
    result = await adapter.standalone_sender(dict(REQUIRED), "97254", "cron hello")
    assert result == {"success": True, "message_id": "wamid.cron"}


async def test_standalone_sender_error(monkeypatch):
    async def fake_send(session, base_url, token, pnid, to, text):
        return {"success": False, "error": "down", "retryable": True, "message_ids": []}

    monkeypatch.setattr(adapter, "send_whatsapp_text", fake_send)
    result = await adapter.standalone_sender(dict(REQUIRED), "97254", "x")
    assert result == {"error": "down"}
