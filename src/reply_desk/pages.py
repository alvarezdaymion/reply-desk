"""A one-page site for an interested lead: render it, publish it, and prove it is live before any
reply that links to it can send.

- render: fills templates/page.html from [pages] and the lead's first name and company, all HTML
  escaped. The page carries <meta name="reply-desk-page" content="<marker>">, a hash of its
  content, so the live check can tell this version from a stale or cached copy. Pages are noindex.
- publish: `local` only writes site/<slug>/index.html (serve that folder from any static host);
  `command` also runs your deploy command, without a shell, with {site_dir} and {slug} filled in.
- check_live: an HTTP GET of the page URL must return 200 and contain the marker.
"""

from __future__ import annotations

import hashlib
import html
import re
import secrets
import string
import subprocess
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .drafts import domain_of
from .http import USER_AGENT

TEMPLATE = Path(__file__).parent / "templates" / "page.html"
MARKER_SLOT = "__REPLY_DESK_MARKER__"

Fetch = Callable[[str], tuple[int, str]]


class PublishError(RuntimeError):
    pass


@dataclass(frozen=True)
class Page:
    slug: str
    url: str
    marker: str
    path: Path


def slug_for(company: str, lead_email: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", company.lower()).strip("-")[:40].strip("-") or "page"
    return "%s-%s" % (base, hashlib.sha256(lead_email.lower().encode()).hexdigest()[:6])


def render(cfg: Any, reply: dict[str, Any]) -> tuple[str, str]:
    """The page HTML and its marker."""
    p, biz = cfg["pages"], cfg["business"]
    values = {"first_name": reply.get("first_name") or "there",
              "company": reply.get("company") or domain_of(reply.get("lead_email", "")),
              "business": biz["name"]}

    def text(s: str) -> str:
        return html.escape(s.format(**values))

    booking = biz["booking_url"] if re.match(r"https?://", biz["booking_url"] or "") else ""
    doc = string.Template(TEMPLATE.read_text(encoding="utf-8")).substitute(
        title=html.escape("%s for %s" % (biz["name"], values["company"])),
        business=html.escape(biz["name"]),
        headline=text(p["headline"]),
        paragraphs="\n  ".join("<p>%s</p>" % text(x) for x in p["paragraphs"]),
        points="<ul>%s</ul>" % "".join("<li>%s</li>" % text(x) for x in p["points"]) if p["points"] else "",
        cta='<a class="cta" href="%s">%s</a>' % (html.escape(booking, quote=True), text(p["cta_label"]))
        if booking else "",
        marker=MARKER_SLOT,
    )
    marker = "rd-" + hashlib.sha256(doc.encode()).hexdigest()[:16]
    return doc.replace(MARKER_SLOT, marker), marker


def shown(path: Path) -> str:
    """A path relative to the working folder when it is inside it, for shorter output."""
    try:
        return str(path.resolve().relative_to(Path.cwd().resolve()))
    except ValueError:
        return str(path)


class LocalPublisher:
    """Writes the page and nothing else. Point a static host at the site folder."""

    def publish(self, site_dir: Path, slug: str) -> str:
        return "written to %s" % shown(site_dir / slug / "index.html")


class CommandPublisher:
    """Runs your deploy command after the page is written, e.g. ["rsync", "-a", "{site_dir}/", "host:/srv/pages/"]."""

    def __init__(self, argv: list[str], timeout: float = 120, run: Callable[..., Any] = subprocess.run):
        self.argv, self.timeout, self.run = list(argv), timeout, run

    def publish(self, site_dir: Path, slug: str) -> str:
        argv = [a.replace("{site_dir}", str(site_dir)).replace("{slug}", slug) for a in self.argv]
        try:
            done = self.run(argv, capture_output=True, text=True, timeout=self.timeout, check=False)
        except (OSError, subprocess.TimeoutExpired) as e:
            raise PublishError("the publish command did not run: %s" % type(e).__name__) from None
        if done.returncode != 0:
            raise PublishError("the publish command exited with %d" % done.returncode)
        return "published with %s" % argv[0]


def publisher_for(cfg: Any) -> LocalPublisher | CommandPublisher:
    p = cfg["pages"]
    if p["publisher"] == "command":
        return CommandPublisher(p["command"], float(p["command_timeout_seconds"]))
    return LocalPublisher()


def build(cfg: Any, reply: dict[str, Any], publisher: Any) -> tuple[Page, str]:
    """Write and publish the page. Returns the page and what the publisher did."""
    doc, marker = render(cfg, reply)
    slug = slug_for(reply.get("company") or domain_of(reply["lead_email"]), reply["lead_email"])
    path = cfg.site_dir / slug / "index.html"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(doc, encoding="utf-8")
    note = publisher.publish(cfg.site_dir, slug)
    return Page(slug, cfg["pages"]["base_url"].rstrip("/") + "/" + slug + "/", marker, path), note


def urllib_fetch(url: str) -> tuple[int, str]:
    req = urllib.request.Request(url, headers={"Cache-Control": "no-cache", "User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, resp.read(512 * 1024).decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, ""


def check_live(url: str, marker: str, fetch: Fetch = urllib_fetch) -> tuple[bool, str]:
    """True only when a GET of `url` returns 200 and the body contains this page's marker."""
    if not re.match(r"https?://", url or ""):
        return False, "not an http(s) URL"
    probe = url + ("&" if "?" in url else "?") + "rd-check=" + secrets.token_hex(4)  # skip caches
    try:
        status, body = fetch(probe)
    except Exception as e:
        return False, "the GET failed: %s" % type(e).__name__
    if status != 200:
        return False, "the GET returned HTTP %d" % status
    if marker not in body:
        return False, "the page answered but is not this version (marker missing)"
    return True, "HTTP 200 and the page marker matched"
