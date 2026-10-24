import subprocess

import pytest
from conftest import make_cfg

from reply_desk import pages
from reply_desk.pages import CommandPublisher, PublishError, check_live

REPLY = {"first_name": "Pat", "company": "Acme <Plumbing> & Co", "lead_email": "pat@example.com"}


def test_render_escapes_lead_data_and_carries_the_marker(tmp_path):
    doc, marker = pages.render(make_cfg(tmp_path), REPLY)
    assert "Acme &lt;Plumbing&gt; &amp; Co" in doc and "<Plumbing>" not in doc
    assert '<meta name="reply-desk-page" content="%s">' % marker in doc
    assert 'content="noindex, nofollow"' in doc and 'href="https://example.com/book"' in doc


def test_marker_changes_with_the_content(tmp_path):
    _, a = pages.render(make_cfg(tmp_path), REPLY)
    _, b = pages.render(make_cfg(tmp_path), {**REPLY, "company": "Other Co"})
    assert a != b and a.startswith("rd-")


def test_only_http_booking_links_become_buttons(tmp_path):
    doc, _ = pages.render(make_cfg(tmp_path, business={"booking_url": "javascript:alert(1)"}), REPLY)
    assert "javascript:" not in doc and 'class="cta"' not in doc


def test_build_writes_the_page_and_returns_its_url(tmp_path):
    cfg = make_cfg(tmp_path)
    page, note = pages.build(cfg, REPLY, pages.LocalPublisher())
    assert page.url == "https://pages.example.com/%s/" % page.slug and page.path.is_file()
    assert page.slug.startswith("acme-plumbing-co-") and "written to" in note


@pytest.mark.parametrize("answer,ok", [
    ((200, "<html>rd-abc</html>"), True),
    ((200, "<html>an older copy</html>"), False),
    ((404, ""), False),
    ((500, "rd-abc"), False),
])
def test_check_live_needs_200_and_the_marker(answer, ok):
    seen = []

    def fetch(url):
        seen.append(url)
        return answer

    assert check_live("https://pages.example.com/acme/", "rd-abc", fetch)[0] is ok
    assert seen[0].startswith("https://pages.example.com/acme/?rd-check=")


def test_check_live_fails_closed():
    def broken(url):
        raise OSError("no route")

    assert check_live("https://pages.example.com/acme/", "rd-abc", broken) == (False, "the GET failed: OSError")
    assert check_live("file:///tmp/x", "rd-abc", broken)[0] is False


def test_command_publisher_fills_in_the_folder_and_slug(tmp_path):
    calls = []

    def run(argv, **kw):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0)

    CommandPublisher(["deploy", "{site_dir}", "--only", "{slug}"], run=run).publish(tmp_path, "acme-1")
    assert calls == [["deploy", str(tmp_path), "--only", "acme-1"]]


def test_command_publisher_failures_raise():
    with pytest.raises(PublishError, match="exited with 3"):
        CommandPublisher(["x"], run=lambda argv, **kw: subprocess.CompletedProcess(argv, 3)).publish(None, "s")

    def slow(argv, **kw):
        raise subprocess.TimeoutExpired(argv, 1)

    with pytest.raises(PublishError, match="TimeoutExpired"):
        CommandPublisher(["x"], run=slow).publish(None, "s")


def test_page_http_errors_preserve_retry_after_and_do_not_expose_response_text(monkeypatch):
    import io
    import urllib.error

    from reply_desk.http import HttpError

    def limited(*args, **kwargs):
        raise urllib.error.HTTPError("https://pages.example.com/private", 429, "private response",
                                     {"Retry-After": "120"}, io.BytesIO(b"private response"))

    monkeypatch.setattr("urllib.request.urlopen", limited)
    with pytest.raises(HttpError) as error:
        pages.check_live("https://pages.example.com/test/", "rd-test", pages.urllib_fetch)
    assert error.value.retry_after == 120 and "private" not in str(error.value)
