import argparse
import json

import adapter


def make_parser():
    parser = argparse.ArgumentParser(prog="hermes hookmyapp")
    adapter.register_cli_subcommands(parser)
    return parser


def fake_cli(responses):
    """responses: {(first, second): (rc, stdout)} keyed by first two CLI args."""
    calls = []

    def run(*args):
        calls.append(args)
        rc, out = responses.get(tuple(args[:2]), (0, ""))
        return rc, out, ""

    return run, calls


def test_subcommands_registered():
    parser = make_parser()
    args = parser.parse_args(["setup", "--channel", "ch_1", "--port", "9000"])
    assert args.func is adapter.cmd_setup
    assert args.channel == "ch_1"
    args = parser.parse_args(["status"])
    assert args.func is adapter.cmd_status


def test_setup_pulls_env_and_prints_listen_oneliner(monkeypatch, tmp_path, capsys):
    env_json = json.dumps({"META_GRAPH_API_URL": "https://gw", "WHATSAPP_ACCESS_TOKEN": "hmat_secret",
                           "WHATSAPP_PHONE_NUMBER_ID": "10001", "WEBHOOK_HMAC_SECRET": "sekrit",
                           "VERIFY_TOKEN": "vt", "HOOKMYAPP_CHANNEL_ID": "ch_1"})
    run, calls = fake_cli({
        ("channels", "list"): (0, json.dumps([{"id": "ch_1", "name": "Main"}])),
        ("channels", "env"): (0, env_json),
    })
    monkeypatch.setattr(adapter, "_run_hookmyapp", run)
    monkeypatch.setattr(adapter.shutil, "which", lambda name: "/usr/bin/hookmyapp")
    monkeypatch.setattr(adapter, "ENV_FILE", tmp_path / ".env")
    args = make_parser().parse_args(["setup", "--port", "9000"])
    adapter.cmd_setup(args)
    out = capsys.readouterr().out
    env_text = (tmp_path / ".env").read_text()
    assert "WEBHOOK_HMAC_SECRET=sekrit" in env_text
    assert "sekrit" not in out and "hmat_secret" not in out  # secrets masked
    assert "hookmyapp channels listen ch_1 --port 9000" in out
    assert "hermes gateway restart" in out


def test_setup_with_webhook_url_registers_webhook(monkeypatch, tmp_path, capsys):
    run, calls = fake_cli({
        ("channels", "list"): (0, json.dumps([{"id": "ch_1", "name": "Main"}])),
        ("channels", "env"): (0, json.dumps({"VERIFY_TOKEN": "vt"})),
        ("channels", "webhook"): (0, "ok"),
    })
    monkeypatch.setattr(adapter, "_run_hookmyapp", run)
    monkeypatch.setattr(adapter.shutil, "which", lambda name: "/usr/bin/hookmyapp")
    monkeypatch.setattr(adapter, "ENV_FILE", tmp_path / ".env")
    args = make_parser().parse_args(["setup", "--webhook-url", "https://me.example/hookmyapp/webhook"])
    adapter.cmd_setup(args)
    assert ("channels", "webhook", "set", "ch_1", "--url",
            "https://me.example/hookmyapp/webhook") in calls


def test_setup_fail_open_without_cli(monkeypatch, capsys):
    monkeypatch.setattr(adapter.shutil, "which", lambda name: None)
    args = make_parser().parse_args(["setup"])
    adapter.cmd_setup(args)  # must not raise
    out = capsys.readouterr().out
    assert "npm install -g @gethookmyapp/cli" in out
    assert "channels env" in out  # manual instructions printed


def test_status_masks_secrets(monkeypatch, capsys):
    monkeypatch.setenv("WEBHOOK_HMAC_SECRET", "supersecret")
    monkeypatch.delenv("VERIFY_TOKEN", raising=False)
    args = make_parser().parse_args(["status"])
    adapter.cmd_status(args)
    out = capsys.readouterr().out
    assert "supersecret" not in out
    assert "WEBHOOK_HMAC_SECRET" in out and "VERIFY_TOKEN" in out
