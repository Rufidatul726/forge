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
