"""Template library bodies."""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field, model_validator

from app.models.enums import Regulation
from app.schemas.common import ORMModel


class TemplateSummary(ORMModel):
    id: int
    organization_id: int | None = None
    code: str
    name: str
    description: str | None = None
    regulation: Regulation | None = None
    filing_type: str | None = None
    category: str
    version: int
    is_active: bool
    is_system: bool
    statutory_reference: str | None = None
    created_at: datetime


class TemplateResponse(TemplateSummary):
    template_json: dict | None = None
    body_template: str | None = None
    instructions: str | None = None
    metadata_json: dict | None = None
    updated_at: datetime


class TemplateCreateRequest(BaseModel):
    """A tenant's own template.

    ``category`` decides which of the two payloads is required: a ``filing``
    template needs a field schema for the editor to render, a ``document``
    template needs a body to fill in. A row with neither is a template that
    cannot produce anything, so it is refused here rather than discovered when
    someone tries to use it.
    """

    code: str = Field(min_length=2, max_length=128, pattern=r"^[a-z0-9][a-z0-9._-]*$")
    name: str = Field(min_length=2, max_length=255)
    category: str = Field(default="filing", pattern=r"^(filing|document|checklist)$")
    description: str | None = None
    regulation: Regulation | None = None
    filing_type: str | None = Field(default=None, max_length=64)
    template_json: dict | None = None
    body_template: str | None = None
    instructions: str | None = None
    statutory_reference: str | None = Field(default=None, max_length=255)
    metadata_json: dict | None = None

    @model_validator(mode="after")
    def _payload_matches_category(self):
        if self.category == "filing" and not self.template_json:
            raise ValueError("A filing template needs a template_json field schema")
        if self.category == "document" and not self.body_template:
            raise ValueError("A document template needs a body_template")
        if self.template_json is not None:
            fields = self.template_json.get("fields")
            if fields is not None and not isinstance(fields, list):
                raise ValueError("template_json.fields must be a list")
        return self


class TemplateUpdateRequest(BaseModel):
    """Edit a tenant template.

    Changing ``template_json`` or ``body_template`` creates a new version
    rather than editing in place — see the router. The other fields are
    presentation and are applied to the existing row, because nothing is
    rendered from them and a version bump for a typo in the description would
    make the version number meaningless.
    """

    name: str | None = Field(default=None, min_length=2, max_length=255)
    description: str | None = None
    template_json: dict | None = None
    body_template: str | None = None
    instructions: str | None = None
    statutory_reference: str | None = Field(default=None, max_length=255)
    is_active: bool | None = None
    metadata_json: dict | None = None


class TemplateRenderRequest(BaseModel):
    """Fill a document template's placeholders."""

    values: dict[str, str | int | float | None] = Field(default_factory=dict)


class TemplateRenderResponse(BaseModel):
    """The filled body, and what could not be filled.

    ``missing`` is returned rather than raising, because a half-filled board
    resolution is still useful to a CA who wants to see the shape before
    gathering the rest — and because failing the whole render on one absent
    placeholder makes the preview unusable during drafting.
    """

    code: str
    version: int
    body: str
    missing: list[str] = []
