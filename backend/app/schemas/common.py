"""Shared schema pieces: pagination, the money type, and Indian identifier validators."""
from __future__ import annotations

import re
from typing import Annotated, Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field, field_validator

T = TypeVar("T")

# Every monetary field on the wire. Named so a reader of a schema sees the unit
# without having to remember the project rule, and non-negative because no
# figure in this product is a credit — a refund is its own field.
Paise = Annotated[int, Field(ge=0, description="Amount in paise (₹1 = 100)")]

# The regulator's formats. Validating on the way in is worth doing because a
# GSTIN with a transposed digit produces a filing that is rejected weeks later
# by the portal, with the penalty clock still running.
GSTIN_RE = re.compile(r"^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][0-9A-Z]Z[0-9A-Z]$")
PAN_RE = re.compile(r"^[A-Z]{5}[0-9]{4}[A-Z]$")
CIN_RE = re.compile(r"^[LU][0-9]{5}[A-Z]{2}[0-9]{4}[A-Z]{3}[0-9]{6}$")
LLPIN_RE = re.compile(r"^[A-Z]{3}-[0-9]{4}$")
TAN_RE = re.compile(r"^[A-Z]{4}[0-9]{5}[A-Z]$")
# Indian mobile numbers, with or without the +91 country code.
PHONE_RE = re.compile(r"^(\+91[\-\s]?)?[6-9][0-9]{9}$")


class ORMModel(BaseModel):
    """Base for anything read out of the database."""

    model_config = ConfigDict(from_attributes=True)


class Page(BaseModel, Generic[T]):
    """A page of results.

    ``total`` is the unfiltered-by-page count, which costs a second query and
    is worth it: without it the UI cannot render "page 3 of 40", and a CA firm
    scanning 400 clients' filings needs to know how far the list goes.
    """

    items: list[T]
    total: int
    limit: int
    offset: int

    @property
    def has_more(self) -> bool:
        return self.offset + len(self.items) < self.total


class PageParams(BaseModel):
    """Standard pagination query parameters.

    The 200 ceiling is a guard, not a preference: a CA firm asking for every
    filing across 100 clients in one request would build a response big enough
    to matter, and the cursor for that is several pages.
    """

    limit: int = Field(default=50, ge=1, le=200)
    offset: int = Field(default=0, ge=0)


def normalise_identifier(value: str | None) -> str | None:
    """Uppercase and strip a statutory identifier, or ``None`` if empty.

    Applied before every format check and before fingerprinting, so that
    ``27aabcu9603r1zm`` and ``27AABCU9603R1ZM `` are one GSTIN rather than two.
    """
    if value is None:
        return None
    cleaned = "".join(value.split()).upper()
    return cleaned or None


def _identifier_validator(pattern: re.Pattern[str], label: str):
    """Build a reusable validator for one identifier format."""

    def _check(value: str | None) -> str | None:
        cleaned = normalise_identifier(value)
        if cleaned is None:
            return None
        if not pattern.match(cleaned):
            raise ValueError(f"Not a valid {label}")
        return cleaned

    return _check


validate_gstin = _identifier_validator(GSTIN_RE, "GSTIN")
validate_pan = _identifier_validator(PAN_RE, "PAN")
validate_cin = _identifier_validator(CIN_RE, "CIN")
validate_llpin = _identifier_validator(LLPIN_RE, "LLPIN")
validate_tan = _identifier_validator(TAN_RE, "TAN")


def validate_phone(value: str | None) -> str | None:
    """Normalise an Indian mobile number to bare ten digits.

    The country code is stripped rather than kept, because WhatsApp and every
    SMS gateway want a different prefix format and storing one of them makes
    the other a string edit at send time.
    """
    if value is None or not value.strip():
        return None
    cleaned = re.sub(r"[\s\-()]", "", value.strip())
    if not PHONE_RE.match(cleaned):
        raise ValueError("Not a valid Indian mobile number")
    return cleaned[-10:]


class MessageResponse(BaseModel):
    """For endpoints whose only useful answer is "it worked"."""

    message: str
    detail: dict | None = None


class IdentifierMixin(BaseModel):
    """The statutory identifier fields, with their validators attached.

    Mixed into the organization create/update schemas so the formats are
    checked in exactly one place — a second copy would drift the first time
    one of them changed.
    """

    gstin: str | None = None
    pan: str | None = None
    cin: str | None = None
    llpin: str | None = None
    tan: str | None = None

    _v_gstin = field_validator("gstin")(validate_gstin)
    _v_pan = field_validator("pan")(validate_pan)
    _v_cin = field_validator("cin")(validate_cin)
    _v_llpin = field_validator("llpin")(validate_llpin)
    _v_tan = field_validator("tan")(validate_tan)
