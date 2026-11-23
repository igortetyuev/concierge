from typing import Annotated, Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, field_validator
from sqlalchemy import delete, func, select, update

from concierge.agent import prompt as agent_prompt
from concierge.agent import tools as agent_tools
from concierge.agent.models import (
    CapabilityBinding,
    CodeAgent,
    McpServer,
    ModelProvider,
    NamedModel,
)
from concierge.agent.providers import http_base_url, provider_api_key
from concierge.auth import security
from concierge.auth.dependencies import SessionDep, require
from concierge.auth.models import Role, User
from concierge.channels import ChannelEvent, get_hub
from concierge.channels.telegram import gateway as tg_gateway
from concierge.channels.telegram import service as tg_service
from concierge.channels.telegram.models import TelegramBinding, TelegramLink
from concierge.conversations.models import (
    Conversation,
    ConversationMember,
    Run,
)
from concierge.core import audit
from concierge.core.audit import AuditEvent
from concierge.core.db import utcnow
from concierge.core.ratelimit import client_ip
from concierge.media import speaker as speaker_service
from concierge.media.models import Attachment, Voice, VoiceSample
from concierge.settings import service as settings_service

router = APIRouter(prefix="/admin")

ManageUsers = Annotated[User, Depends(require("manage_users"))]
ManageProviders = Annotated[User, Depends(require("manage_providers"))]
ManageMcp = Annotated[User, Depends(require("manage_mcp"))]
ManageCodeAgents = Annotated[User, Depends(require("manage_code_agents"))]


# ---------- users ----------


class UserIn(BaseModel):
    username: str
    display_name: str
    password: str
    role_id: int
    prompt_fragment: str | None = None


class UserPatch(BaseModel):
    display_name: str | None = None
    password: str | None = None
    role_id: int | None = None
    is_active: bool | None = None
    prompt_fragment: str | None = None


class UserOut(BaseModel):
    id: int
    username: str
    display_name: str
    role_id: int
    role: str
    is_active: bool
    prompt_fragment: str | None

    @classmethod
    def of(cls, u: User) -> "UserOut":
        return cls(
            id=u.id,
            username=u.username,
            display_name=u.display_name,
            role_id=u.role_id,
            role=u.role.name,
            is_active=u.is_active,
            prompt_fragment=u.prompt_fragment,
        )


@router.get("/users")
async def list_users(_: ManageUsers, session: SessionDep) -> list[UserOut]:
    rows = (await session.execute(select(User).order_by(User.id))).scalars()
    return [UserOut.of(u) for u in rows]


@router.post("/users", status_code=201)
async def create_user(
    body: UserIn, request: Request, admin: ManageUsers, session: SessionDep
) -> UserOut:
    if await session.get(Role, body.role_id) is None:
        raise HTTPException(404, "role not found")
    exists = (
        await session.execute(select(User).where(User.username == body.username))
    ).scalar_one_or_none()
    if exists is not None:
        raise HTTPException(409, "username taken")
    user = User(
        username=body.username,
        display_name=body.display_name,
        role_id=body.role_id,
        password_hash=security.hash_password(body.password),
        prompt_fragment=body.prompt_fragment,
    )
    session.add(user)
    audit.record(
        session,
        "admin.user.create",
        actor_id=admin.id,
        actor_name=admin.username,
        target=user.username,
        ip=client_ip(request),
    )
    await session.commit()
    await session.refresh(user, attribute_names=["role"])
    return UserOut.of(user)


@router.patch("/users/{user_id}")
async def patch_user(
    user_id: int,
    body: UserPatch,
    request: Request,
    admin: ManageUsers,
    session: SessionDep,
) -> UserOut:
    user = await session.get(User, user_id)
    if user is None:
        raise HTTPException(404, "user not found")
    data = body.model_dump(exclude_unset=True)
    if "role_id" in data and await session.get(Role, data["role_id"]) is None:
        raise HTTPException(404, "role not found")
    if "password" in data:
        user.password_hash = security.hash_password(data.pop("password"))
    for key, value in data.items():
        setattr(user, key, value)
    audit.record(
        session,
        "admin.user.patch",
        actor_id=admin.id,
        actor_name=admin.username,
        target=user.username,
        detail={"fields": sorted(data)},
        ip=client_ip(request),
    )
    await session.commit()
    await session.refresh(user, attribute_names=["role"])
    return UserOut.of(user)


@router.delete("/users/{user_id}")
async def delete_user(
    user_id: int, request: Request, admin: ManageUsers, session: SessionDep
) -> dict[str, bool]:
    if user_id == admin.id:
        raise HTTPException(400, "cannot delete yourself")
    user = await session.get(User, user_id)
    if user is None:
        raise HTTPException(404, "user not found")
    audit.record(
        session,
        "admin.user.delete",
        actor_id=admin.id,
        actor_name=admin.username,
        target=user.username,
        ip=client_ip(request),
    )
    await session.delete(user)
    await session.commit()
    return {"ok": True}


# ---------- roles ----------


class RoleIn(BaseModel):
    name: str
    permissions: dict[str, Any] = {}
    prompt_fragment: str | None = None


class RolePatch(BaseModel):
    name: str | None = None
    permissions: dict[str, Any] | None = None
    prompt_fragment: str | None = None


class RoleOut(BaseModel):
    id: int
    name: str
    permissions: dict[str, Any]
    prompt_fragment: str | None

    @classmethod
    def of(cls, r: Role) -> "RoleOut":
        return cls(
            id=r.id,
            name=r.name,
            permissions=r.permissions,
            prompt_fragment=r.prompt_fragment,
        )


