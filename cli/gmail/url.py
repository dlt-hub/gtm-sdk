"""Decode Gmail web URLs to API-usable message/thread IDs."""

from __future__ import annotations

import json
import shutil
import subprocess
from enum import Enum
from typing import Any

import typer

from libs.gmail.url_decoder import decode_token, extract_id_from_url

app = typer.Typer(help="Gmail URL decoding.")


class ReadFormat(str, Enum):
    text = "text"
    json = "json"
    markdown = "markdown"


# Characters that markdown renderers interpret. Backslash-escape them in header
# values so addresses like <user@example.com>, names like "Foo*Bar", or subjects
# containing #/_/[]/!/-/| don't render as headings, italics, links, etc.
_MD_ESCAPE: dict[str, str] = {c: f"\\{c}" for c in r"\`*_{}[]()#+-.!|<>"}


def _escape_md(value: str) -> str:
    return value.translate(str.maketrans(_MD_ESCAPE))


def _format_addr(addr: dict[str, Any] | None) -> str:
    if not addr:
        return ""
    name = addr.get("name")
    email = addr.get("email") or ""
    return f"{name} <{email}>" if name else email


def _format_addr_list(addrs: list[dict[str, Any]] | None) -> str:
    if not addrs:
        return ""
    return ", ".join(_format_addr(a) for a in addrs)


def _longest_backtick_run(value: str) -> int:
    longest = 0
    run = 0
    for ch in value:
        if ch == "`":
            run += 1
            longest = max(longest, run)
        else:
            run = 0
    return longest


def _fence_body(body: str, lang: str = "text") -> str:
    # Pick a fence longer than any run of backticks inside the body so the
    # fence can't be closed early by content that happens to contain ```.
    fence = "`" * max(3, _longest_backtick_run(body) + 1)
    return f"{fence}{lang}\n{body}\n{fence}"


def _inline_code(value: str) -> str:
    # Inline code spans must use a backtick run longer than any inside `value`
    # and pad with spaces when `value` starts/ends with a backtick. This keeps
    # IDs like Message-ID containing backticks from terminating the span early.
    n = _longest_backtick_run(value) + 1
    ticks = "`" * n
    padded = value
    if value.startswith("`"):
        padded = f" {padded}"
    if value.endswith("`"):
        padded = f"{padded} "
    return f"{ticks}{padded}{ticks}"


def _build_gws_read_cmd(
    *,
    gws_path: str,
    hex_id: str,
    format_: ReadFormat,
    html: bool,
    text_headers: bool,
) -> list[str]:
    """Build the `gws gmail +read` argv for a given output mode.

    The contract with gws differs by output mode:

    - text:     follow the user's intent literally. --headers only if requested,
                --html only if requested. Output is human-readable plain/HTML body.
    - json:     ALWAYS request --headers because the json response shape from
                `gws gmail +read --format json` only carries `from/to/cc/subject/date`
                when --headers is set. Callers piping to jq expect those fields.
    - markdown: request --format json + --headers for the same reason: markdown
                rendering reads the parsed header fields and the body.

    Caller MUST have already rejected --html + non-text modes (handled in `decode()`).
    """
    if format_ is ReadFormat.text:
        cmd = [gws_path, "gmail", "+read", "--id", hex_id, "--format", "text"]
        if text_headers:
            cmd.append("--headers")
        if html:
            cmd.append("--html")
        return cmd

    # json and markdown both need the parsed-header JSON payload from gws.
    return [gws_path, "gmail", "+read", "--id", hex_id, "--format", "json", "--headers"]


