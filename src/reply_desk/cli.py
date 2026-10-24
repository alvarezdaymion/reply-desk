"""Command line: reply-desk demo | init | check | run | status | resolve."""

from __future__ import annotations

import argparse
import datetime as dt
import os
import shutil
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from . import config as config_mod
from . import demo
from .desk import Desk
from .status import status_lines
from .store import Store

TEMPLATE = Path(__file__).parent / "templates" / "reply-desk.example.toml"
REQUIRED_ENV = ("INSTANTLY_API_KEY", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID")


def load_env(path: Path) -> None:
    """Read KEY=VALUE lines from a .env file into the environment. Variables already set win."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        key, sep, value = line.strip().partition("=")
        if sep and key and not key.startswith("#"):
            os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


@contextmanager
def run_lock(state_dir: Path) -> Iterator[None]:
    """One process at a time per state folder, including manual send resolution."""
    state_dir.mkdir(parents=True, exist_ok=True)
    try:
        import fcntl
    except ImportError:  # not POSIX: rely on the ledger's conditional updates alone
        yield
        return
    with open(state_dir / "run.lock", "w") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit("another reply-desk process is using %s. Stop the service before resolve, then restart it."
                             % state_dir) from None
        yield


def build_desk(cfg: config_mod.Config, say: Any = print) -> Desk:
    from .connectors.instantly import InstantlyClient
    from .connectors.telegram import TelegramClient

    try:
        inbox, telegram = InstantlyClient(), TelegramClient()
    except RuntimeError as e:
        raise SystemExit("%s (see .env.example), then run: reply-desk check" % e) from None
    drafter = None
    if cfg["model"]["enabled"]:
        try:
            from .llm import ModelDrafter

            drafter = ModelDrafter(cfg)
        except ImportError:
            print("[model] enabled = true, but the llm extra is missing (uv sync --extra llm). Using the rules.")
        except Exception as e:
            print("[model] could not start (%s). Using the rules." % type(e).__name__)
    return Desk(cfg, Store(cfg.db_path), inbox, telegram, drafter=drafter, say=say)


def cmd_demo(args: argparse.Namespace) -> int:
    return demo.run(Path(args.dir))


def cmd_init(args: argparse.Namespace) -> int:
    dest = Path(args.config)
    if dest.exists() and not args.force:
        print("%s already exists (use --force to overwrite)" % dest)
        return 1
    shutil.copyfile(TEMPLATE, dest)
    print("Wrote %s. Edit [business], [pages] and [drafts], put credentials in .env, then run: reply-desk check" % dest)
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    cfg = config_mod.load(args.config)
    print("Config OK: %s" % Path(args.config).resolve())
    mode = cfg["sending"]["mode"]
    print("Sending: %s (%s)" % (mode, "approved replies are sent" if mode == "live" else
                                "Send runs every check and sends nothing"))
    print("Readbacks after each send: %s seconds" % ", ".join("%g" % s for s in cfg["sending"]["readback_seconds"]))
    pages = cfg["pages"]
    print("Pages: %s" % ("%s, publisher %s" % (pages["base_url"], pages["publisher"]) if pages["enabled"] else "off"))
    print("Follow-ups: %s" % (", ".join("+%d days" % g for g in cfg["followups"]["gaps_days"]) or "off"))
    print("Model drafts: %s" % ("on (%s)" % cfg["model"]["model"] if cfg["model"]["enabled"] else "off (rules only)"))
    need = list(REQUIRED_ENV) + (["ANTHROPIC_API_KEY"] if cfg["model"]["enabled"] else [])
    missing = [v for v in need if not os.environ.get(v)]
    for v in need:
        print("  env %-20s %s" % (v, "missing" if v in missing else "set"))
    if args.online and os.environ.get("TELEGRAM_BOT_TOKEN") and "TELEGRAM_CHAT_ID" in missing:
        from .connectors.telegram import recent_chats

        try:
            chats = ", ".join("%s (%s)" % c for c in recent_chats(os.environ["TELEGRAM_BOT_TOKEN"]))
            print("  chats that messaged the bot lately: %s" % (chats or "none; message the bot, then run this again"))
        except Exception as e:  # never contains the token
            print("  %s" % e)
    if args.online and not missing:
        from .connectors.instantly import InstantlyClient
        from .connectors.telegram import TelegramClient

        for client in (InstantlyClient, TelegramClient):
            try:
                print("  " + client().ping())
            except Exception as e:  # errors from these clients never contain the token
                print("  %s: %s" % (client.__name__, e))
                missing.append(client.__name__)
    if args.page:
        if not pages["enabled"]:
            print("  pages are off; no page to check")
        elif not check_page(cfg):
            missing.append("page")
    return 1 if missing else 0


def check_page(cfg: config_mod.Config, fetch: Any = None, sleep: Any = time.sleep) -> bool:
    """Publish a test page and run the same live check a real reply gets."""
    from . import pages
    from .http import HttpError, retry_delay

    fetch = fetch or pages.urllib_fetch
    test = {"first_name": "Test", "company": "Reply Desk check", "lead_email": "check@example.com"}
    page, note = pages.build(cfg, test, pages.publisher_for(cfg))
    print("  test page %s (%s)" % (page.url, note))
    try:
        ok, why = pages.check_live(page.url, page.marker, fetch)
        for _ in range(4):  # a host can take a few seconds to serve a new deploy
            if ok:
                break
            sleep(3)
            ok, why = pages.check_live(page.url, page.marker, fetch)
    except HttpError as e:
        print("  live check: %s; wait at least %g seconds before checking again" % (e, retry_delay(e)))
        return False
    print("  live check: %s" % why)
    return ok


def cmd_run(args: argparse.Namespace) -> int:
    cfg = config_mod.load(args.config)
    with run_lock(cfg.state_dir):
        desk = build_desk(cfg, say=(lambda _: None) if args.quiet else print)
        poll_every, last_poll = float(cfg["inbox"]["poll_seconds"]), -1e9
        failures = 0
        try:
            while True:
                poll = not args.loop or time.monotonic() - last_poll >= poll_every
                if poll:
                    last_poll = time.monotonic()
                report = desk.tick(updates_timeout=25 if args.loop else 0, poll=poll)
                stats = ", ".join("%s %d" % (k.replace("_", " "), v) for k, v in sorted(report["stats"].items()) if v)
                if stats or report["errors"] or not args.loop:
                    print("%s %s" % (dt.datetime.now().strftime("%H:%M:%S"), stats or "nothing new"))
                for err in report["errors"]:
                    print("  error in %s" % err)
                if not args.loop:
                    return 1 if report["errors"] else 0
                failures = failures + 1 if report["errors"] else 0
                if failures:
                    time.sleep(max(min(60, 2 ** min(failures - 1, 6)), report.get("retry_after") or 0))
        except KeyboardInterrupt:
            print("stopped")
            return 0


def cmd_status(args: argparse.Namespace) -> int:
    cfg = config_mod.load(args.config)
    if not cfg.db_path.exists():
        print("No state yet at %s. Run: reply-desk run" % cfg.db_path)
        return 0
    store = Store(cfg.db_path)
    for line in status_lines(store, cfg.tz()):
        print(line)
    waiting = len(store.cards("pending"))
    print("\nCards waiting for a tap: %d. Mode: %s." % (waiting, cfg["sending"]["mode"]))
    return 0


def cmd_resolve(args: argparse.Namespace) -> int:
    cfg = config_mod.load(args.config)
    with run_lock(cfg.state_dir):
        desk = build_desk(cfg)
        if args.recheck:
            ids = [args.send_id] if args.send_id else [s["id"] for s in desk.store.sends("locked")]
            for send_id in ids:
                print(desk.recheck(send_id))
            if not ids:
                print("No locked sends.")
            return 0
        if args.send_id is None or args.sent == args.not_sent:
            print("Give a send ID and exactly one of --sent or --not-sent (or use --recheck).")
            return 2
        print(desk.resolve(args.send_id, sent=args.sent, note=args.note))
        return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="reply-desk", description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("demo", help="run a synthetic inbox end to end, no keys or network needed")
    d.add_argument("--dir", default="reply-desk-demo", help="where the demo writes its state and pages")
    d.set_defaults(fn=cmd_demo)
    i = sub.add_parser("init", help="write reply-desk.toml from the example")
    i.add_argument("--config", default="reply-desk.toml")
    i.add_argument("--force", action="store_true")
    i.set_defaults(fn=cmd_init)
    c = sub.add_parser("check", help="validate the config and show which credentials are set")
    c.add_argument("--config", default="reply-desk.toml")
    c.add_argument("--online", action="store_true", help="also make one read-only call to Instantly and Telegram")
    c.add_argument("--page", action="store_true", help="publish a test page and check that it is live")
    c.set_defaults(fn=cmd_check)
    r = sub.add_parser("run", help="one tick: inbox, cards, taps, follow-ups (--loop to keep going)")
    r.add_argument("--config", default="reply-desk.toml")
    r.add_argument("--loop", action="store_true", help="keep running; taps are picked up within seconds")
    r.add_argument("--quiet", action="store_true", help="print only the per-tick summary")
    r.set_defaults(fn=cmd_run)
    s = sub.add_parser("status", help="counts, locked sends and planned follow-ups")
    s.add_argument("--config", default="reply-desk.toml")
    s.set_defaults(fn=cmd_status)
    v = sub.add_parser("resolve", help="settle a locked send after checking your sent folder")
    v.add_argument("send_id", nargs="?", type=int)
    v.add_argument("--sent", action="store_true", help="it went out")
    v.add_argument("--not-sent", action="store_true", help="it did not go out")
    v.add_argument("--recheck", action="store_true", help="read the locked send(s) back once more")
    v.add_argument("--note", default="")
    v.add_argument("--config", default="reply-desk.toml")
    v.set_defaults(fn=cmd_resolve)
    args = ap.parse_args(argv)
    if args.cmd in ("check", "run", "resolve"):
        load_env(Path(args.config).resolve().parent / ".env")
    return int(args.fn(args))


if __name__ == "__main__":
    sys.exit(main())