@router.get("/roles")
async def list_roles(_: ManageUsers, session: SessionDep) -> list[RoleOut]:
    rows = (await session.execute(select(Role).order_by(Role.id))).scalars()
    return [RoleOut.of(r) for r in rows]


@router.post("/roles", status_code=201)
async def create_role(
    body: RoleIn, request: Request, admin: ManageUsers, session: SessionDep
) -> RoleOut:
    exists = (
        await session.execute(select(Role).where(Role.name == body.name))
    ).scalar_one_or_none()
    if exists is not None:
        raise HTTPException(409, "role exists")
    role = Role(
        name=body.name,
        permissions=body.permissions,
        prompt_fragment=body.prompt_fragment,
    )
    session.add(role)
    audit.record(
        session,
        "admin.role.create",
        actor_id=admin.id,
        actor_name=admin.username,
        target=role.name,
        ip=client_ip(request),
    )
    await session.commit()
    return RoleOut.of(role)


@router.patch("/roles/{role_id}")
async def patch_role(
    role_id: int,
    body: RolePatch,
    request: Request,
    admin: ManageUsers,
    session: SessionDep,
) -> RoleOut:
    role = await session.get(Role, role_id)
    if role is None:
        raise HTTPException(404, "role not found")
    data = body.model_dump(exclude_unset=True)
    for key, value in data.items():
        setattr(role, key, value)
    audit.record(
        session,
        "admin.role.patch",
        actor_id=admin.id,
        actor_name=admin.username,
        target=role.name,
        detail={"fields": sorted(data)},
        ip=client_ip(request),
    )
    await session.commit()
    return RoleOut.of(role)


@router.delete("/roles/{role_id}")
async def delete_role(
    role_id: int, request: Request, admin: ManageUsers, session: SessionDep
) -> dict[str, bool]:
    role = await session.get(Role, role_id)
    if role is None:
        raise HTTPException(404, "role not found")
    users = (
        await session.execute(
            select(func.count()).select_from(User).where(User.role_id == role_id)
        )
    ).scalar_one()
    if users:
        raise HTTPException(409, "role has users")
    audit.record(
        session,
        "admin.role.delete",
        actor_id=admin.id,
        actor_name=admin.username,
        target=role.name,
        ip=client_ip(request),
    )
    await session.delete(role)
    await session.commit()
    return {"ok": True}


# ---------- model providers ----------


class ProviderIn(BaseModel):
    name: str
    kind: str
    base_url: str | None = None
    api_key_ref: str | None = None
    enabled: bool = True


class ProviderPatch(BaseModel):
    name: str | None = None
    kind: str | None = None
    base_url: str | None = None
    api_key_ref: str | None = None
    enabled: bool | None = None


class ProviderOut(BaseModel):
    id: int
    name: str
    kind: str
    base_url: str | None
    api_key_ref: str | None
    enabled: bool

    @classmethod
    def of(cls, p: ModelProvider) -> "ProviderOut":
        return cls(
            id=p.id,
            name=p.name,
            kind=p.kind,
            base_url=p.base_url,
            api_key_ref=p.api_key_ref,
            enabled=p.enabled,
        )


@router.get("/providers")
async def list_providers(
    _: ManageProviders, session: SessionDep
) -> list[ProviderOut]:
    rows = (
        await session.execute(select(ModelProvider).order_by(ModelProvider.id))
    ).scalars()
    return [ProviderOut.of(p) for p in rows]


@router.post("/providers", status_code=201)
async def create_provider(
    body: ProviderIn, request: Request, admin: ManageProviders, session: SessionDep
) -> ProviderOut:
    provider = ModelProvider(**body.model_dump())
    session.add(provider)
    audit.record(
        session,
        "admin.provider.create",
        actor_id=admin.id,
        actor_name=admin.username,
        target=provider.name,
        ip=client_ip(request),
    )
    await session.commit()
    return ProviderOut.of(provider)


@router.patch("/providers/{provider_id}")
async def patch_provider(
    provider_id: int,
    body: ProviderPatch,
    request: Request,
    admin: ManageProviders,
    session: SessionDep,
) -> ProviderOut:
    provider = await session.get(ModelProvider, provider_id)
    if provider is None:
        raise HTTPException(404, "provider not found")
    data = body.model_dump(exclude_unset=True)
    for key, value in data.items():
        setattr(provider, key, value)
    audit.record(
        session,
        "admin.provider.patch",
        actor_id=admin.id,
        actor_name=admin.username,
        target=provider.name,
        detail={"fields": sorted(data)},
        ip=client_ip(request),
    )
    await session.commit()
    return ProviderOut.of(provider)


@router.delete("/providers/{provider_id}")
async def delete_provider(
    provider_id: int, request: Request, admin: ManageProviders, session: SessionDep
) -> dict[str, bool]:
    provider = await session.get(ModelProvider, provider_id)
    if provider is None:
        raise HTTPException(404, "provider not found")
    await session.execute(
        delete(CapabilityBinding).where(
            CapabilityBinding.provider_id == provider_id
        )
    )
    await session.execute(
        delete(NamedModel).where(NamedModel.provider_id == provider_id)
    )
    audit.record(
        session,
        "admin.provider.delete",
        actor_id=admin.id,
        actor_name=admin.username,
        target=provider.name,
        ip=client_ip(request),
    )
    await session.delete(provider)
    await session.commit()
    return {"ok": True}


