import re
from typing import Any

import narwhals as nw

from libs.parsers.contacts import parse_clean_name_remove_credentials


def _clean_name_pair(
    first: str | None,
    last: str | None,
) -> tuple[str | None, str | None]:
    """Strip credentials and rebalance first/last name parts.

    Mirrors the prior polars expression: if the last-name column carries
    multiple tokens, the final token becomes the last name and the leading
    tokens fold back into the first name. If the last-name column is empty
    and the first-name column carries multiple tokens, the final first-name
    token migrates to last name.
    """
    clean_first = parse_clean_name_remove_credentials(first) if first else None
    clean_last = parse_clean_name_remove_credentials(last) if last else None

    last_parts = clean_last.split(" ") if clean_last else []
    first_parts = clean_first.split(" ") if clean_first else []

    if len(last_parts) > 1:
        new_last: str | None = last_parts[-1]
        new_first: str | None = (
            f"{clean_first or ''} {' '.join(last_parts[:-1])}".strip()
        )
    elif len(last_parts) == 1:
        new_last = last_parts[-1]
        new_first = clean_first
    elif len(first_parts) > 1:
        new_last = first_parts[-1]
        new_first = " ".join(first_parts[:-1])
    else:
        new_last = None
        new_first = clean_first

    return new_first, new_last


@nw.narwhalify
def df_clean_names(
    df: nw.DataFrame[Any],
    first_name_col: str,
    last_name_col: str,
) -> nw.DataFrame[Any]:
    """Clean first and last names in any dataframe."""
    if {first_name_col, last_name_col} - set(df.columns):
        return df

    firsts = df.get_column(first_name_col).to_list()
    lasts = df.get_column(last_name_col).to_list()

    new_firsts: list[str | None] = []
    new_lasts: list[str | None] = []

    for first_val, last_val in zip(firsts, lasts, strict=True):
        nf, nl = _clean_name_pair(first_val, last_val)
        new_firsts.append(nf)
        new_lasts.append(nl)

    patch = nw.from_dict(
        {
            first_name_col: new_firsts,
            last_name_col: new_lasts,
        },
        backend=nw.get_native_namespace(df),
    )

    return df.with_columns(
        patch.get_column(first_name_col).cast(nw.String),
        patch.get_column(last_name_col).cast(nw.String),
    )


def parse_pomona_class_year(
    school_name: str | None,
    end_date: str | None,
) -> str | None:
    """Extract class year from end date for Pomona College alumni."""
    if not school_name or not end_date:
        return None

    if "Pomona College" not in str(school_name):
        return None

    year_match = re.search(r"\b(19|20)\d{2}\b", str(end_date))
    match year_match:
        case None:
            return None

        case _:
            return year_match.group(0)


@nw.narwhalify
def parse_clay_names(df: nw.DataFrame[Any]) -> nw.DataFrame[Any]:
    """Clean first and last names in Clay data."""
    match (
        "First Name (2)" in df.columns and "Last Name (2)" in df.columns,
        "First Name" in df.columns and "Last Name" in df.columns,
    ):
        case (True, _):
            return df_clean_names(df, "First Name (2)", "Last Name (2)")

        case (False, True):
            return df_clean_names(df, "First Name", "Last Name")

        case _:
            return df


@nw.narwhalify
def parse_pomona_class_years(df: nw.DataFrame[Any]) -> nw.DataFrame[Any]:
    """Extract class years for Pomona College alumni from Clay data."""
    if (
        "School Name - Education" not in df.columns
        or "End Date - Education" not in df.columns
    ):
        return df.with_columns(nw.lit(None).cast(nw.String).alias("Class Year"))

    schools = df.get_column("School Name - Education").to_list()
    end_dates = df.get_column("End Date - Education").to_list()

    class_years: list[str | None] = [
        parse_pomona_class_year(school, end_date)
        for school, end_date in zip(schools, end_dates, strict=True)
    ]

    patch = nw.from_dict(
        {"Class Year": class_years},
        backend=nw.get_native_namespace(df),
    )

    return df.with_columns(patch.get_column("Class Year").cast(nw.String))
