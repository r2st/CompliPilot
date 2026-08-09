"""The filing and document template library (section 4.8).

Shared like the obligations catalogue: system templates have
``organization_id IS NULL`` and are visible to every tenant, a tenant's own sit
beside them, and nobody edits anybody else's.

**Versions are immutable.** Changing a template's field schema or body inserts
a new row at the next version and deactivates the old one; it never edits in
place. A filing keeps a pointer to the row it was drafted against, so reopening
a return submitted last year renders the fields that existed then rather than
today's. Editing in place would silently rewrite history for every filing that
pointed at it.
"""
from __future__ import annotations

import logging
import re

from fastapi import APIRouter, Depends, Query, Request, status
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import get_tenant_context, require_manager
from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.core.tenancy import TenantContext, catalogue_scoped
from app.models.enums import AuditAction, Regulation
from app.models.template import Template
from app.routers._helpers import changed_fields, deny_system_row, paginate, record, snapshot
from app.schemas.common import MessageResponse, Page
from app.schemas.template import (
    TemplateCreateRequest,
    TemplateRenderRequest,
    TemplateRenderResponse,
    TemplateResponse,
    TemplateSummary,
    TemplateUpdateRequest,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/templates", tags=["templates"])

_AUDITED = ("code", "name", "category", "version", "is_active", "regulation")

# Placeholders in a document template body: ``{company_name}``. Matched with a
# regex rather than handed to ``str.format`` on raw input, because a body
# containing ``{0.__class__}`` handed to ``format`` is an attribute-traversal
# vector — the classic Python format-string injection. Substitution below is a
# plain dictionary lookup, which has no such reach.
_PLACEHOLDER = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")


def placeholders(body: str) -> list[str]:
    """The placeholder names a template body declares, in order, de-duplicated."""
    seen: list[str] = []
    for name in _PLACEHOLDER.findall(body):
        if name not in seen:
            seen.append(name)
    return seen


def render(body: str, values: dict) -> tuple[str, list[str]]:
    """Fill a body's placeholders. Returns ``(rendered, unfilled)``.

    An unfilled placeholder is left in place rather than blanked. A board
    resolution with a visible ``{director_name}`` tells the drafter what is
    missing; one with a silent gap where the name belongs gets signed.
    """
    missing: list[str] = []

    def _substitute(match: re.Match[str]) -> str:
        key = match.group(1)
        if key not in values or values[key] is None:
            missing.append(key)
            return match.group(0)
        return str(values[key])

    return _PLACEHOLDER.sub(_substitute, body), missing


@router.get("", response_model=Page[TemplateSummary], summary="Browse templates")
def list_templates(
    ctx: TenantContext = Depends(get_tenant_context),
    db: Session = Depends(get_db),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    regulation: Regulation | None = None,
    category: str | None = Query(None, max_length=32),
    filing_type: str | None = Query(None, max_length=64),
    search: str | None = Query(None, max_length=128),
    include_inactive: bool = False,
    mine_only: bool = False,
):
    """The library as this tenant sees it: system templates plus their own."""
    stmt = catalogue_scoped(Template, ctx)
    if not include_inactive:
        stmt = stmt.where(Template.is_active.is_(True))
    if regulation is not None:
        stmt = stmt.where(Template.regulation == regulation)
    if category:
        stmt = stmt.where(Template.category == category)
    if filing_type:
        stmt = stmt.where(Template.filing_type == filing_type)
    if mine_only:
        stmt = stmt.where(Template.organization_id == ctx.org_id)
    if search:
        pattern = f"%{search}%"
        stmt = stmt.where(
            or_(
                Template.name.ilike(pattern),
                Template.code.ilike(pattern),
                Template.description.ilike(pattern),
            )
        )

    stmt = stmt.order_by(Template.code, Template.version.desc())
    rows, total = paginate(db, stmt, limit=limit, offset=offset)
    return Page[TemplateSummary](
        items=[TemplateSummary.model_validate(r) for r in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


def _get_template(
    db: Session, ctx: TenantContext, template_id: int, *, include_retired: bool = False
) -> Template:
    """One template by id, in this tenant's view of the library, or a 404.

    ``include_retired`` is what makes reopening an old filing work. A filing
    keeps a pointer to the row it was drafted against, and retiring a template
    only soft deletes it — so the read paths resolve retired rows and the write
    paths do not. Without the split, retiring a template would break every
    filing ever drafted against it, which is the opposite of what soft deleting
    it was for.
    """
    template = db.execute(
        catalogue_scoped(Template, ctx, include_deleted=include_retired).where(
            Template.id == template_id
        )
    ).scalar_one_or_none()
    if template is None:
        raise NotFoundError("No such template")
    return template


@router.get("/by-code/{code}", response_model=TemplateResponse, summary="Latest by code")
def get_template_by_code(
    code: str,
    ctx: TenantContext = Depends(get_tenant_context),
    db: Session = Depends(get_db),
    version: int | None = Query(None, description="A specific version; default is latest"),
):
    """Resolve a template by its stable code.

    A tenant's own row wins over the system one with the same code — that is
    what makes an override an override. Ordering by ``organization_id``
    descending puts the tenant's row (a value) ahead of the system's (null)
    on both Postgres and SQLite without relying on either's NULL ordering.
    """
    stmt = catalogue_scoped(Template, ctx).where(Template.code == code)
    if version is not None:
        stmt = stmt.where(Template.version == version)
    else:
        stmt = stmt.where(Template.is_active.is_(True))

    template = db.execute(
        stmt.order_by(
            Template.organization_id.is_(None), Template.version.desc()
        ).limit(1)
    ).scalar_one_or_none()
    if template is None:
        raise NotFoundError("No such template")
    return TemplateResponse.model_validate(template)


@router.get("/{template_id}", response_model=TemplateResponse, summary="One template")
def get_template(
    template_id: int,
    ctx: TenantContext = Depends(get_tenant_context),
    db: Session = Depends(get_db),
):
    return TemplateResponse.model_validate(
        _get_template(db, ctx, template_id, include_retired=True)
    )


@router.post(
    "",
    response_model=TemplateResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Add a template of your own",
)
def create_template(
    payload: TemplateCreateRequest,
    request: Request,
    ctx: TenantContext = Depends(require_manager),
    db: Session = Depends(get_db),
):
    """A tenant template. May share a code with a system one — that is an override."""
    existing = db.execute(
        select(Template).where(
            Template.organization_id == ctx.org_id,
            Template.code == payload.code,
            Template.deleted_at.is_(None),
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise ConflictError(
            "You already have a template with that code; edit it to create a new version",
            details={"code": payload.code, "template_id": existing.id},
        )

    template = Template(
        organization_id=ctx.org_id,
        is_system=False,
        is_active=True,
        version=1,
        **payload.model_dump(),
    )
    db.add(template)
    db.flush()

    record(
        db,
        ctx,
        request,
        action=AuditAction.CREATE,
        entity_type="template",
        entity_id=template.id,
        after=snapshot(template, _AUDITED),
        summary=f"Template {template.code} v1 created",
    )
    db.commit()
    db.refresh(template)
    return TemplateResponse.model_validate(template)


@router.patch(
    "/{template_id}", response_model=TemplateResponse, summary="Edit or version a template"
)
def update_template(
    template_id: int,
    payload: TemplateUpdateRequest,
    request: Request,
    ctx: TenantContext = Depends(require_manager),
    db: Session = Depends(get_db),
):
    """Edit a tenant template.

    A change to ``template_json`` or ``body_template`` produces a **new
    version**: the response is a different row with ``version + 1``, and the
    one that was edited is deactivated. Everything else — name, description,
    instructions — is applied in place, because nothing is rendered from those
    and a version bump for a typo would make the number meaningless.
    """
    template = _get_template(db, ctx, template_id)
    deny_system_row(template, label="template")

    changes = payload.model_dump(exclude_unset=True)
    content_changed = (
        "template_json" in changes and changes["template_json"] != template.template_json
    ) or (
        "body_template" in changes and changes["body_template"] != template.body_template
    )

    if content_changed:
        highest = db.execute(
            select(func.max(Template.version)).where(
                Template.organization_id == ctx.org_id, Template.code == template.code
            )
        ).scalar_one()
        successor = Template(
            organization_id=ctx.org_id,
            code=template.code,
            name=changes.get("name", template.name),
            description=changes.get("description", template.description),
            regulation=template.regulation,
            filing_type=template.filing_type,
            category=template.category,
            template_json=changes.get("template_json", template.template_json),
            body_template=changes.get("body_template", template.body_template),
            instructions=changes.get("instructions", template.instructions),
            statutory_reference=changes.get(
                "statutory_reference", template.statutory_reference
            ),
            metadata_json=changes.get("metadata_json", template.metadata_json),
            version=int(highest or template.version) + 1,
            is_active=True,
            is_system=False,
        )
        template.is_active = False
        db.add(successor)
        db.flush()

        record(
            db,
            ctx,
            request,
            action=AuditAction.CREATE,
            entity_type="template",
            entity_id=successor.id,
            before={"version": template.version, "template_id": template.id},
            after=snapshot(successor, _AUDITED),
            summary=(
                f"Template {successor.code} versioned to v{successor.version}; "
                f"v{template.version} deactivated"
            ),
        )
        db.commit()
        db.refresh(successor)
        return TemplateResponse.model_validate(successor)

    before = snapshot(template, _AUDITED)
    for field, value in changes.items():
        if value is not None:
            setattr(template, field, value)

    diff = changed_fields(before, snapshot(template, _AUDITED))
    if diff:
        record(
            db,
            ctx,
            request,
            action=AuditAction.UPDATE,
            entity_type="template",
            entity_id=template.id,
            before={k: v["from"] for k, v in diff.items()},
            after={k: v["to"] for k, v in diff.items()},
            summary=f"Template {template.code} updated: {', '.join(sorted(diff))}",
        )
    db.commit()
    db.refresh(template)
    return TemplateResponse.model_validate(template)


@router.post(
    "/{template_id}/render",
    response_model=TemplateRenderResponse,
    summary="Fill a document template",
)
def render_template(
    template_id: int,
    payload: TemplateRenderRequest,
    ctx: TenantContext = Depends(get_tenant_context),
    db: Session = Depends(get_db),
):
    """Substitute values into a document template's placeholders.

    Only for ``document`` templates — a filing template's ``template_json`` is
    a field schema for the editor, not a body, and there is nothing to render.
    """
    template = _get_template(db, ctx, template_id, include_retired=True)
    if not template.body_template:
        raise ValidationError(
            "This template has no body to render; it is a "
            f"{template.category} template",
            details={"category": template.category},
        )

    body, missing = render(template.body_template, payload.values)
    return TemplateRenderResponse(
        code=template.code, version=template.version, body=body, missing=missing
    )


@router.get(
    "/{template_id}/placeholders",
    response_model=list[str],
    summary="What a document template asks for",
)
def template_placeholders(
    template_id: int,
    ctx: TenantContext = Depends(get_tenant_context),
    db: Session = Depends(get_db),
):
    """The placeholder names, so the UI can build the form before rendering."""
    template = _get_template(db, ctx, template_id, include_retired=True)
    return placeholders(template.body_template or "")


@router.delete(
    "/{template_id}", response_model=MessageResponse, summary="Retire a template"
)
def delete_template(
    template_id: int,
    request: Request,
    ctx: TenantContext = Depends(require_manager),
    db: Session = Depends(get_db),
):
    """Soft delete and deactivate.

    Filings drafted against it keep their ``template_id`` and still resolve —
    the FK is ``SET NULL`` on hard delete, which never happens here, and a
    soft-deleted row is still readable by id when an old filing is reopened.
    """
    template = _get_template(db, ctx, template_id)
    deny_system_row(template, label="template")

    # Snapshot before the mutation, not after. Taken afterwards it records
    # ``is_active: False`` as the *prior* state, so the entry describes a row
    # that was already retired rather than the retirement that happened — and
    # an audit trail is only evidence if it says what changed.
    before = snapshot(template, _AUDITED)

    template.is_active = False
    template.soft_delete()
    record(
        db,
        ctx,
        request,
        action=AuditAction.SOFT_DELETE,
        entity_type="template",
        entity_id=template.id,
        before=before,
        summary=f"Template {template.code} v{template.version} retired",
    )
    db.commit()
    return MessageResponse(message="Template retired")