@router.get("/providers/{provider_id}/models")
async def list_provider_models(
    provider_id: int,
    admin: ManageProviders,
    session: SessionDep,
    task: str | None = None,
) -> list[str]:
    """Proxy `GET {base_url}/models` of an OpenAI-compatible provider —
    e.g. speaches' local cache or an ollama host. `task` is forwarded
    verbatim (speaches: text-to-speech / automatic-speech-recognition).
    """
    provider = await session.get(ModelProvider, provider_id)
    if provider is None:
        raise HTTPException(404, "provider not found")
    base = http_base_url(provider)
    if base is None:
        raise HTTPException(422, "provider has no base_url")
    api_key = await provider_api_key(session, admin, provider)
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(
                f"{base}/models",
                params={"task": task} if task else {},
                headers=headers,
            )
            resp.raise_for_status()
    except httpx.HTTPError as e:
        raise HTTPException(502, f"provider /models failed: {e}") from e
    data = resp.json()
    return sorted(str(m["id"]) for m in data.get("data", []) if m.get("id"))


# ---------- capability bindings ----------


class BindingIn(BaseModel):
    capability: str
    provider_id: int
    model: str
    params: dict[str, Any] = {}
    role_id: int | None = None


class BindingPatch(BaseModel):
    capability: str | None = None
    provider_id: int | None = None
    model: str | None = None
    params: dict[str, Any] | None = None
    role_id: int | None = None


class BindingOut(BaseModel):
    id: int
    capability: str
    provider_id: int
    model: str
    params: dict[str, Any]
    role_id: int | None

    @classmethod
    def of(cls, b: CapabilityBinding) -> "BindingOut":
        return cls(
            id=b.id,
            capability=b.capability,
            provider_id=b.provider_id,
            model=b.model,
            params=b.params,
            role_id=b.role_id,
        )


@router.get("/capability-bindings")
async def list_bindings(
    _: ManageProviders, session: SessionDep
) -> list[BindingOut]:
    rows = (
        await session.execute(
            select(CapabilityBinding).order_by(CapabilityBinding.id)
        )
    ).scalars()
    return [BindingOut.of(b) for b in rows]


@router.post("/capability-bindings", status_code=201)
async def create_binding(
    body: BindingIn, request: Request, admin: ManageProviders, session: SessionDep
) -> BindingOut:
    if await session.get(ModelProvider, body.provider_id) is None:
        raise HTTPException(404, "provider not found")
    if body.role_id is not None and await session.get(Role, body.role_id) is None:
        raise HTTPException(404, "role not found")
    binding = CapabilityBinding(**body.model_dump())
    session.add(binding)
    audit.record(
        session,
        "admin.binding.create",
        actor_id=admin.id,
        actor_name=admin.username,
        target=binding.capability,
        detail={"model": binding.model, "role_id": binding.role_id},
        ip=client_ip(request),
    )
    await session.commit()
    return BindingOut.of(binding)


@router.patch("/capability-bindings/{binding_id}")
async def patch_binding(
    binding_id: int,
    body: BindingPatch,
    request: Request,
    admin: ManageProviders,
    session: SessionDep,
) -> BindingOut:
    binding = await session.get(CapabilityBinding, binding_id)
    if binding is None:
        raise HTTPException(404, "binding not found")
    data = body.model_dump(exclude_unset=True)
    for key, value in data.items():
        setattr(binding, key, value)
    audit.record(
        session,
        "admin.binding.patch",
        actor_id=admin.id,
        actor_name=admin.username,
        target=binding.capability,
        detail={"fields": sorted(data)},
        ip=client_ip(request),
    )
    await session.commit()
    return BindingOut.of(binding)


@router.delete("/capability-bindings/{binding_id}")
async def delete_binding(
    binding_id: int, request: Request, admin: ManageProviders, session: SessionDep
) -> dict[str, bool]:
    binding = await session.get(CapabilityBinding, binding_id)
    if binding is None:
        raise HTTPException(404, "binding not found")
    audit.record(
        session,
        "admin.binding.delete",
        actor_id=admin.id,
        actor_name=admin.username,
        target=binding.capability,
        ip=client_ip(request),
    )
    await session.delete(binding)
    await session.commit()
    return {"ok": True}


# ---------- named models ----------


def _check_named_tools(v: list[str] | None) -> list[str] | None:
    if v is not None:
        bad = [n for n in v if n not in agent_tools.NAMED_MODEL_TOOLS]
        if bad:
            raise ValueError(f"unknown tools: {', '.join(bad)}")
    return v


class NamedModelIn(BaseModel):
    name: str
    provider_id: int
    model: str
    params: dict[str, Any] = {}
    system_prompt: str | None = None
    tools: list[str] = []
    enabled: bool = True

    _check_tools = field_validator("tools")(_check_named_tools)


class NamedModelPatch(BaseModel):
    name: str | None = None
    provider_id: int | None = None
    model: str | None = None
    params: dict[str, Any] | None = None
    system_prompt: str | None = None
    tools: list[str] | None = None
    enabled: bool | None = None

    _check_tools = field_validator("tools")(_check_named_tools)


