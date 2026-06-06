"""Unit tests for the shared bootstrap helpers in scripts/lib/env.py.

These helpers are on the critical path for every repo script that
self-bootstraps `infisical run` (e.g. attio-inspect-meeting-relationship,
attio-probe_workspace_slug), so a regression in `parse_dotenv` or credential
resolution would silently break bootstrap across multiple commands. BD: ai-3hq.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.lib import env


def test_clean_env_strips_and_blanks_to_none() -> None:
    assert env.clean_env("  abc \n") == "abc"
    assert env.clean_env("   ") is None
    assert env.clean_env("") is None
    assert env.clean_env(None) is None


def test_parse_dotenv_handles_export_quotes_and_comments() -> None:
    text = "\n".join(
        [
            "# a comment",
            "",
            "export INFISICAL_PROJECT_ID=proj-123",
            'INFISICAL_TOKEN="st.tok.en"',
            "SINGLE='quoted value'",
            "UNQUOTED=bare # trailing comment",
            "NO_EQUALS_LINE",
        ],
    )
    parsed = env.parse_dotenv(text)
    assert parsed["INFISICAL_PROJECT_ID"] == "proj-123"
    assert parsed["INFISICAL_TOKEN"] == "st.tok.en"
    # Quoted values are kept verbatim; a `#` inside is not a comment.
    assert parsed["SINGLE"] == "quoted value"
    # Unquoted values strip an inline ` # comment`.
    assert parsed["UNQUOTED"] == "bare"
    assert "NO_EQUALS_LINE" not in parsed


def test_parse_dotenv_keeps_hash_inside_quoted_value() -> None:
    parsed = env.parse_dotenv('TOKEN="abc#def"')
    assert parsed["TOKEN"] == "abc#def"


def test_parse_dotenv_strips_comment_after_quoted_value() -> None:
    # `KEY="val" # comment` must yield `val`, not `"val"` (quotes intact would
    # break Infisical auth). The closing quote bounds the value; the rest drops.
    parsed = env.parse_dotenv('INFISICAL_TOKEN="st.tok.en" # generated 2026')
    assert parsed["INFISICAL_TOKEN"] == "st.tok.en"


def test_parse_dotenv_unterminated_quote_keeps_remainder() -> None:
    parsed = env.parse_dotenv('KEY="oops')
    assert parsed["KEY"] == "oops"


def test_read_infisical_credentials_prefers_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("INFISICAL_PROJECT_ID", "env-proj")
    monkeypatch.setenv("INFISICAL_TOKEN", "env-tok")
    assert env.read_infisical_credentials() == ("env-proj", "env-tok")


def test_read_infisical_credentials_falls_back_to_dotenv(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.delenv("INFISICAL_PROJECT_ID", raising=False)
    monkeypatch.delenv("INFISICAL_TOKEN", raising=False)
    env_file = tmp_path / ".env.local"
    env_file.write_text(
        'INFISICAL_PROJECT_ID="file-proj"\nINFISICAL_TOKEN=file-tok\n',
    )
    monkeypatch.setattr(env, "REPO_ROOT", tmp_path)
    assert env.read_infisical_credentials() == ("file-proj", "file-tok")


def test_read_infisical_credentials_does_not_mix_sources(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    # Only the project id is in the environment; the token is only in the file.
    # The pair must come wholly from the file — never one value from each source.
    monkeypatch.setenv("INFISICAL_PROJECT_ID", "env-proj")
    monkeypatch.delenv("INFISICAL_TOKEN", raising=False)
    env_file = tmp_path / ".env.local"
    env_file.write_text(
        "INFISICAL_PROJECT_ID=file-proj\nINFISICAL_TOKEN=file-tok\n",
    )
    monkeypatch.setattr(env, "REPO_ROOT", tmp_path)
    assert env.read_infisical_credentials() == ("file-proj", "file-tok")


def test_read_infisical_credentials_returns_none_when_absent(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.delenv("INFISICAL_PROJECT_ID", raising=False)
    monkeypatch.delenv("INFISICAL_TOKEN", raising=False)
    monkeypatch.setattr(env, "REPO_ROOT", tmp_path)  # empty dir, no .env.local
    assert env.read_infisical_credentials() is None


def test_infisical_run_example_includes_required_flags() -> None:
    example = env.infisical_run_example("scripts/foo.py")
    assert "--projectId" in example
    assert "--token" in example
    assert "--env=" in example
    assert "scripts/foo.py" in example
