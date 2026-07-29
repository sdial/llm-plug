import secrets
import uuid
from datetime import UTC, datetime

from pydantic import BaseModel, Field


def _generate_key() -> str:
    return f"llmplug-api-{secrets.token_hex(16)}"


class ApiKey(BaseModel):
    id: str = Field(default_factory=lambda: f"key_{uuid.uuid4().hex[:8]}")
    name: str
    key: str = Field(default_factory=_generate_key)
    allowed_models: list[str] = Field(default_factory=list)
    notes: str = ""
    request_count: int = 0
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    created_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())


class ApiKeyCreate(BaseModel):
    name: str
    key: str | None = None
    allowed_models: list[str] = Field(default_factory=list)
    notes: str = ""


class ApiKeyUpdate(BaseModel):
    name: str | None = None
    key: str | None = None
    allowed_models: list[str] | None = None
    notes: str | None = None
