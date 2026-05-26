import re
from collections.abc import Callable
from re import Match, Pattern
from typing import Any

import emoji
import phonenumbers
from phonenumbers import PhoneNumber, PhoneNumberFormat


def parse_name_case(
    name: Any,
) -> str | None:
    if name is None or not isinstance(name, str) or not name.strip():
        return None

    # Remove emojis
    name = emoji.replace_emoji(name, replace="").strip(" ,")
    if not name:
        return None

    # Remove Dr. or Dr prefix (case-insensitive) from individual name fields
    name = re.sub(r"^Dr\.?\s*", "", name, flags=re.IGNORECASE).strip()
    if not name:
        return None

    def title_if_uniform(word):
        if word.islower() or word.isupper():
            return word.title()

        return word

    return " ".join(title_if_uniform(word) for word in name.strip(" ,").split())


def parse_last_name_if_null_from_first_name(
    first_name: Any,
) -> tuple[str | None, str | None]:
    if first_name is None or not isinstance(first_name, str):
        return None, None

    # Remove empty parentheses and any surrounding whitespace
    first_name = first_name.replace("()", "").strip()
    parts: list[str] = first_name.split()
    if len(parts) > 1:
        last_name: str = parts[-1]
        first_name_new: str = " ".join(parts[:-1])
        return first_name_new, last_name

    return first_name, None


def parse_middle_name_if_null_from_first_name(
    first_name: Any,
) -> tuple[str | None, str | None]:
    if first_name is None or not isinstance(first_name, str):
        return None, None

    parts: list[str] = first_name.strip().split()
    if not parts:
        return None, None

    extracted_first_name: str = parts[0]
    middle_name: str | None = " ".join(parts[1:]) if len(parts) > 1 else None
    return extracted_first_name, middle_name


def parse_first_middle_and_last_name(
    full_name: Any,
) -> tuple[str | None, str | None, str | None]:
    if full_name is None or not isinstance(full_name, str):
        return None, None, None

    def full_name_splitter(full_name: str) -> tuple[str | None, str | None, str | None]:
        first_name: str | None = None
        middle_name: str | None = None
        last_name: str | None = None

        full_name = re.sub(r"\s+", " ", full_name).strip()
        # Remove Dr. or Dr prefix (case-insensitive) before parsing
        full_name = re.sub(r"^Dr\.?\s*", "", full_name, flags=re.IGNORECASE).strip()
        # Remove empty parentheses and any surrounding whitespace
        full_name = full_name.replace("()", "").strip()
        pattern1: Pattern[str] = re.compile(r"^([^,]+),\s*([^\s]+)(?:\s+([^\s.]+))?")
        pattern2: Pattern[str] = re.compile(r"^([^\s]+)\s+([^\s.]+)(?:\s+([^\s.]+))?$")
        match: Match[str] | None = pattern1.match(full_name)
        if match:
            groups: list[str | None] = list(match.groups())
            last_name = groups[0]
            first_name = groups[1]
            middle_name = groups[2]

        else:
            match: Match[str] | None = pattern2.match(full_name)
            if match and len(match.groups()) >= 2:
                groups: list[str | None] = list(match.groups())
                first_name = groups[0]
                middle_last: list[str | None] = groups[1:]

                match len(middle_last) > 1:
                    case True:
                        match middle_last[1] is None:
                            case True:
                                middle_name = None
                                last_name = middle_last[0]

                            case False:
                                middle_name = middle_last[0]
                                last_name = middle_last[1]

                    case False:
                        match bool(middle_last):
                            case True:
                                middle_name = middle_last[0]
                                last_name = None

                            case False:
                                pass

            else:
                return full_name, None, None

        return (
            parse_name_case(first_name),
            parse_name_case(middle_name),
            parse_name_case(last_name),
        )

    first_name: str | None
    middle_name: str | None
    last_name: str | None
    first_name, middle_name, last_name = full_name_splitter(full_name=full_name)
    if last_name is None:
        first_name, last_name = parse_last_name_if_null_from_first_name(first_name)

    return first_name, middle_name, last_name


def parse_year(
    year: Any,
) -> int | None:
    def extract_first_digit(text: str) -> int | None:
        match: Match[str] | None = re.search(r"\d+", str(text))
        return int(match.group(0)) if match else None

    if year is None:
        return None

    # If year is already an integer, return it directly
    if isinstance(year, int):
        match year < 1900:
            case True:
                match year <= 30:
                    case True:
                        return 2000 + year

                    case False:
                        return 1900 + year

            case False:
                return year

    # Handle string year
    if not isinstance(year, str):
        return None

    year = str(year).strip().strip("'\"")
    if not year:
        return None

    # Try to extract first digit sequence if it's a string
    year_num: int | None = extract_first_digit(year)
    if year_num is None:
        return None

    match year_num < 1900:
        case True:
            match year_num <= 30:
                case True:
                    return 2000 + year_num

                case False:
                    return 1900 + year_num

        case False:
            pass

    return year_num


def parse_birthday(
    birthday: Any,
) -> str | None:
    if not birthday or not isinstance(birthday, str):
        return None

    birthday = birthday.strip()
    birthday = birthday.strip("\"'")
    if not birthday:
        return None

    return birthday