class NamedModelOut(BaseModel):
    id: int
    name: str
    provider_id: int
    model: str
    params: dict[str, Any]
    system_prompt: str | None
    tools: list[str]
    enabled: bool

    @classmethod
    def of(cls, m: NamedModel) -> "NamedModelOut":
        return cls(
            id=m.id,
            name=m.name,
            provider_id=m.provider_id,
            model=m.model,
            params=m.params,
            system_prompt=m.system_prompt,
            tools=m.tools,
            enabled=m.enabled,
        )


@router.get("/named-model-tools")
async def list_named_model_tools(_: ManageProviders) -> list[str]:
    """Tool names selectable in a named model's checklist."""
    return list(agent_tools.NAMED_MODEL_TOOLS)


@router.get("/named-models")
async def list_named_models(
    _: ManageProviders, session: SessionDep
) -> list[NamedModelOut]:
    rows = (
        await session.execute(select(NamedModel).order_by(NamedModel.id))
    ).scalars()
    return [NamedModelOut.of(m) for m in rows]


@router.post("/named-models", status_code=201)
async def create_named_model(
    body: NamedModelIn,
    request: Request,
    admin: ManageProviders,
    session: SessionDep,
) -> NamedModelOut:
    if await session.get(ModelProvider, body.provider_id) is None:
        raise HTTPException(404, "provider not found")
    named = NamedModel(**body.model_dump())
    session.add(named)
    audit.record(
        session,
        "admin.named_model.create",
        actor_id=admin.id,
        actor_name=admin.username,
        target=named.name,
        detail={"model": named.model},
        ip=client_ip(request),
    )
    await session.commit()
    return NamedModelOut.of(named)


@router.patch("/named-models/{model_id}")
async def patch_named_model(
    model_id: int,
    body: NamedModelPatch,
    request: Request,
    admin: ManageProviders,
    session: SessionDep,
) -> NamedModelOut:
    named = await session.get(NamedModel, model_id)
    if named is None:
        raise HTTPException(404, "named model not found")
    data = body.model_dump(exclude_unset=True)
    if "provider_id" in data and await session.get(
        ModelProvider, data["provider_id"]
    ) is None:
        raise HTTPException(404, "provider not found")
    for key, value in data.items():
        setattr(named, key, value)
    audit.record(
        session,
        "admin.named_model.patch",
        actor_id=admin.id,
        actor_name=admin.username,
        target=named.name,
        detail={"fields": sorted(data)},
        ip=client_ip(request),
    )
    await session.commit()
    return NamedModelOut.of(named)


@router.delete("/named-models/{model_id}")
async def delete_named_model(
    model_id: int,
    request: Request,
    admin: ManageProviders,
    session: SessionDep,
) -> dict[str, bool]:
    named = await session.get(NamedModel, model_id)
    if named is None:
        raise HTTPException(404, "named model not found")
    audit.record(
        session,
        "admin.named_model.delete",
        actor_id=admin.id,
        actor_name=admin.username,
        target=named.name,
        ip=client_ip(request),
    )
    await session.delete(named)
    await session.commit()
    return {"ok": True}


# ---------- mcp servers ----------


class McpIn(BaseModel):
    name: str
    transport: str
    command: str | None = None
    url: str | None = None
    env_ref: str | None = None
    enabled: bool = True
    allowed_tools: list[str] = []


class McpPatch(BaseModel):
    name: str | None = None
    transport: str | None = None
    command: str | None = None
    url: str | None = None
    env_ref: str | None = None
    enabled: bool | None = None
    allowed_tools: list[str] | None = None


class McpOut(BaseModel):
    id: int
    name: str
    transport: str
    command: str | None
    url: str | None
    env_ref: str | None
    enabled: bool
    allowed_tools: list[str]

    @classmethod
    def of(cls, m: McpServer) -> "McpOut":
        return cls(
            id=m.id,
            name=m.name,
            transport=m.transport,
            command=m.command,
            url=m.url,
            env_ref=m.env_ref,
            enabled=m.enabled,
            allowed_tools=m.allowed_tools,
        )


@router.get("/mcp-servers")
async def list_mcp(_: ManageMcp, session: SessionDep) -> list[McpOut]:
    rows = (
        await session.execute(select(McpServer).order_by(McpServer.id))
    ).scalars()
    return [McpOut.of(m) for m in rows]


@router.post("/mcp-servers", status_code=201)
async def create_mcp(
    body: McpIn, request: Request, admin: ManageMcp, session: SessionDep
) -> McpOut:
    server = McpServer(**body.model_dump())
    session.add(server)
    audit.record(
        session,
        "admin.mcp.create",
        actor_id=admin.id,
        actor_name=admin.username,
        target=server.name,
        ip=client_ip(request),
    )
    await session.commit()
    return McpOut.of(server)


@router.patch("/mcp-servers/{server_id}")
async def patch_mcp(
    server_id: int, body: McpPatch, request: Request, admin: ManageMcp, session: SessionDep
) -> McpOut:
    server = await session.get(McpServer, server_id)
    if server is None:
        raise HTTPException(404, "mcp server not found")
    data = body.model_dump(exclude_unset=True)
    for key, value in data.items():
        setattr(server, key, value)
    audit.record(
        session,
        "admin.mcp.patch",
        actor_id=admin.id,
        actor_name=admin.username,
        target=server.name,
        detail={"fields": sorted(data)},
        ip=client_ip(request),
    )
    await session.commit()
    return McpOut.of(server)


