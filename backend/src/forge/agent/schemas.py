from pydantic import BaseModel, ConfigDict, Field


class ReadFileArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str = Field(
        min_length=1,
        max_length=1024,
        description="Relative path to the workspace file to read.",
    )


class SearchTextArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(
        min_length=1,
        max_length=1024,
        description="Text substring or token to search for in files.",
    )
    pattern: str = Field(
        default="**/*",
        min_length=1,
        max_length=255,
        description="Glob pattern to match file paths against.",
    )
    limit: int = Field(
        default=100,
        gt=0,
        le=1000,
        description="Maximum number of line matches to return.",
    )
    case_sensitive: bool = Field(
        default=False,
        description="Whether to perform case-sensitive matching.",
    )


class WriteFileArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str = Field(
        min_length=1,
        max_length=1024,
        description="Relative path to the workspace file to write.",
    )
    content: str = Field(
        max_length=1024 * 1024,
        description="Text content to write to the file.",
    )


class RunCommandArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    argv: list[str] = Field(
        min_length=1,
        max_length=64,
        description="Command arguments list, where argv[0] is the executable name.",
    )
    cwd: str = Field(
        default=".",
        max_length=1024,
        description="Workspace-relative working directory.",
    )
    timeout_seconds: float = Field(
        default=30.0,
        gt=0,
        le=300.0,
        description="Maximum execution timeout in seconds.",
    )


class SearchSymbolsArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(
        min_length=1,
        max_length=255,
        description="Symbol name substring to search for (function, class, method, etc.).",
    )
    pattern: str = Field(
        default="**/*",
        min_length=1,
        max_length=255,
        description="Glob pattern to filter code files.",
    )
    limit: int = Field(
        default=50,
        gt=0,
        le=500,
        description="Maximum number of symbol matches to return.",
    )


class ListFilesArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pattern: str = Field(
        default="**/*",
        min_length=1,
        max_length=255,
        description="Glob pattern to filter file paths.",
    )
    limit: int = Field(
        default=100,
        gt=0,
        le=1000,
        description="Maximum number of files to return.",
    )


class WebSearchArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(
        min_length=1,
        max_length=1000,
        description="Search query to find external technical documentation and web sources.",
    )
    limit: int = Field(
        default=5,
        gt=0,
        le=20,
        description="Maximum number of search results to retrieve.",
    )
    domains: list[str] = Field(
        default_factory=list,
        max_length=10,
        description="Optional list of domain restrictions (e.g. docs.pydantic.dev).",
    )
    recency: str | None = Field(
        default=None,
        description="Optional recency filter (day, week, month, year).",
    )


class FetchUrlArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str = Field(
        min_length=1,
        max_length=2048,
        description="HTTP or HTTPS URL of documentation page to safely retrieve.",
    )
    max_chars: int = Field(
        default=12000,
        gt=0,
        le=50000,
        description="Maximum number of characters of extracted content to return.",
    )


class SearchDocumentationArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(
        min_length=1,
        max_length=1000,
        description="Documentation topic, API, function, or framework feature to search for.",
    )
    package: str | None = Field(
        default=None,
        max_length=255,
        description="Optional package or library name (e.g. fastapi, pydantic).",
    )
    version: str | None = Field(
        default=None,
        max_length=64,
        description="Optional package version to target (e.g. 2.0).",
    )
    domains: list[str] = Field(
        default_factory=list,
        max_length=10,
        description="Optional list of domain restrictions.",
    )
    limit: int = Field(
        default=5,
        gt=0,
        le=20,
        description="Maximum number of documentation sources to retrieve.",
    )


class InspectResearchArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    research_id: str = Field(
        min_length=1,
        max_length=64,
        description="Unique identifier of previously generated research report.",
    )
    source_id: str | None = Field(
        default=None,
        max_length=64,
        description="Optional source ID filter (e.g. src_1).",
    )
