from __future__ import annotations

import json
import subprocess
from typing import Any

import pytest
from typer.testing import CliRunner

import cli.gmail.url as url_cli
from cli.main import app

_FAKE_HEX = "19e6c2e7b6b0a77d"
_FAKE_URL = f"https://mail.google.com/mail/u/0/#inbox/FMfcgzQ{_FAKE_HEX}stub"
_GWS_PAYLOAD: dict[str, Any] = {
    "thread_id": "thread123",
    "message_id": "msg@example.com",
    "references": [],
    "from": {"name": "Ada", "email": "ada@example.com"},
    "reply_to": None,
    "to": [{"name": None, "email": "you@example.com"}],
    "cc": None,
    "subject": "Hello [world] *bold*",
    "date": "Wed, 27 May 2026 18:24:03 -0700",
    "body_text": "Line one\n# Not a heading\n-- \nsig",
    "body_html": "<p>...</p>",
}


@pytest.fixture
def patch_decoder(monkeypatch: pytest.MonkeyPatch) -> None:
    # Bypass real Gmail URL decoding so tests don't depend on the encoded payload.
    def _fixed_hex_url(_url: str) -> str:
        return _FAKE_HEX

    def _fixed_hex_tok(_tok: str) -> str:
        return _FAKE_HEX

    def _fake_which(_binary: str) -> str:
        return "/fake/gws"

    monkeypatch.setattr(url_cli, "extract_id_from_url", _fixed_hex_url)
    monkeypatch.setattr(url_cli, "decode_token", _fixed_hex_tok)
    monkeypatch.setattr(url_cli.shutil, "which", _fake_which)


def _fake_completed(
    stdout: str,
    returncode: int = 0,
    stderr: str = "",
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=[],
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
    )


def test_decode_without_read_prints_hex_to_stdout(patch_decoder: None) -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["gmail", "url", "decode", _FAKE_URL])
    assert result.exit_code == 0
    assert result.stdout.strip() == _FAKE_HEX
    assert result.stderr == ""