@router.delete("/mcp-servers/{server_id}")
async def delete_mcp(
    server_id: int, request: Request, admin: ManageMcp, session: SessionDep
) -> dict[str, bool]:
    server = await session.get(McpServer, server_id)
    if server is None:
        raise HTTPException(404, "mcp server not found")
    audit.record(
        session,
        "admin.mcp.delete",
        actor_id=admin.id,
        actor_name=admin.username,
        target=server.name,
        ip=client_ip(request),
    )
    await session.delete(server)
    await session.commit()
    return {"ok": True}


# ---------- code agents ----------


class CodeAgentIn(BaseModel):
    name: str
    ssh_host: str
    workdir: str
    args: dict[str, Any] = {}
    is_default: bool = False


class CodeAgentPatch(BaseModel):
    name: str | None = None
    ssh_host: str | None = None
    workdir: str | None = None
    args: dict[str, Any] | None = None
    is_default: bool | None = None


class CodeAgentOut(BaseModel):
    id: int
    name: str
    ssh_host: str
    workdir: str
    args: dict[str, Any]
    is_default: bool

    @classmethod
    def of(cls, a: CodeAgent) -> "CodeAgentOut":
        return cls(
            id=a.id,
            name=a.name,
            ssh_host=a.ssh_host,
            workdir=a.workdir,
            args=a.args,
            is_default=a.is_default,
        )


@router.get("/code-agents")
async def list_code_agents(
    _: ManageCodeAgents, session: SessionDep
) -> list[CodeAgentOut]:
    rows = (
        await session.execute(select(CodeAgent).order_by(CodeAgent.id))
    ).scalars()
    return [CodeAgentOut.of(a) for a in rows]


@router.post("/code-agents", status_code=201)
async def create_code_agent(
    body: CodeAgentIn, request: Request, admin: ManageCodeAgents, session: SessionDep
) -> CodeAgentOut:
    agent = CodeAgent(**body.model_dump())
    session.add(agent)
    await session.flush()
    if agent.is_default:
        await session.execute(
            update(CodeAgent)
            .where(CodeAgent.id != agent.id)
            .values(is_default=False)
        )
    audit.record(
        session,
        "admin.code_agent.create",
        actor_id=admin.id,
        actor_name=admin.username,
        target=f"{agent.name}@{agent.ssh_host}",
        ip=client_ip(request),
    )
    await session.commit()
    return CodeAgentOut.of(agent)


@router.patch("/code-agents/{agent_id}")
async def patch_code_agent(
    agent_id: int,
    body: CodeAgentPatch,
    request: Request,
    admin: ManageCodeAgents,
    session: SessionDep,
) -> CodeAgentOut:
    agent = await session.get(CodeAgent, agent_id)
    if agent is None:
        raise HTTPException(404, "code agent not found")
    data = body.model_dump(exclude_unset=True)
    for key, value in data.items():
        setattr(agent, key, value)
    if agent.is_default:
        await session.execute(
            update(CodeAgent)
            .where(CodeAgent.id != agent.id)
            .values(is_default=False)
        )
    audit.record(
        session,
        "admin.code_agent.patch",
        actor_id=admin.id,
        actor_name=admin.username,
        target=f"{agent.name}@{agent.ssh_host}",
        detail={"fields": sorted(data)},
        ip=client_ip(request),
    )
    await session.commit()
    return CodeAgentOut.of(agent)


@router.delete("/code-agents/{agent_id}")
async def delete_code_agent(
    agent_id: int, request: Request, admin: ManageCodeAgents, session: SessionDep
) -> dict[str, bool]:
    agent = await session.get(CodeAgent, agent_id)
    if agent is None:
        raise HTTPException(404, "code agent not found")
    audit.record(
        session,
        "admin.code_agent.delete",
        actor_id=admin.id,
        actor_name=admin.username,
        target=f"{agent.name}@{agent.ssh_host}",
        ip=client_ip(request),
    )
    await session.delete(agent)
    await session.commit()
    return {"ok": True}


# ---------- telegram ----------


class TelegramLinkIn(BaseModel):
    user_id: int
    telegram_user_id: int
    telegram_username: str | None = None


class TelegramLinkOut(BaseModel):
    user_id: int
    username: str
    telegram_user_id: int
    telegram_username: str | None
    linked_at: Any


@router.get("/telegram/links")
async def list_telegram_links(
    _: ManageUsers, session: SessionDep
) -> list[TelegramLinkOut]:
    rows = (
        await session.execute(
            select(TelegramLink, User)
            .join(User, User.id == TelegramLink.user_id)
            .order_by(TelegramLink.user_id)
        )
    ).all()
    return [
        TelegramLinkOut(
            user_id=link.user_id,
            username=user.username,
            telegram_user_id=link.telegram_user_id,
            telegram_username=link.telegram_username,
            linked_at=link.linked_at,
        )
        for link, user in rows
    ]


@router.post("/telegram/links", status_code=201)
async def create_telegram_link(
    body: TelegramLinkIn, request: Request, admin: ManageUsers, session: SessionDep
) -> TelegramLinkOut:
    user = await session.get(User, body.user_id)
    if user is None:
        raise HTTPException(404, "user not found")
    existing = await session.get(TelegramLink, body.user_id)
    if existing is not None:
        await session.delete(existing)
    clash = (
        await session.execute(
            select(TelegramLink).where(
                TelegramLink.telegram_user_id == body.telegram_user_id
            )
        )
    ).scalar_one_or_none()
    if clash is not None:
        await session.delete(clash)
    link = TelegramLink(
        user_id=body.user_id,
        telegram_user_id=body.telegram_user_id,
        telegram_username=body.telegram_username,
    )
    session.add(link)
    audit.record(
        session,
        "admin.telegram_link.create",
        actor_id=admin.id,
        actor_name=admin.username,
        target=user.username,
        detail={"telegram_user_id": body.telegram_user_id},
        ip=client_ip(request),
    )
    await session.commit()
    return TelegramLinkOut(
        user_id=link.user_id,
        username=user.username,
        telegram_user_id=link.telegram_user_id,
        telegram_username=link.telegram_username,
        linked_at=link.linked_at,
    )


