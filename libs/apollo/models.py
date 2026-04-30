from __future__ import annotations

from pydantic import BaseModel

# --- People input models ---


class PersonEnrichInput(BaseModel):
    email: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    name: str | None = None
    domain: str | None = None
    linkedin_url: str | None = None
    organization_name: str | None = None


class PersonSearchInput(BaseModel):
    q_keywords: str | None = None
    person_titles: list[str] = []
    person_seniorities: list[str] = []
    person_locations: list[str] = []
    q_organization_domains_list: list[str] = []
    organization_locations: list[str] = []
    organization_num_employees_ranges: list[str] = []
    page: int = 1
    per_page: int = 10


# --- Organization input models ---


class OrgEnrichInput(BaseModel):
    domain: str


class OrgSearchInput(BaseModel):
    q_keywords: str | None = None
    organization_locations: list[str] = []
    organization_num_employees_ranges: list[str] = []
    page: int = 1
    per_page: int = 10