def test_read_text_routes_hex_to_stderr(
    monkeypatch: pytest.MonkeyPatch,
    patch_decoder: None,
) -> None:
    captured_cmd: list[list[str]] = []

    def _fake_run(cmd: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        captured_cmd.append(cmd)
        return _fake_completed("Subject: hi\n\nbody text\n")

    monkeypatch.setattr(url_cli.subprocess, "run", _fake_run)
    runner = CliRunner()
    result = runner.invoke(app, ["gmail", "url", "decode", _FAKE_URL, "--read"])
    assert result.exit_code == 0
    # Hex on stderr, body on stdout — preserves piping while keeping the ID visible.
    assert _FAKE_HEX in result.stderr
    assert "body text" in result.stdout
    assert _FAKE_HEX not in result.stdout
    # text mode does not auto-include --headers and does not pass --html
    cmd = captured_cmd[0]
    assert cmd[:5] == ["/fake/gws", "gmail", "+read", "--id", _FAKE_HEX]
    assert "--headers" not in cmd
    assert "--html" not in cmd
    assert "--format" in cmd and cmd[cmd.index("--format") + 1] == "text"


def test_read_json_passthrough_is_pipeable(
    monkeypatch: pytest.MonkeyPatch,
    patch_decoder: None,
) -> None:
    payload = json.dumps({"subject": "x", "body_text": "y"})

    def _fake_run(cmd: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        assert "--headers" in cmd, "json mode must auto-include headers"
        assert cmd[cmd.index("--format") + 1] == "json"
        return _fake_completed(payload)

    monkeypatch.setattr(url_cli.subprocess, "run", _fake_run)
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["gmail", "url", "decode", _FAKE_URL, "--read", "--format", "json"],
    )
    assert result.exit_code == 0
    # stdout must be valid JSON (and only JSON) so `| jq` works.
    assert json.loads(result.stdout) == {"subject": "x", "body_text": "y"}


def test_read_markdown_escapes_headers_and_fences_body(
    monkeypatch: pytest.MonkeyPatch,
    patch_decoder: None,
) -> None:
    def _fake_run(cmd: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        # markdown is built locally; we still ask gws for json under the hood.
        assert cmd[cmd.index("--format") + 1] == "json"
        return _fake_completed(json.dumps(_GWS_PAYLOAD))

    monkeypatch.setattr(url_cli.subprocess, "run", _fake_run)
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["gmail", "url", "decode", _FAKE_URL, "--read", "--format", "markdown"],
    )
    assert result.exit_code == 0
    out = result.stdout
    # Subject metacharacters are escaped so the # doesn't end the H1 prematurely
    # and the [world]/*bold* don't render as a link/italics.
    assert "Hello \\[world\\] \\*bold\\*" in out
    # Email signature separator `-- ` must not render as a setext underline or
    # horizontal rule — body sits inside a fenced code block.
    assert "```text" in out
    assert "# Not a heading" in out  # body content preserved verbatim
    assert "-- " in out


def test_read_markdown_fence_grows_past_backticks_in_body(
    monkeypatch: pytest.MonkeyPatch,
    patch_decoder: None,
) -> None:
    body_with_fence = "before\n```\ninside\n```\nafter"
    payload = {**_GWS_PAYLOAD, "body_text": body_with_fence}

    def _fake_run(_cmd: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        return _fake_completed(json.dumps(payload))

    monkeypatch.setattr(url_cli.subprocess, "run", _fake_run)
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["gmail", "url", "decode", _FAKE_URL, "--read", "--format", "markdown"],
    )
    assert result.exit_code == 0
    # A 3-backtick fence would be closed by the body's own ``` block. Use 4+.
    assert "````text" in result.stdout


def test_read_markdown_non_json_stdout_exits_non_zero(
    monkeypatch: pytest.MonkeyPatch,
    patch_decoder: None,
) -> None:
    def _fake_run(_cmd: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        return _fake_completed("oops not json", returncode=0)

    monkeypatch.setattr(url_cli.subprocess, "run", _fake_run)
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["gmail", "url", "decode", _FAKE_URL, "--read", "--format", "markdown"],
    )
    # The pre-fix behavior was an uncaught JSONDecodeError + traceback. Now we
    # exit 1 with a human-readable error and the raw stdout on stderr.
    assert result.exit_code == 1
    assert "non-JSON" in result.stderr
    assert "oops not json" in result.stderr


def test_read_text_html_passes_through_to_gws(
    monkeypatch: pytest.MonkeyPatch,
    patch_decoder: None,
) -> None:
    captured: list[list[str]] = []

    def _fake_run(cmd: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        captured.append(cmd)
        return _fake_completed("<p>html body</p>")

    monkeypatch.setattr(url_cli.subprocess, "run", _fake_run)
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["gmail", "url", "decode", _FAKE_URL, "--read", "--html"],
    )
    assert result.exit_code == 0
    assert "--html" in captured[0]


@pytest.mark.parametrize("fmt", ["json", "markdown"])
def test_read_html_rejected_in_non_text_modes(
    monkeypatch: pytest.MonkeyPatch,
    patch_decoder: None,
    fmt: str,
) -> None:
    # Should fail BEFORE invoking gws so callers don't think they got HTML back.
    def _explode(*_args: Any, **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise AssertionError("gws should not be invoked when --html is rejected")

    monkeypatch.setattr(url_cli.subprocess, "run", _explode)
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["gmail", "url", "decode", _FAKE_URL, "--read", "--format", fmt, "--html"],
    )
    assert result.exit_code == 2
    assert "--html only applies" in result.stderr


def test_read_markdown_message_id_with_backticks_stays_intact(
    monkeypatch: pytest.MonkeyPatch,
    patch_decoder: None,
) -> None:
    # RFC 5322 Message-IDs can legally contain almost anything (e.g. a quoted
    # local part with backticks). The renderer must grow the inline-code
    # delimiter so the span isn't terminated mid-ID.
    nasty_id = "weird`id`@example.com"
    payload = {**_GWS_PAYLOAD, "message_id": nasty_id}

    def _fake_run(_cmd: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        return _fake_completed(json.dumps(payload))

    monkeypatch.setattr(url_cli.subprocess, "run", _fake_run)
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["gmail", "url", "decode", _FAKE_URL, "--read", "--format", "markdown"],
    )
    assert result.exit_code == 0
    # The full ID survives verbatim inside the inline code span.
    assert nasty_id in result.stdout
    # The opening delimiter must be at least 2 backticks long (the ID contains
    # a single backtick run of length 1, so 1+1=2).
    assert "``" in result.stdout


def test_read_markdown_falls_back_to_html_when_body_text_empty(
    monkeypatch: pytest.MonkeyPatch,
    patch_decoder: None,
) -> None:
    payload = {**_GWS_PAYLOAD, "body_text": "", "body_html": "<p>Hello *world*</p>"}

    def _fake_run(_cmd: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        return _fake_completed(json.dumps(payload))

    monkeypatch.setattr(url_cli.subprocess, "run", _fake_run)
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["gmail", "url", "decode", _FAKE_URL, "--read", "--format", "markdown"],
    )
    assert result.exit_code == 0
    # HTML body is preserved verbatim inside an html-tagged fence so the
    # content doesn't silently vanish for HTML-only messages.
    assert "```html" in result.stdout
    assert "<p>Hello *world*</p>" in result.stdout


def test_read_gws_failure_propagates_stderr_and_exit_code(
    monkeypatch: pytest.MonkeyPatch,
    patch_decoder: None,
) -> None:
    # gws can emit a partial body on stdout alongside a non-zero exit. The
    # CLI must forward that to stderr (not stdout) so jq pipes don't ingest
    # garbage but operators can still see what gws returned.
    def _fake_run(_cmd: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        return _fake_completed(
            "partial body fragment\n",
            returncode=2,
            stderr="gws: not found\n",
        )

    monkeypatch.setattr(url_cli.subprocess, "run", _fake_run)
    runner = CliRunner()
    result = runner.invoke(app, ["gmail", "url", "decode", _FAKE_URL, "--read"])
    assert result.exit_code == 2
    assert "gws: not found" in result.stderr
    assert "partial body fragment" in result.stderr
    # stdout stays clean so `... --format json | jq` doesn't choke on partials.
    assert "partial body fragment" not in result.stdout


def test_read_without_gws_on_path_fails_cleanly(
    monkeypatch: pytest.MonkeyPatch,
    patch_decoder: None,
) -> None:
    def _missing(_binary: str) -> str | None:
        return None

    monkeypatch.setattr(url_cli.shutil, "which", _missing)
    runner = CliRunner()
    result = runner.invoke(app, ["gmail", "url", "decode", _FAKE_URL, "--read"])
    assert result.exit_code == 1
    assert "gws not found" in result.stderr


def test_help_documents_new_options() -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["gmail", "url", "decode", "--help"])
    assert result.exit_code == 0
    for flag in ("--read", "--format", "--html", "--headers"):
        assert flag in result.stdout