@router.delete("/telegram/links/{user_id}")
async def delete_telegram_link(
    user_id: int, request: Request, admin: ManageUsers, session: SessionDep
) -> dict[str, bool]:
    if await session.get(TelegramLink, user_id) is None:
        raise HTTPException(404, "link not found")
    audit.record(
        session,
        "admin.telegram_link.delete",
        actor_id=admin.id,
        actor_name=admin.username,
        target=str(user_id),
        ip=client_ip(request),
    )
    dropped = await tg_service.unlink_user(session, user_id)
    for chat_id, thread_id in dropped:
        await tg_gateway.binding_dropped(chat_id, thread_id)
    return {"ok": True}


class TelegramBindingOut(BaseModel):
    chat_id: int
    thread_id: int
    kind: str
    conversation_id: int
    title: str


class TelegramOverviewOut(BaseModel):
    running: bool
    enabled: bool
    bot_username: str | None
    links: int
    bindings: list[TelegramBindingOut]


@router.get("/telegram/overview")
async def telegram_overview(
    _: ManageUsers, session: SessionDep
) -> TelegramOverviewOut:
    enabled = bool(
        await settings_service.get_global(session, "telegram.enabled", False)
    )
    bot_username = await settings_service.get_global(
        session, "telegram.bot_username"
    )
    links = int(
        (
            await session.execute(
                select(func.count()).select_from(TelegramLink)
            )
        ).scalar_one()
    )
    rows = (
        await session.execute(
            select(TelegramBinding, Conversation.title)
            .join(
                Conversation,
                Conversation.id == TelegramBinding.conversation_id,
            )
            .order_by(TelegramBinding.chat_id, TelegramBinding.thread_id)
        )
    ).all()
    return TelegramOverviewOut(
        running=tg_gateway.is_running(),
        enabled=enabled,
        bot_username=str(bot_username) if bot_username else None,
        links=links,
        bindings=[
            TelegramBindingOut(
                chat_id=b.chat_id,
                thread_id=b.thread_id,
                kind=b.kind,
                conversation_id=b.conversation_id,
                title=title,
            )
            for b, title in rows
        ],
    )


# ---------- runs journal ----------


class RunOut(BaseModel):
    id: int
    conversation_id: int
    conversation_title: str
    status: str
    model: str | None
    usage: dict[str, Any]
    started_at: Any
    finished_at: Any
    error: str | None


class RunsPage(BaseModel):
    total: int
    runs: list[RunOut]


@router.get("/runs")
async def list_runs(
    _: ManageUsers, session: SessionDep, limit: int = 50, offset: int = 0
) -> RunsPage:
    total = int(
        (
            await session.execute(select(func.count()).select_from(Run))
        ).scalar_one()
    )
    rows = (
        await session.execute(
            select(Run, Conversation.title)
            .join(Conversation, Conversation.id == Run.conversation_id)
            .order_by(Run.id.desc())
            .limit(min(limit, 200))
            .offset(offset)
        )
    ).all()
    return RunsPage(
        total=total,
        runs=[
            RunOut(
                id=r.id,
                conversation_id=r.conversation_id,
                conversation_title=title,
                status=r.status,
                model=r.model,
                usage=r.usage,
                started_at=r.started_at,
                finished_at=r.finished_at,
                error=r.error,
            )
            for r, title in rows
        ],
    )


# ---------- prompt preview ----------


@router.get("/prompt-preview")
async def prompt_preview(
    user_id: int,
    _: ManageUsers,
    session: SessionDep,
    conversation_id: int | None = None,
) -> dict[str, str]:
    """Composed system prompt for a user (optionally inside a real session)."""
    user = await session.get(User, user_id)
    if user is None:
        raise HTTPException(404, "user not found")
    if conversation_id is not None:
        conv = await session.get(Conversation, conversation_id)
        if conv is None:
            raise HTTPException(404, "conversation not found")
        member_ids = (
            await session.execute(
                select(ConversationMember.user_id).where(
                    ConversationMember.conversation_id == conv.id
                )
            )
        ).scalars().all()
        names = (
            (
                await session.execute(
                    select(User.display_name).where(User.id.in_(member_ids))
                )
            ).scalars().all()
            if member_ids
            else [user.display_name]
        )
    else:
        conv = Conversation(kind="personal", title="", owner_id=user.id)
        names = [user.display_name]
    text = await agent_prompt.system_prompt(session, user, conv, list(names))
    return {"prompt": text}


# ---------- audit log ----------


class AuditOut(BaseModel):
    id: int
    at: Any
    actor_id: int | None
    actor_name: str
    action: str
    target: str
    detail: dict[str, Any]
    ip: str


class AuditPage(BaseModel):
    total: int
    events: list[AuditOut]


