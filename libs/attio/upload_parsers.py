from typing import Any
from urllib.parse import urlparse


def parse_email_addresses(row: dict[str, Any]) -> list[str] | None:
    """Parse email addresses from row data."""
    email_addresses: list[str] = []
    email_address_01: Any = row.get("email_address_01")
    email_address_02: Any = row.get("email_address_02")

    match (bool(email_address_01), bool(email_address_02)):
        case (True, _):
            email_addresses.append(email_address_01)

        case (False, True):
            email_addresses.append(email_address_02)

        case _:
            pass

    return email_addresses if email_addresses else None


def parse_name(row: dict[str, Any]) -> list[dict[str, str]] | None:
    """Parse name structure from row data."""
    first_name: Any = row.get("first_name")
    last_name: Any = row.get("last_name")

    if not first_name and not last_name:
        return None

    name_data: dict[str, str] = {}
    if first_name is not None:
        name_data.update({"first_name": first_name or ""})

    if last_name is not None:
        name_data.update({"last_name": last_name or ""})

    # Create full name
    full_name_parts: list[str] = []
    if name_data.get("first_name"):
        full_name_parts.append(name_data.get("first_name", ""))

    if name_data.get("last_name"):
        full_name_parts.append(name_data.get("last_name", ""))

    name_data.update(
        {"full_name": " ".join(full_name_parts) if full_name_parts else ""},
    )

    return [name_data]


def parse_description(row: dict[str, Any]) -> list[str] | None:
    """Parse description from row data."""
    return None


def parse_company(row: dict[str, Any]) -> list[dict[str, Any]] | None:
    """Parse company structure from row data."""
    company_domain: Any = row.get("company_domain")

    if company_domain:
        company_data: dict[str, Any] = {
            "target_object": "companies",
            "domains": [{"domain": company_domain}],
        }
        return [company_data]

    return None


def parse_phone_numbers(row: dict[str, Any]) -> list[dict[str, str]] | None:
    """Parse phone numbers with country code detection from row data."""
    phone_number_01: Any = row.get("phone_number_01")

    if not phone_number_01:
        return None

    phone_data: dict[str, str] = {"original_phone_number": phone_number_01}

    # Try to detect country code - default to US
    phone_str: str = str(phone_number_01)
    match True:
        case _ if phone_str.startswith(("+1", "1")):
            phone_data.update({"country_code": "US"})

        case _ if phone_str.startswith("+44"):
            phone_data.update({"country_code": "GB"})

        case _ if phone_str.startswith("+33"):
            phone_data.update({"country_code": "FR"})

        case _:
            phone_data.update({"country_code": "US"})  # Default to US

    return [phone_data]


def parse_primary_location(row: dict[str, Any]) -> list[dict[str, Any]] | None:
    """Parse primary location from row data."""
    location_name: Any = row.get("Location Name")

    if not location_name:
        return None

    location_parts: list[str] = str(location_name).split(",")
    location_data: dict[str, Any] = {
        "line_1": location_parts[0].strip() if len(location_parts) > 0 else None,
        "line_2": None,
        "line_3": None,
        "line_4": None,
        "locality": location_parts[1].strip() if len(location_parts) > 1 else None,
        "region": location_parts[2].strip() if len(location_parts) > 2 else None,
        "postcode": None,
        "country_code": "US",
        "latitude": None,
        "longitude": None,
    }
    return [location_data]


def parse_linkedin(row: dict[str, Any]) -> list[str] | None:
    """Parse LinkedIn URL from row data."""
    linkedin_url: Any = row.get("linkedin")

    if not linkedin_url:
        return None

    # Ensure it's a proper LinkedIn URL
    if not linkedin_url.startswith("http"):
        linkedin_url = f"https://linkedin.com/in/{linkedin_url}"

    return [linkedin_url]


def parse_connected_on(row: dict[str, Any]) -> list[str] | None:
    """Parse Connected On date from row data."""
    connected_on: Any = row.get("Connected On")
    return [str(connected_on)] if connected_on else None


def parse_connections(row: dict[str, Any]) -> list[int] | None:
    """Parse Connections count from row data."""
    connections: Any = row.get("Connections")
    if not connections:
        return None

    try:
        return [int(connections)]
    except (ValueError, TypeError):
        return None


def parse_num_followers(row: dict[str, Any]) -> list[int] | None:
    """Parse Num Followers count from row data."""
    num_followers: Any = row.get("Num Followers")
    if not num_followers:
        return None

    try:
        return [int(num_followers)]
    except (ValueError, TypeError):
        return None


def parse_company_domain(row: dict[str, Any]) -> list[dict[str, Any]] | None:
    """Parse company_domain from row data and format as associated_company record reference."""
    company_domain: Any = row.get("company_domain")
    company_domain_latest: Any = row.get("Company Domain - Latest Experience")

    domains_list: list[dict[str, str]] = []

    # Add primary domain (LinkedIn-based) first if it exists
    if company_domain:
        domains_list.append({"domain": str(company_domain)})

    # Add secondary domain (latest experience) if it exists and is different
    if company_domain_latest and str(company_domain_latest) != str(company_domain):
        domains_list.append({"domain": str(company_domain_latest)})

    if domains_list:
        return [
            {
                "domains": domains_list,
                "target_object": "companies",
            },
        ]

    return None


