import adapter


def test_env_wins_over_yaml(monkeypatch):
    adapter.apply_yaml_config({"HOOKMYAPP_PORT": "9999"}, None)
    monkeypatch.setenv("HOOKMYAPP_PORT", "7777")
    assert adapter.get_setting("HOOKMYAPP_PORT") == "7777"
    adapter.apply_yaml_config(None, None)


def test_yaml_wins_over_default_and_platform_cfg_wins_over_yaml(monkeypatch):
    monkeypatch.delenv("HOOKMYAPP_PORT", raising=False)
    adapter.apply_yaml_config({"HOOKMYAPP_PORT": "9999"},
                              {"HOOKMYAPP_PORT": "8888"})
    assert adapter.get_setting("HOOKMYAPP_PORT") == "8888"
    adapter.apply_yaml_config(None, None)


def test_defaults(monkeypatch):
    for name in ("HOOKMYAPP_HOST", "HOOKMYAPP_PORT", "HOOKMYAPP_WEBHOOK_PATH"):
        monkeypatch.delenv(name, raising=False)
    assert adapter.get_setting("HOOKMYAPP_HOST") == "0.0.0.0"
    assert adapter.get_setting("HOOKMYAPP_PORT") == "8649"
    assert adapter.get_setting("HOOKMYAPP_WEBHOOK_PATH") == "/hookmyapp/webhook"


def test_empty_env_falls_through(monkeypatch):
    monkeypatch.setenv("HOOKMYAPP_PORT", "")
    assert adapter.get_setting("HOOKMYAPP_PORT") == "8649"


def test_extra_get_reads_platform_config_extra_and_plain_dict():
    class Cfg:  # real-shaped: PlatformConfig keeps values under .extra
        extra = {"VERIFY_TOKEN": "vt"}

    assert adapter._extra_get(Cfg(), "VERIFY_TOKEN") == "vt"
    assert adapter._extra_get({"VERIFY_TOKEN": "vt2"}, "VERIFY_TOKEN") == "vt2"
    assert adapter._extra_get(None, "VERIFY_TOKEN") is None


def test_save_env_value_upserts(tmp_path):
    env = tmp_path / ".env"
    adapter._save_env_value("WHATSAPP_PHONE_NUMBER_ID", "111", path=env)
    adapter._save_env_value("VERIFY_TOKEN", "tok", path=env)
    adapter._save_env_value("WHATSAPP_PHONE_NUMBER_ID", "222", path=env)
    lines = env.read_text().splitlines()
    assert "WHATSAPP_PHONE_NUMBER_ID=222" in lines
    assert "VERIFY_TOKEN=tok" in lines
    assert sum(1 for l in lines if l.startswith("WHATSAPP_PHONE_NUMBER_ID=")) == 1
