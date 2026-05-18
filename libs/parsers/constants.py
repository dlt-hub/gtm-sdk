import pyarrow as pa

NULL_VALUES: list[str] = [
    "",
    "NA",
    "N/A",
    "null",
    "None",
]

DF_COLUMN_FULL_NAME: str = "full_name"
DF_COLUMN_FIRST_NAME: str = "first_name"
DF_COLUMN_MIDDLE_NAME: str = "middle_name"
DF_COLUMN_LAST_NAME: str = "last_name"
DF_COLUMN_LINKEDIN: str = "linkedin"
DF_COLUMN_E_MAIL_1_VALUE: str = "e_mail_1_value"
DF_COLUMN_PHONE_1_VALUE: str = "phone_1_value"
DF_COLUMN_BIRTHDAY: str = "birthday"
DF_COLUMN_CLASS_YEAR: str = "class_year"
DF_COLUMN_SOURCE: str = "source"
DF_COLUMN_ORGANIZATION_NAME: str = "organization_name"
DF_COLUMN_ORGANIZATION_TITLE: str = "organization_title"
DF_COLUMN_PHOTO: str = "photo"

DF_SCHEMA: dict[str, pa.DataType] = {
    DF_COLUMN_CLASS_YEAR: pa.string(),
    DF_COLUMN_E_MAIL_1_VALUE: pa.string(),
    DF_COLUMN_FIRST_NAME: pa.string(),
    DF_COLUMN_MIDDLE_NAME: pa.string(),
    DF_COLUMN_FULL_NAME: pa.string(),
    DF_COLUMN_LAST_NAME: pa.string(),
    DF_COLUMN_LINKEDIN: pa.string(),
    DF_COLUMN_PHONE_1_VALUE: pa.string(),
    DF_COLUMN_PHOTO: pa.string(),
    DF_COLUMN_BIRTHDAY: pa.string(),
    DF_COLUMN_SOURCE: pa.string(),
    DF_COLUMN_ORGANIZATION_TITLE: pa.string(),
    "file_path": pa.string(),
}

DF_TARGET_COLUMNS: dict[str, list[str]] = {
    DF_COLUMN_FULL_NAME: [
        "full_name",
        "name",
        "Name",
        "prefFullname",
        "PrefFullName",
    ],
    DF_COLUMN_FIRST_NAME: [
        "first_name",
        "First Name",
        "PrefName-ISal",
        "FirstName",
    ],
    DF_COLUMN_LAST_NAME: [
        "last_name",
        "Last Name",
        "LegalLastName",
        "LastName",
    ],
    DF_COLUMN_LINKEDIN: [
        "linkedin",
        "LinkedIn",
        "LinkedIn Profile",
        "Linkedin",
        "URL",
    ],
    DF_COLUMN_E_MAIL_1_VALUE: [
        "e_mail_1_value",
        "email",
        "Email",
        "Email Address",
        "E-Mail Address",
        "PCEmail",
        "email address",
        "Emails",
    ],
    DF_COLUMN_PHONE_1_VALUE: [
        "phone_1_value",
        "Phone #",
        "Phone Number:",
        "phone",
        "Phone 1 - Value",
    ],
    DF_COLUMN_BIRTHDAY: [
        "birthday",
        "BDate",
    ],
    DF_COLUMN_CLASS_YEAR: [
        "PG Yr",
        "Class Year",
        "Class year",
        "Class year full",
        "Yrs",  # P'27
        "yrs",
        "Class Year",
    ],
    DF_COLUMN_SOURCE: [
        "source",
    ],
    DF_COLUMN_ORGANIZATION_TITLE: [
        "organization_title",
        "Title",
    ],
}

EMAIL_DOMAINS_TO_KEEP: list[str] = [
    "gmail.com",
    "aol.com",
    "hotmail.com",
    "icloud.com",
    "live.com",
    "me.com",
    "msn.com",
    "outlook.com",
    "pm.me",
    "yahoo.com",
]
