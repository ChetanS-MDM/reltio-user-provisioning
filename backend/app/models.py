"""Request bodies."""
from typing import List, Literal, Optional

from pydantic import BaseModel, Field, field_validator


class PresignRequest(BaseModel):
    filename: str = Field(min_length=1, max_length=200)
    size: int = Field(gt=0, le=50_000_000)


class SubmitRequest(BaseModel):
    global_id: str = Field(min_length=1, max_length=20)
    email: str = Field(min_length=3, max_length=254, pattern=r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
    country_code: str = Field(min_length=2, max_length=12)
    dou: str = Field(min_length=1, max_length=300)
    justification: str = Field(min_length=1, max_length=2000)
    tenants: List[str] = Field(min_length=1, max_length=20)
    attachment_key: str = Field(min_length=1, max_length=500)

    @field_validator("tenants")
    @classmethod
    def _strip_tenants(cls, v: List[str]) -> List[str]:
        cleaned = [t.strip() for t in v if t.strip()]
        if not cleaned:
            raise ValueError("At least one tenant is required.")
        return cleaned


class DecisionRequest(BaseModel):
    decision: Literal["approve", "reject"]
    comment: str = Field(default="", max_length=1000)
    approved_roles: Optional[List[str]] = None

    @field_validator("comment")
    @classmethod
    def _strip_comment(cls, v: str) -> str:
        return v.strip()


class EmailDecisionRequest(BaseModel):
    token: str = Field(min_length=1)
    decision: Literal["approve", "reject"]
    comment: str = Field(default="", max_length=1000)
    approved_roles: Optional[List[str]] = None

    @field_validator("comment")
    @classmethod
    def _strip_comment(cls, v: str) -> str:
        return v.strip()