def _render_markdown(msg: dict[str, Any]) -> str:
    subject = msg.get("subject") or "(no subject)"
    lines = [f"# {_escape_md(subject)}", ""]
    if from_ := msg.get("from"):
        lines.append(f"**From:** {_escape_md(_format_addr(from_))}")
    if to := msg.get("to"):
        lines.append(f"**To:** {_escape_md(_format_addr_list(to))}")
    if cc := msg.get("cc"):
        lines.append(f"**Cc:** {_escape_md(_format_addr_list(cc))}")
    if date := msg.get("date"):
        lines.append(f"**Date:** {_escape_md(date)}")
    if thread_id := msg.get("thread_id"):
        lines.append(f"**Thread:** {_inline_code(thread_id)}")
    if message_id := msg.get("message_id"):
        lines.append(f"**Message-ID:** {_inline_code(message_id)}")
    # Prefer plain text; fall back to HTML for HTML-only messages so the body
    # isn't silently dropped. Mark the language so a renderer can syntax-color
    # the HTML if it wants.
    body = msg.get("body_text") or ""
    body_lang = "text"
    if not body.strip():
        html_body = msg.get("body_html") or ""
        if html_body.strip():
            body = html_body
            body_lang = "html"
    lines.extend(["", _fence_body(body, body_lang)])
    return "\n".join(lines)


@app.command()
def decode(
    url_or_token: str = typer.Argument(help="A Gmail web URL or FMfcg... token"),
    read: bool = typer.Option(
        False,
        "--read",
        "-r",
        help="Fetch the message via gws after decoding",
    ),
    format_: ReadFormat = typer.Option(
        ReadFormat.text,
        "--format",
        "-f",
        help="Output format when --read is set",
    ),
    html: bool = typer.Option(
        False,
        "--html",
        help="Return HTML body instead of plain text (text format only)",
    ),
    headers: bool = typer.Option(
        False,
        "--headers",
        "-H",
        help="Include parsed headers (text format only; json/markdown always carry them)",
    ),
) -> None:
    """Decode a Gmail URL or token to a hex API ID, optionally fetching the message."""
    if url_or_token.startswith("http"):
        hex_id = extract_id_from_url(url_or_token)
    else:
        hex_id = decode_token(url_or_token)

    if not hex_id:
        typer.echo("Could not decode the provided URL or token.", err=True)
        raise typer.Exit(1)

    if not read:
        typer.echo(hex_id)
        return

    # --html only makes sense for text mode (json carries body_html alongside
    # body_text; markdown renders body_html as a fallback when body_text is
    # empty). Reject the combination explicitly rather than silently dropping
    # the flag, which would mislead callers into thinking they got HTML.
    if html and format_ is not ReadFormat.text:
        typer.echo(
            "--html only applies to --format text "
            "(json includes body_html alongside body_text; "
            "markdown falls back to body_html when body_text is empty).",
            err=True,
        )
        raise typer.Exit(2)

    # When --read is set, route the hex ID to stderr so stdout stays a clean
    # text/json/markdown payload that can be piped to jq, a file, or another tool.
    typer.echo(hex_id, err=True)

    gws_path = shutil.which("gws")
    if not gws_path:
        typer.echo("gws not found on PATH.", err=True)
        raise typer.Exit(1)

    cmd = _build_gws_read_cmd(
        gws_path=gws_path,
        hex_id=hex_id,
        format_=format_,
        html=html,
        text_headers=headers,
    )
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        # Preserve gws stdout on failure — a non-zero exit doesn't mean an
        # empty payload (gws may emit a partial body or a structured error
        # alongside its diagnostic stderr). Route both to stderr so the exit
        # code stays meaningful and callers piping stdout to jq don't ingest
        # partial junk, while operators still see what gws actually returned.
        if result.stdout:
            typer.echo(result.stdout, err=True, nl=False)
        typer.echo(result.stderr, err=True)
        raise typer.Exit(result.returncode)

    if format_ is ReadFormat.markdown:
        try:
            msg = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            typer.echo(
                f"gws gmail +read returned non-JSON stdout (cannot render markdown): {exc}",
                err=True,
            )
            typer.echo(result.stdout, err=True)
            raise typer.Exit(1) from exc
        typer.echo(_render_markdown(msg))
    else:
        typer.echo(result.stdout, nl=False)
