import pytest

from libs.attio.upload_parsers import is_linkedin_url


@pytest.mark.parametrize(
    "value",
    [
        "linkedin.com",
        "linkedin.com/in/foo",
        "https://www.linkedin.com/in/foo",
        "http://linkedin.com",
        "LinkedIn.com",
        "https://uk.linkedin.com/in/foo",
        "  linkedin.com/in/foo  ",
        # Protocol-relative URL — sometimes pasted from <a href="//..."> tags.
        "//linkedin.com/in/foo",
        "//www.linkedin.com/in/foo",
        # Explicit port — both bare-host and schemed forms.
        "linkedin.com:443/in/foo",
        "https://linkedin.com:443/in/foo",
    ],
)
def test_is_linkedin_url_accepts_real_linkedin(value: str) -> None:
    assert is_linkedin_url(value) is True


@pytest.mark.parametrize(
    "value",
    [
        "evil-linkedin.com",
        "linkedin.com.attacker.tld",
        "https://evil.com/linkedin.com",
        "https://linkedin.com.attacker.tld/in/foo",
        "notlinkedin.com",
        "linkedincom",
        "not a url",
        "",
        "   ",
        None,
        # Scheme-confusion bypasses: host parses as linkedin.com but the
        # scheme is non-web, so the URL is unsafe to treat as a LinkedIn
        # link (could be exfiltrated to ftp/file, or executed as JS).
        "ftp://linkedin.com/in/foo",
        "javascript://linkedin.com/in/foo",
        "file://linkedin.com/in/foo",
        "data://linkedin.com/in/foo",
        "mailto:foo@linkedin.com",
        # Userinfo-style bypasses: urlparse would resolve hostname to
        # linkedin.com, but the input is an email address or a phishing
        # URL embedding userinfo, not a real LinkedIn link.
        "foo@linkedin.com",
        "https://foo@linkedin.com/in/bar",
        "https://foo:bar@linkedin.com/in/baz",
        # Malformed-authority bypass: urlparse returns
        # `hostname == "linkedin.com"` but the "port" is non-numeric, so the
        # real host is `linkedin.com:443.evil.tld` — not LinkedIn.
        "linkedin.com:443.evil.tld",
        "linkedin.com:notaport/in/foo",
        "https://linkedin.com:443.evil.tld/in/foo",
    ],
)
def test_is_linkedin_url_rejects_bypass_and_garbage(value: str | None) -> None:
    assert is_linkedin_url(value) is False