@router.get("/audit-log")
async def list_audit(
    _: ManageUsers, session: SessionDep, limit: int = 100, offset: int = 0
) -> AuditPage:
    total = int(
        (
            await session.execute(select(func.count()).select_from(AuditEvent))
        ).scalar_one()
    )
    rows = (
        await session.execute(
            select(AuditEvent)
            .order_by(AuditEvent.id.desc())
            .limit(min(limit, 500))
            .offset(offset)
        )
    ).scalars()
    return AuditPage(
        total=total,
        events=[
            AuditOut(
                id=e.id,
                at=e.at,
                actor_id=e.actor_id,
                actor_name=e.actor_name,
                action=e.action,
                target=e.target,
                detail=e.detail,
                ip=e.ip,
            )
            for e in rows
        ],
    )


# ---------- voices (speaker-ID) ----------


async def _invalidate_convs(conv_ids: set[int]) -> None:
    hub = get_hub()
    for cid in conv_ids:
        await hub.publish(ChannelEvent("messages.invalidate", cid, {}))


class VoicePatch(BaseModel):
    label: str | None = None
    user_id: int | None = None
    authenticated: bool | None = None


class VoiceMergeIn(BaseModel):
    into_id: int


class VoiceSampleIn(BaseModel):
    attachment_id: int


class VoiceOut(BaseModel):
    id: int
    label: str
    user_id: int | None
    user_name: str | None
    authenticated: bool
    sample_count: int
    sample_attachment_id: int | None
    last_heard_at: Any
    created_at: Any


class VoiceSampleOut(BaseModel):
    id: int
    attachment_id: int
    attachment_name: str
    score: float
    created_at: Any


@router.get("/voices")
async def list_voices(_: ManageUsers, session: SessionDep) -> list[VoiceOut]:
    rows = (
        await session.execute(
            select(Voice, User.display_name)
            .outerjoin(User, Voice.user_id == User.id)
            .order_by(Voice.id)
        )
    ).all()
    return [
        VoiceOut(
            id=v.id,
            label=v.label,
            user_id=v.user_id,
            user_name=uname,
            authenticated=v.authenticated,
            sample_count=v.sample_count,
            sample_attachment_id=v.sample_attachment_id,
            last_heard_at=v.last_heard_at,
            created_at=v.created_at,
        )
        for v, uname in rows
    ]


@router.patch("/voices/{voice_id}")
async def patch_voice(
    voice_id: int,
    body: VoicePatch,
    request: Request,
    admin: ManageUsers,
    session: SessionDep,
) -> VoiceOut:
    voice = await session.get(Voice, voice_id)
    if voice is None:
        raise HTTPException(404, "voice not found")
    data = body.model_dump(exclude_unset=True)
    if "label" in data:
        voice.label = str(data["label"])[:64] or voice.label
    if "user_id" in data:
        uid = data["user_id"]
        if uid is not None and await session.get(User, uid) is None:
            raise HTTPException(404, "user not found")
        voice.user_id = uid
    if "authenticated" in data:
        voice.authenticated = bool(data["authenticated"])
    conv_ids = set()
    if "label" in data or "user_id" in data:
        conv_ids = await speaker_service.relabel_speaker(
            session, voice, source_voice_id=voice.id
        )
    if {"authenticated", "user_id"} & set(data):
        conv_ids |= await speaker_service.authenticate_voice(session, voice)
    audit.record(
        session,
        "admin.voice.patch",
        actor_id=admin.id,
        actor_name=admin.username,
        target=voice.label,
        detail={"fields": sorted(data)},
        ip=client_ip(request),
    )
    await session.commit()
    if conv_ids:
        await _invalidate_convs(conv_ids)
    uname = None
    if voice.user_id is not None:
        owner = await session.get(User, voice.user_id)
        uname = owner.display_name if owner else None
    return VoiceOut(
        id=voice.id,
        label=voice.label,
        user_id=voice.user_id,
        user_name=uname,
        authenticated=voice.authenticated,
        sample_count=voice.sample_count,
        sample_attachment_id=voice.sample_attachment_id,
        last_heard_at=voice.last_heard_at,
        created_at=voice.created_at,
    )


@router.post("/voices/{voice_id}/merge")
async def merge_voice(
    voice_id: int,
    body: VoiceMergeIn,
    request: Request,
    admin: ManageUsers,
    session: SessionDep,
) -> dict[str, bool]:
    src = await session.get(Voice, voice_id)
    into = await session.get(Voice, body.into_id)
    if src is None or into is None:
        raise HTTPException(404, "voice not found")
    if src.id == into.id:
        raise HTTPException(400, "cannot merge a voice into itself")
    await session.execute(
        update(VoiceSample)
        .where(VoiceSample.voice_id == src.id)
        .values(voice_id=into.id)
    )
    if into.sample_attachment_id is None:
        into.sample_attachment_id = src.sample_attachment_id
    if src.last_heard_at and (
        into.last_heard_at is None or src.last_heard_at > into.last_heard_at
    ):
        into.last_heard_at = src.last_heard_at
    await session.delete(src)
    await session.flush()
    await speaker_service.recompute_centroid(session, into.id)
    conv_ids = await speaker_service.relabel_speaker(
        session, into, source_voice_id=voice_id
    )
    conv_ids |= await speaker_service.authenticate_voice(session, into)
    audit.record(
        session,
        "admin.voice.merge",
        actor_id=admin.id,
        actor_name=admin.username,
        target=into.label,
        detail={"from_id": voice_id, "from_label": src.label},
        ip=client_ip(request),
    )
    await session.commit()
    if conv_ids:
        await _invalidate_convs(conv_ids)
    return {"ok": True}


