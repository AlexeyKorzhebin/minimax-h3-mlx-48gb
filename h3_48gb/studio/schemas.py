"""Request bodies of the studio API (spec §7). Responses are plain dicts built by `ops`; the
master's types come from these models through OpenAPI (`web/openapi.json`)."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

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


# --- slice S1 ---------------------------------------------------------------------------------

TAG_PATTERN = r"^[a-z][a-z0-9_-]{0,31}$"
Tag = Field(pattern=TAG_PATTERN)


class CastAssetIn(_Body):
    asset_id: str
    role: Literal["image"]        # voice references arrive with audio uploads (after S1)


class CastCreate(_Body):
    expected_revision: int
    tag: str = Tag
    description_ru: str = Field(min_length=1, max_length=4000)
    assets: list[CastAssetIn] = Field(default_factory=list, max_length=8)
    description_en: str | None = Field(default=None, max_length=4000)
    portrait_phrase: str | None = Field(default=None, max_length=400)

    @field_validator("description_ru")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("описание не может быть пустым")
        return value

    @field_validator("assets")
    @classmethod
    def _no_repeats(cls, value: list[CastAssetIn]) -> list[CastAssetIn]:
        ids = [a.asset_id for a in value]
        if len(set(ids)) != len(ids):
            raise ValueError("один и тот же файл указан дважды")
        return value


Mode = Literal["from_frame", "refs", "text"]
_TEXT = Field(default="", max_length=8000)


class SourceFrame(_Body):
    kind: Literal["none", "prev_last_frame", "asset"] = "none"
    scene_id: str | None = None
    asset_id: str | None = None


class SceneIn(_Body):
    scene_id: str | None = None
    # И1a: required for an existing scene (scene_id given) — the edit_version this copy was read at
    expected_edit_version: int | None = None
    mode: Mode
    action_text: str = _TEXT
    performance_text: str = _TEXT
    sound_text: str = _TEXT
    music_text: str = _TEXT
    state_in_text: str = _TEXT
    state_out_text: str = _TEXT
    requested_s: float = Field(gt=0, le=60)
    seed: int | None = Field(default=None, ge=1, le=2**31 - 1)
    source_frame: SourceFrame = Field(default_factory=SourceFrame)
    refs: list[str] = Field(default_factory=list, max_length=12)


class DeletedScene(_Body):
    """A scene the tab removes, at the edit_version it read (И1a): a deletion is an edit too."""
    scene_id: str
    expected_edit_version: int


class ScenesReplace(_Body):
    expected_revision: int
    deleted: list[DeletedScene] = Field(default_factory=list)
    scenes: list[SceneIn]          # no upper bound: a project may grow without limit (§4.4)


class ScenePatch(_Body):
    expected_edit_version: int
    mode: Mode | None = None
    action_text: str | None = Field(default=None, max_length=8000)
    performance_text: str | None = Field(default=None, max_length=8000)
    sound_text: str | None = Field(default=None, max_length=8000)
    music_text: str | None = Field(default=None, max_length=8000)
    state_in_text: str | None = Field(default=None, max_length=8000)
    state_out_text: str | None = Field(default=None, max_length=8000)
    requested_s: float | None = Field(default=None, gt=0, le=60)
    seed: int | None = Field(default=None, ge=1, le=2**31 - 1)
    source_frame: SourceFrame | None = None
    refs: list[str] | None = Field(default=None, max_length=12)


class LintRequest(_Body):
    scene_ids: list[str] | None = None


class ProposeRequest(_Body):
    idea_text: str = Field(min_length=1, max_length=20000)
    scenes_count: int = Field(default=2, ge=1)


class ApplyRequest(_Body):
    proposal_id: str
    expected_revision: int
    # every scene the proposal replaces, at the edit_version the tab read (O26 deletes them all)
    deleted: list[DeletedScene] = Field(default_factory=list)


# --- slice S2 ---------------------------------------------------------------------------------

class PlanRequest(_Body):
    """O10. `image`: `scene_id` (the scene's keyframe) or `prompt` + `width` + `height`.
    `render` (slice S3): `scene_ids` and `tier`."""
    kind: Literal["image", "render"]
    scene_id: str | None = None
    prompt: str | None = Field(default=None, min_length=1, max_length=4000)
    width: int | None = None
    height: int | None = None
    scene_ids: list[str] | None = None
    tier: Literal["sketch", "final"] | None = None

    @model_validator(mode="after")
    def _one_source(self):
        if self.scene_id is not None and (self.prompt is not None or self.width is not None
                                          or self.height is not None):
            raise ValueError("scene_id excludes prompt, width and height")
        return self


class PlanConfirm(_Body):
    plan_id: str


class CancelRequest(_Body):
    reason: str = Field(default="", max_length=500)
