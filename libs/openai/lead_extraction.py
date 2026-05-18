import json
import os
from pathlib import Path
from typing import Any

import narwhals as nw
import pyarrow as pa
import pyarrow.csv as pacsv
from pydantic import BaseModel, Field


class LeadInfo(BaseModel):
    """Structured output model for lead information extracted from description."""

    title: str | None = Field(
        default=None,
        description="Job title of the person",
    )
    company: str | None = Field(
        default=None,
        description="Company name",
    )
    location: str | None = Field(
        default=None,
        description="Location or city",
    )
    school: str | None = Field(
        default=None,
        description="School or university name",
    )


def extract_lead_info_from_description(
    description: str,
    openai_client: Any,
    model: str = "gpt-4o-mini",
) -> LeadInfo:
    """Extract structured lead information from a description string using OpenAI.

    Args:
        description: Text description that may contain title, company, location, school
        openai_client: OpenAI client instance
        model: OpenAI model to use for extraction

    Returns:
        LeadInfo object with extracted information (None for fields not found)
    """
    system_prompt: str = """You are an expert at extracting structured information from text descriptions.
Extract the following information if present in the description:
- title: Job title or position
- company: Company or organization name
- location: City, state, or geographic location
- school: Educational institution or university name

Return None for any field that is not clearly present in the description.
Be conservative - only extract information that is explicitly mentioned."""

    completion = openai_client.beta.chat.completions.parse(
        model=model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": f"Extract information from: {description}"},
        ],
        response_format=LeadInfo,
    )

    lead_info: LeadInfo | None = completion.choices[0].message.parsed

    if lead_info is None:
        return LeadInfo()

    return lead_info


@nw.narwhalify
def df_add_extracted_lead_info(
    df: nw.DataFrame[Any],
    description_column: str,
    openai_client: Any,
    model: str = "gpt-4o-mini",
) -> nw.DataFrame[Any]:
    """Add extracted lead information columns to DataFrame.

    Args:
        df: Input DataFrame with description column
        description_column: Name of column containing descriptions
        openai_client: OpenAI client instance
        model: OpenAI model to use for extraction

    Returns:
        DataFrame with additional columns: extracted_title, extracted_company,
        extracted_location, extracted_school
    """

    def process_row(description: str | None) -> dict[str, str | None]:
        if description is None or description.strip() == "":
            return {
                "extracted_title": None,
                "extracted_company": None,
                "extracted_location": None,
                "extracted_school": None,
            }

        try:
            lead_info: LeadInfo = extract_lead_info_from_description(
                description=description,
                openai_client=openai_client,
                model=model,
            )

            return {
                "extracted_title": lead_info.title,
                "extracted_company": lead_info.company,
                "extracted_location": lead_info.location,
                "extracted_school": lead_info.school,
            }

        except Exception as e:
            print(f"Error processing description: {e}")

            return {
                "extracted_title": None,
                "extracted_company": None,
                "extracted_location": None,
                "extracted_school": None,
            }

    descriptions: list[str | None] = df.get_column(description_column).to_list()
    extracted_data: list[dict[str, str | None]] = [
        process_row(desc) for desc in descriptions
    ]

    backend = nw.get_native_namespace(df)
    extracted_df = nw.from_dict(
        {
            "extracted_title": [row["extracted_title"] for row in extracted_data],
            "extracted_company": [row["extracted_company"] for row in extracted_data],
            "extracted_location": [row["extracted_location"] for row in extracted_data],
            "extracted_school": [row["extracted_school"] for row in extracted_data],
        },
        backend=backend,
    ).with_columns(
        nw.col("extracted_title").cast(nw.String),
        nw.col("extracted_company").cast(nw.String),
        nw.col("extracted_location").cast(nw.String),
        nw.col("extracted_school").cast(nw.String),
    )

    return nw.concat([df, extracted_df], how="horizontal")


def process_csv_with_lead_extraction(
    input_csv_path: str,
    output_csv_path: str,
    description_column: str,
    openai_api_key: str | None = None,
    model: str = "gpt-4o-mini",
) -> nw.DataFrame[Any]:
    """Process a CSV file to extract lead information from description column.

    Args:
        input_csv_path: Path to input CSV file
        output_csv_path: Path to save output CSV with extracted information
        description_column: Name of column containing descriptions to parse
        openai_api_key: OpenAI API key (uses env var if not provided)
        model: OpenAI model to use for extraction

    Returns:
        DataFrame with original columns plus extracted lead information
    """
    api_key: str | None = openai_api_key or os.getenv("OPENAI_API_KEY")

    if not api_key:
        raise ValueError("OpenAI API key not provided and OPENAI_API_KEY not set")

    import openai as openai_sdk

    client = getattr(openai_sdk, "OpenAI")(api_key=api_key)

    print(f"Reading CSV from: {input_csv_path}")
    # trunk-ignore(pyright/reportPrivateImportUsage): pyarrow.csv.read_csv is a public API, stubs flag it as internal
    table: pa.Table = pacsv.read_csv(input_csv_path)
    df: nw.DataFrame[Any] = nw.from_native(table, eager_only=True)
    print(f"Loaded {len(df)} rows")

    if description_column not in df.columns:
        raise ValueError(f"Column '{description_column}' not found in CSV")

    print(f"Extracting lead information from '{description_column}' column...")
    result_df: nw.DataFrame[Any] = df_add_extracted_lead_info(
        df=df,
        description_column=description_column,
        openai_client=client,
        model=model,
    )

    print(f"Writing results to: {output_csv_path}")
    # trunk-ignore(pyright/reportPrivateImportUsage): pyarrow.csv.write_csv is a public API, stubs flag it as internal
    pacsv.write_csv(nw.to_native(result_df), output_csv_path)
    print("Processing complete")

    return result_df


@nw.narwhalify
def save_rows_as_json_files(
    df: nw.DataFrame[Any],
    output_directory: str,
) -> int:
    """Save each row of DataFrame as a separate JSON file.

    Args:
        df: Input DataFrame to save as JSON files
        output_directory: Directory path where JSON files will be saved

    Returns:
        Number of JSON files created
    """
    output_path: Path = Path(output_directory)
    output_path.mkdir(
        parents=True,
        exist_ok=True,
    )

    files_created: int = 0

    for idx, row in enumerate(df.iter_rows(named=True)):
        name: str | None = row.get("name")

        if name:
            sanitized_name: str = "".join(
                c if c.isalnum() or c in (" ", "-", "_") else "_" for c in name
            )
            sanitized_name = sanitized_name.replace(" ", "_")
            filename: str = f"{idx:05d}_{sanitized_name}.json"

        else:
            filename = f"{idx:05d}_unnamed.json"

        file_path: Path = output_path / filename

        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(
                row,
                f,
                indent=2,
                ensure_ascii=False,
            )

        files_created += 1

        if (idx + 1) % 100 == 0:
            print(f"Created {idx + 1} JSON files...")

    print(f"Created {files_created} JSON files in {output_directory}")

    return files_created