@router.delete("/voices/{voice_id}")
async def delete_voice(
    voice_id: int, request: Request, admin: ManageUsers, session: SessionDep
) -> dict[str, bool]:
    voice = await session.get(Voice, voice_id)
    if voice is None:
        raise HTTPException(404, "voice not found")
    audit.record(
        session,
        "admin.voice.delete",
        actor_id=admin.id,
        actor_name=admin.username,
        target=voice.label,
        ip=client_ip(request),
    )
    await session.execute(
        delete(VoiceSample).where(VoiceSample.voice_id == voice_id)
    )
    conv_ids = await speaker_service.clear_speaker(session, voice_id)
    await session.delete(voice)
    await session.commit()
    if conv_ids:
        await _invalidate_convs(conv_ids)
    return {"ok": True}


@router.get("/voices/{voice_id}/samples")
async def list_voice_samples(
    voice_id: int, _: ManageUsers, session: SessionDep
) -> list[VoiceSampleOut]:
    if await session.get(Voice, voice_id) is None:
        raise HTTPException(404, "voice not found")
    rows = (
        await session.execute(
            select(VoiceSample, Attachment.original_name)
            .join(Attachment, VoiceSample.attachment_id == Attachment.id)
            .where(VoiceSample.voice_id == voice_id)
            .order_by(VoiceSample.id)
        )
    ).all()
    return [
        VoiceSampleOut(
            id=s.id,
            attachment_id=s.attachment_id,
            attachment_name=name,
            score=s.score,
            created_at=s.created_at,
        )
        for s, name in rows
    ]


@router.post("/voices/{voice_id}/samples", status_code=201)
async def add_voice_sample(
    voice_id: int,
    body: VoiceSampleIn,
    request: Request,
    admin: ManageUsers,
    session: SessionDep,
) -> VoiceSampleOut:
    """Attach an attachment to the voice: reuse its stored embedding when a
    sample row already exists (moved between voices), embed fresh otherwise."""
    voice = await session.get(Voice, voice_id)
    if voice is None:
        raise HTTPException(404, "voice not found")
    att = await session.get(Attachment, body.attachment_id)
    if att is None:
        raise HTTPException(404, "attachment not found")
    moved_from = None
    sample = (
        await session.execute(
            select(VoiceSample).where(VoiceSample.attachment_id == att.id)
        )
    ).scalar_one_or_none()
    if sample is not None:
        moved_from = sample.voice_id if sample.voice_id != voice_id else None
        sample.voice_id = voice_id
        sample.score = (
            speaker_service.cosine(sample.embedding, voice.centroid)
            if voice.centroid
            else 0.0
        )
    else:
        vec, _ = await speaker_service.embed_attachment(session, att)
        if vec is None:
            raise HTTPException(422, "cannot compute embedding")
        sample = VoiceSample(
            voice_id=voice_id,
            attachment_id=att.id,
            embedding=vec,
            score=speaker_service.cosine(vec, voice.centroid)
            if voice.centroid
            else 0.0,
        )
        session.add(sample)
    if voice.sample_attachment_id is None:
        voice.sample_attachment_id = att.id
    voice.last_heard_at = utcnow()
    await session.flush()
    await speaker_service.recompute_centroid(session, voice_id)
    if moved_from is not None:
        await speaker_service.recompute_centroid(session, moved_from)
    conv_ids = await speaker_service.relabel_speaker(
        session, voice, attachment_ids={att.id}
    )
    conv_ids |= await speaker_service.authenticate_attachment(session, att)
    audit.record(
        session,
        "admin.voice.sample.add",
        actor_id=admin.id,
        actor_name=admin.username,
        target=voice.label,
        detail={"attachment_id": att.id, "from_voice_id": moved_from},
        ip=client_ip(request),
    )
    await session.commit()
    if conv_ids:
        await _invalidate_convs(conv_ids)
    return VoiceSampleOut(
        id=sample.id,
        attachment_id=sample.attachment_id,
        attachment_name=att.original_name,
        score=sample.score,
        created_at=sample.created_at,
    )


@router.delete("/voices/{voice_id}/samples/{sample_id}")
async def delete_voice_sample(
    voice_id: int,
    sample_id: int,
    request: Request,
    admin: ManageUsers,
    session: SessionDep,
) -> dict[str, bool]:
    voice = await session.get(Voice, voice_id)
    if voice is None:
        raise HTTPException(404, "voice not found")
    sample = await session.get(VoiceSample, sample_id)
    if sample is None or sample.voice_id != voice_id:
        raise HTTPException(404, "sample not found")
    if voice.sample_attachment_id == sample.attachment_id:
        voice.sample_attachment_id = None
    await session.delete(sample)
    await session.flush()
    await speaker_service.recompute_centroid(session, voice_id)
    if voice.sample_attachment_id is None:
        first = (
            await session.execute(
                select(VoiceSample.attachment_id)
                .where(VoiceSample.voice_id == voice_id)
                .order_by(VoiceSample.id.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        voice.sample_attachment_id = first
    audit.record(
        session,
        "admin.voice.sample.delete",
        actor_id=admin.id,
        actor_name=admin.username,
        target=voice.label,
        detail={"attachment_id": sample.attachment_id},
        ip=client_ip(request),
    )
    await session.commit()
    return {"ok": True}
