import os

import pytest
from conftest import Rig, make_cfg

from reply_desk import cli, config
from reply_desk.demo import site_fetch


def test_demo_runs_end_to_end_with_no_keys(tmp_path, capsys, monkeypatch):
    for var in ("INSTANTLY_API_KEY", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    assert cli.main(["demo", "--dir", str(tmp_path / "demo")]) == 0
    out = capsys.readouterr().out
    assert "send 1: readback 2 at 10 s matched" in out  # the flaky send, confirmed by readback
    assert "send 2: locked" in out  # the one that never shows up
    assert "found it in the thread" in out  # the restart mid-send
    assert out.count("Already handled. Nothing was sent again.") == 2
    assert "Instantly send calls: 4, one per approved card. Repeats: 0." in out
    assert list((tmp_path / "demo" / "site").glob("*/index.html"))


def test_demo_refuses_a_folder_it_did_not_create(tmp_path):
    (tmp_path / "notes.txt").write_text("mine")
    with pytest.raises(SystemExit):
        cli.main(["demo", "--dir", str(tmp_path)])


def test_init_writes_a_config_that_loads_with_safe_defaults(tmp_path, capsys):
    path = tmp_path / "reply-desk.toml"
    assert cli.main(["init", "--config", str(path)]) == 0
    cfg = config.load(path)
    assert cfg["sending"]["mode"] == "dry-run" and cfg["inbox"]["blocklist_unsubscribes"] is False
    assert cfg["drafts"] == config.DEFAULT_DRAFTS  # the example mirrors the defaults
    assert cli.main(["init", "--config", str(path)]) == 1  # no overwrite without --force


def test_check_reports_missing_credentials(tmp_path, capsys, monkeypatch):
    for var in cli.REQUIRED_ENV:
        monkeypatch.delenv(var, raising=False)
    path = tmp_path / "reply-desk.toml"
    cli.main(["init", "--config", str(path)])
    assert cli.main(["check", "--config", str(path)]) == 1
    assert "INSTANTLY_API_KEY    missing" in capsys.readouterr().out


def test_env_file_fills_gaps_but_never_overrides(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "from-shell")
    monkeypatch.delenv("INSTANTLY_API_KEY", raising=False)
    (tmp_path / ".env").write_text("# comment\nINSTANTLY_API_KEY='from-file'\nTELEGRAM_CHAT_ID=from-file\n")
    cli.load_env(tmp_path / ".env")
    assert os.environ["INSTANTLY_API_KEY"] == "from-file" and os.environ["TELEGRAM_CHAT_ID"] == "from-shell"


@pytest.mark.parametrize("bad,match", [
    ({"sending": {"mode": "yolo"}}, "dry-run or live"),
    ({"sending": {"readback_seconds": [10, 3]}}, "increasing"),
    ({"drafts": {"question": "Hi {name}"}}, "unknown placeholders"),
    ({"pages": {"publisher": "command"}}, "pages.command"),
    ({"followups": {"timezone": "Mars/Base"}}, "time zone"),
    ({"answers": [{"text": "no keywords"}]}, "keywords"),
])
def test_config_rejects_mistakes(tmp_path, bad, match):
    with pytest.raises(ValueError, match=match):
        config.from_dict(bad, tmp_path)


def test_check_page_publishes_a_test_page_and_runs_the_live_check(tmp_path, capsys):
    cfg = make_cfg(tmp_path)
    assert cli.check_page(cfg, fetch=site_fetch(cfg)) is True
    waits = []
    assert cli.check_page(cfg, fetch=lambda url: (404, ""), sleep=waits.append) is False
    assert waits == [3, 3, 3, 3] and "HTTP 404" in capsys.readouterr().out


def test_run_status_and_resolve_commands(tmp_path, capsys, monkeypatch):
    rig = Rig(tmp_path)
    monkeypatch.setattr(cli, "build_desk", lambda cfg, say=print: rig.desk)
    path = tmp_path / "reply-desk.toml"
    path.write_text('[sending]\nmode = "live"\n[followups]\ntimezone = "UTC"\n')
    rig.inbox.add_inbound("pat@example.com", "Sounds interesting, tell me more.")
    assert cli.main(["run", "--config", str(path)]) == 0
    card = rig.card("pat@example.com")
    rig.inbox.script = ["never"]
    rig.tg.tap(card["id"])
    assert cli.main(["run", "--config", str(path)]) == 0
    assert cli.main(["status", "--config", str(path)]) == 0
    out = capsys.readouterr().out
    assert "replies 1" in out and "Locked sends." in out and "#1 to pat@example.com" in out
    assert cli.main(["resolve", "--config", str(path)]) == 2
    assert cli.main(["resolve", "1", "--not-sent", "--config", str(path)]) == 0
    assert "send 1 marked not sent" in capsys.readouterr().out
    assert rig.store.card(card["id"])["state"] == "closed" and len(rig.inbox.sent) == 1


def test_default_timezone_is_utc_and_empty_timezone_is_rejected(tmp_path):
    import datetime as dt

    cfg = config.from_dict({}, tmp_path)
    assert cfg.tz() is dt.UTC
    with pytest.raises(ValueError, match="time zone"):
        config.from_dict({"followups": {"timezone": ""}}, tmp_path)


def test_resolve_lock_explains_service_stop_and_restart(tmp_path, monkeypatch):
    rig = Rig(tmp_path)
    path = tmp_path / "reply-desk.toml"
    path.write_text("")
    monkeypatch.setattr(cli, "build_desk", lambda cfg: rig.desk)
    with cli.run_lock(rig.cfg.state_dir), pytest.raises(SystemExit, match="[Ss]top.*resolve.*restart"):
        cli.main(["resolve", "1", "--not-sent", "--config", str(path)])


def test_loop_backs_off_on_errors_and_respects_retry_delay(tmp_path, monkeypatch):
    from reply_desk.http import HttpError

    rig = Rig(tmp_path)
    path = tmp_path / "reply-desk.toml"
    path.write_text("")
    calls, sleeps = [], []

    def unavailable(offset, timeout):
        calls.append(True)
        if len(calls) == 4:
            raise KeyboardInterrupt()
        error = HttpError("rate limited", 429)
        error.retry_after = 7 if len(calls) == 2 else None
        raise error

    monkeypatch.setattr(rig.tg, "updates", unavailable)
    monkeypatch.setattr(cli, "build_desk", lambda cfg, say=print: rig.desk)

    def sleep(seconds):
        sleeps.append(seconds)
        rig.clock.sleep(seconds)

    monkeypatch.setattr(cli.time, "sleep", sleep)
    assert cli.main(["run", "--loop", "--config", str(path)]) == 0
    assert sleeps == [1, 7, 4]


def test_canned_answers_reject_placeholders(tmp_path):
    with pytest.raises(ValueError, match="answers.*placeholders"):
        make_cfg(tmp_path, answers=[{"keywords": ["cost"], "text": "It costs {price}."}])


def test_status_lists_uncertain_card_posts(tmp_path, monkeypatch, capsys):
    from reply_desk.http import HttpError
    from reply_desk.status import status_lines

    rig = Rig(tmp_path)

    def unavailable(*args):
        raise HttpError("response lost")

    monkeypatch.setattr(rig.tg, "send_card", unavailable)
    rig.inbox.add_inbound("lead@example.com", "Tell me more")
    rig.desk.tick()
    output = "\n".join(status_lines(rig.store, rig.cfg.tz()))
    assert "Locked card posts" in output and rig.card("lead@example.com")["id"] in output
