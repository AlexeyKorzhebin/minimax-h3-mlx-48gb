"""Request bodies of the studio API (spec §7). Responses are plain dicts built by `ops`; the
master's types come from these models through OpenAPI (`web/openapi.json`)."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

FrameFormat = Literal["16:9", "9:16"]


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ProjectCreate(_Body):
    title: str = Field(min_length=1, max_length=200)
    frame_format: FrameFormat
    idea_text: str = Field(default="", max_length=20000)


class ProjectPatch(_Body):
    expected_revision: int
    title: str | None = Field(default=None, min_length=1, max_length=200)
    frame_format: FrameFormat | None = None
