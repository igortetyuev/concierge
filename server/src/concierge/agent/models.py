from datetime import datetime

from sqlalchemy import JSON, ForeignKey, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from concierge.auth.models import JsonDict
from concierge.core.db import Base

JSONBType = JSON().with_variant(JSONB(), "postgresql")


class ModelProvider(Base):
    __tablename__ = "model_providers"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True)
    kind: Mapped[str] = mapped_column(String(32))
    base_url: Mapped[str | None] = mapped_column(String(512))
    api_key_ref: Mapped[str | None] = mapped_column(String(128))
    enabled: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class CapabilityBinding(Base):
    __tablename__ = "capability_bindings"

    id: Mapped[int] = mapped_column(primary_key=True)
    capability: Mapped[str] = mapped_column(String(32), index=True)
    provider_id: Mapped[int] = mapped_column(ForeignKey("model_providers.id"))
    model: Mapped[str] = mapped_column(String(128))
    params: Mapped[JsonDict] = mapped_column(JSONBType, default=dict)
    role_id: Mapped[int | None] = mapped_column(ForeignKey("roles.id"))

    provider: Mapped[ModelProvider] = relationship(lazy="joined")


class McpServer(Base):
    __tablename__ = "mcp_servers"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True)
    transport: Mapped[str] = mapped_column(String(32))
    command: Mapped[str | None] = mapped_column(String(512))
    url: Mapped[str | None] = mapped_column(String(512))
    env_ref: Mapped[str | None] = mapped_column(String(128))
    enabled: Mapped[bool] = mapped_column(default=True)
    allowed_tools: Mapped[list[str]] = mapped_column(JSONBType, default=list)


class CodeAgent(Base):
    __tablename__ = "code_agents"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(32))
    ssh_host: Mapped[str] = mapped_column(String(128))
    workdir: Mapped[str] = mapped_column(String(512))
    args: Mapped[JsonDict] = mapped_column(JSONBType, default=dict)
    is_default: Mapped[bool] = mapped_column(default=False)
