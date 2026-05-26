import pytest

from libs.parsers.contacts import parse_multiple_email_splitter_and_domain_filter


@pytest.mark.parametrize(
    ("email", "domains", "expected"),
    [
        # Gmail preferred when multiple allowed-domain emails are present.
        (
            "foo@company.com, bar@gmail.com",
            ["company.com", "gmail.com"],
            "bar@gmail.com",
        ),
        # Semicolon separator is normalized to comma.
        (
            "foo@company.com; bar@gmail.com",
            ["company.com", "gmail.com"],
            "bar@gmail.com",
        ),
        # First matching gmail wins among multiple gmails.
        (
            "a@gmail.com, b@gmail.com",
            ["gmail.com"],
            "a@gmail.com",
        ),
        # No gmail, fall back to first allowed-domain email.
        (
            "foo@company.com, bar@other.com",
            ["company.com", "other.com"],
            "foo@company.com",
        ),
        # Allowlist drops everything → empty.
        (
            "foo@company.com",
            ["other.com"],
            "",
        ),
        # Empty input → empty.
        ("", ["gmail.com"], ""),
        # Case is preserved in match but result lowercased.
        (
            "Foo@Gmail.com",
            ["gmail.com"],
            "foo@gmail.com",
        ),
        # Bypass attempt: local-part contains "gmail.com" but real host is
        # the allowed evil.tld. The result is the verbatim allowed address,
        # NOT auto-preferred as a gmail. Confirms the new "@gmail.com"
        # boundary is anchored on the local-part separator.
        (
            "gmail.com.attacker@evil.tld, real@evil.tld",
            ["evil.tld"],
            "gmail.com.attacker@evil.tld",
        ),
    ],
)
def test_parse_multiple_email_splitter_and_domain_filter(
    email: str,
    domains: list[str],
    expected: str,
) -> None:
    assert parse_multiple_email_splitter_and_domain_filter(email, domains) == expected