def parse_source(
    source: Any,
) -> str | None:
    if not source or not isinstance(source, str):
        return None

    source = source.strip().strip("\"'")
    if not source:
        return None

    replacement_rules: list[tuple[Callable[[str], bool], str]] = [
        (lambda s: s.lower().startswith("pom"), "Pomona"),
        (lambda s: s.lower().startswith("berkeley"), "Berkeley"),
        (lambda s: s.lower().startswith("epic"), "Epic"),
        (lambda s: s.lower().startswith("qb"), "QuestBridge"),
        (lambda s: s.lower().startswith("questbridge"), "QuestBridge"),
        (lambda s: s.lower().startswith("mlt"), "MLT"),
        (lambda s: s.lower().startswith("reality"), "Reality"),
    ]
    condition: Callable[[str], bool]
    replacement: str
    for condition, replacement in replacement_rules:
        if condition(source):
            return replacement.strip()

    return source


def parse_multiple_email_splitter_and_domain_filter(
    email: str,
    email_domains_to_keep: list[str],
) -> str:
    if not email:
        return ""

    emails: list[str] = [
        e.strip() for e in email.replace(";", ",").split(",") if e.strip()
    ]
    if not emails:
        return ""

    filtered_emails: list[str] = [
        e
        for e in emails
        if any(e.lower().endswith(f"@{domain}") for domain in email_domains_to_keep)
    ]
    if not filtered_emails:
        return ""

    emails_to_consider: list[str] = filtered_emails
    # Anchor on "@gmail.com" rather than a substring match so lookalikes such as
    # "gmail.com.attacker@evil.tld" do not get auto-preferred. CodeQL flags any
    # `"gmail.com" in e` shape as py/incomplete-url-substring-sanitization.
    gmail_emails: list[str] = [
        e for e in emails_to_consider if e.lower().endswith("@gmail.com")
    ]
    return (gmail_emails[0] if gmail_emails else emails_to_consider[0]).lower()


def parse_phone(
    phone: Any,
    default_region: str = "US",
) -> str:
    if not phone or not isinstance(phone, str):
        return ""

    numbers: list[str] = [n.strip() for n in phone.split(",") if n.strip()]
    if not numbers:
        return ""

    parsed_numbers: set[str] = set()
    for number in numbers:
        try:
            parsed: PhoneNumber = phonenumbers.parse(number, default_region)
            if phonenumbers.is_valid_number(parsed):
                e164: str = phonenumbers.format_number(parsed, PhoneNumberFormat.E164)
                parsed_numbers.add(e164)

        except (phonenumbers.phonenumberutil.NumberParseException, TypeError):
            continue

    return next(iter(parsed_numbers), "")


CREDENTIALS_TO_REMOVE: list[str] = [
    "MBA",
    "PhD",
    "Ph.D.",
    "MD",
    "PharmD",
    "PharmD,",
    "M.A.",
    "CBCP",
    "CPA",
    "JD",
    "J.D.",
    "M.D.",
    "DDS",
    "DO",
    "RN",
    "BSN",
    "MSN",
    "NP",
    "PA",
    "EdD",
    "Ed.D.",
    "PsyD",
    "Psy.D.",
    "DPT",
    "OTR",
    "LCSW",
    "LPC",
    "LMFT",
    "PMP",
    "CFA",
    "CRM",
]

ABBREVIATIONS_TO_REMOVE: list[str] = [
    "Jr",
    "Jr.",
    "Sr",
    "Sr.",
    "II",
    "III",
    "IV",
    "V",
    "VI",
    "2nd",
    "3rd",
    "4th",
    "5th",
    "6th",
    "Mr",
    "Mr.",
    "Mrs",
    "Mrs.",
    "Ms",
    "Ms.",
    "Dr",
    "Dr.",
    "Prof",
    "Prof.",
    "c/NDT",
    "A.M",
    "ACC",
    "ACCOUNT",
    "ACAS",
    "ACIS",
    "AIA",
    "AICP",
    "AIGP",
    "AP",
    "APTD",
    "ASA",
    "M.A",
    "M.B.A",
    "M.D",
    "M.Ed",
    "M.Eng",
    "M.S",
    "M.S.Ed",
    "M.Sc",
    "MA",
    "MAAA",
    "MEd",
    "MHA",
    "MLT",
    "MPA",
    "MPH",
    "MPM",
    "MPhil",
    "MS",
    "MSBA",
    "MSECE",
    "MSEd",
    "MSF",
    "MSHR",
    "MSP",
    "MSPH",
    "MSW",
    "MSc",
    "MSc.A",
    "SP",
    "SPHR",
    "SSCP",
    "SHRM-SCP",
    "Soccer",
    "Social",
    "==",
]


def parse_clean_name_remove_credentials(name: str | None) -> str | None:
    """Remove credentials and abbreviations from a name and return cleaned name."""
    if not name:
        return name

    name = name.strip()
    if not name:
        return None

    # Remove emojis
    name = emoji.replace_emoji(name, replace="").strip(" ,")
    if not name:
        return None

    name = re.sub(r"\([^)]*\)", "", name).strip()
    parts: list[str] = [part.strip() for part in name.replace(",", " ").split()]

    cleaned_parts: list[str] = []
    for i, part in enumerate(parts):
        part = part.strip(" ,.")
        if part and part not in CREDENTIALS_TO_REMOVE:
            should_remove_abbreviation: bool = (
                part in ABBREVIATIONS_TO_REMOVE and len(parts) > 1 and i > 0
            )
            match should_remove_abbreviation:
                case True:
                    pass

                case False:
                    cleaned_parts.append(part)

    return " ".join(cleaned_parts) if cleaned_parts else None