def parse_company_domains_raw(
    row: dict[str, Any],
) -> tuple[str | None, str | None]:
    """Parse both company domains from row data and return as raw strings."""
    company_domain: Any = row.get("company_domain")
    company_domain_latest: Any = row.get("Company Domain - Latest Experience")

    primary: str | None = str(company_domain) if company_domain else None
    secondary: str | None = (
        str(company_domain_latest) if company_domain_latest else None
    )

    return (primary, secondary)


def is_linkedin_url(
    domain: str | None,
) -> bool:
    """Return True iff ``domain`` points at the linkedin.com host over http(s).

    Accepts bare hosts (``linkedin.com/in/foo``), http/https URLs
    (``https://www.linkedin.com/...``), and mixed case. Rejects:

    - host lookalikes (``evil-linkedin.com``, ``linkedin.com.attacker.tld``);
    - non-web schemes (``ftp://``, ``javascript://``, ``mailto:``, ``file://``,
      ``data://``) even when the embedded host string is linkedin.com;
    - userinfo-style inputs (``foo@linkedin.com`` or
      ``https://foo@linkedin.com/...``), which urlparse would otherwise
      happily resolve to a linkedin.com hostname despite being email
      addresses or phishing-shaped URLs.

    Implementation note: the leading-dot suffix check on ``.linkedin.com`` is
    what closes CodeQL ``py/incomplete-url-substring-sanitization`` — a bare
    ``"linkedin.com" in host`` or ``host.startswith("linkedin.com")`` shape is
    bypassable and will be flagged again. The scheme allowlist and userinfo
    rejection close adjacent bypasses (scheme confusion and embedded
    userinfo) in the same check.
    """
    if not domain:
        return False

    raw: str = str(domain).strip().lower()
    if not raw:
        return False

    # urlparse needs either a scheme (`http://...`) or a leading `//`
    # (protocol-relative) to populate `netloc`/`hostname`. Normalize before
    # parsing so we don't (a) re-prepend `//` onto an already protocol-
    # relative URL and double-`//` the netloc, or (b) let bare-host inputs
    # with a port (``linkedin.com:443/in/foo``) get parsed as
    # ``scheme=linkedin.com``.
    if raw.startswith("//") or "://" in raw:
        candidate: str = raw
    else:
        candidate = f"//{raw}"

    parsed = urlparse(candidate)

    # Restrict to bare-host (no scheme) or explicit http(s). This rejects
    # ftp/javascript/file/data/mailto and any other non-web scheme that
    # happens to embed "linkedin.com".
    if parsed.scheme and parsed.scheme not in ("http", "https"):
        return False

    # Reject userinfo-style inputs. `foo@linkedin.com` is an email address,
    # not a LinkedIn URL — urlparse would otherwise return linkedin.com as
    # the hostname.
    if parsed.username or parsed.password:
        return False

    # Reject malformed authorities like `linkedin.com:443.evil.tld`, where
    # urlparse will happily return `hostname == "linkedin.com"` and a
    # non-numeric port. Accessing `parsed.port` validates the port and
    # raises ValueError on anything that isn't a numeric port in range.
    try:
        _ = parsed.port
    except ValueError:
        return False

    host: str | None = parsed.hostname
    if not host:
        return False

    return host == "linkedin.com" or host.endswith(".linkedin.com")


def parse_school(row: dict[str, Any]) -> list[str] | None:
    """Parse school from row data."""
    school: Any = row.get("school")
    return [str(school)] if school else None


def parse_school_major(row: dict[str, Any]) -> list[str] | None:
    """Parse school_major from row data."""
    school_major: Any = row.get("school_major")
    return [str(school_major)] if school_major else None


def parse_graduation_date(row: dict[str, Any]) -> list[str] | None:
    """Parse graduation_date from row data."""
    graduation_date: Any = row.get("graduation_date")
    return [str(graduation_date)] if graduation_date else None


def parse_class_year(row: dict[str, Any]) -> list[int] | None:
    """Parse class_year from row data."""
    class_year: Any = row.get("class_year")
    if not class_year:
        return None

    try:
        return [int(class_year)]
    except (ValueError, TypeError):
        return None


def parse_birth_date(row: dict[str, Any]) -> list[str] | None:
    """Parse birth_date from row data."""
    birth_date: Any = row.get("birth_date")
    return [str(birth_date)] if birth_date else None


def parse_connected_where(row: dict[str, Any]) -> list[str] | None:
    """Parse connected_where from row data."""
    connected_where: Any = row.get("connected_where")
    return [str(connected_where)] if connected_where else None
