import inspect
import json
import logging
import os
from dataclasses import dataclass
from typing import Any, cast

from pydantic_ai.models import Model
from pydantic_ai.models.test import TestModel
from pydantic_ai.settings import ModelSettings
from sqlalchemy.ext.asyncio import AsyncSession

from concierge.agent.models import CapabilityBinding, ModelProvider
from concierge.agent.service import get_capability_binding
from concierge.auth.models import User
from concierge.settings import service as settings_service

log = logging.getLogger(__name__)


@dataclass(slots=True)
class BoundModel:
    """A capability resolved to a concrete model instance."""

    model: Model
    label: str  # e.g. "openai/gpt-5.2" — stored on runs.model
    settings: ModelSettings


_MODEL_SETTINGS_KEYS = (
    ModelSettings.__required_keys__ | ModelSettings.__optional_keys__
)


def _model_settings(params: dict[str, Any]) -> ModelSettings:
    filtered = {k: v for k, v in params.items() if k in _MODEL_SETTINGS_KEYS}
    return cast(ModelSettings, filtered)


async def resolve_ref(
    session: AsyncSession, user: User, ref: str | None
) -> str | None:
    """Resolve an indirection (api_key_ref / env_ref value) to a string.

    Lookup order: settings store (user -> role -> global, secrets unwrapped),
    then an environment variable of the same name.
    """
    if not ref:
        return None
    value = await settings_service.get_value(session, user, ref)
    if isinstance(value, str) and value:
        return value
    if value is None:
        return os.environ.get(ref) or None
    return None


async def resolve_ref_dict(
    session: AsyncSession, user: User, ref: str | None
) -> dict[str, str]:
    """Like resolve_ref, but for structured values (e.g. MCP server env).

    A settings value must be a dict; an env-var fallback is parsed as JSON.
    """
    if not ref:
        return {}
    value = await settings_service.get_value(session, user, ref)
    if isinstance(value, dict):
        return {str(k): str(v) for k, v in value.items()}
    raw = os.environ.get(ref) if value is None else None
    if raw:
        try:
            parsed = json.loads(raw)
        except ValueError:
            parsed = None
        if isinstance(parsed, dict):
            return {str(k): str(v) for k, v in parsed.items()}
    return {}


def _provider_kwargs(provider: ModelProvider, api_key: str | None) -> dict[str, Any]:
    return {
        k: v
        for k, v in {"base_url": provider.base_url, "api_key": api_key}.items()
        if v
    }


def _build_model(
    provider: ModelProvider, model_name: str, api_key: str | None
) -> Model | None:
    kwargs = _provider_kwargs(provider, api_key)
    kind = provider.kind
    if kind in ("openai", "openai-compatible"):
        from pydantic_ai.models.openai import OpenAIChatModel
        from pydantic_ai.providers.openai import OpenAIProvider

        return OpenAIChatModel(model_name, provider=OpenAIProvider(**kwargs))
    if kind == "anthropic":
        from pydantic_ai.models.anthropic import AnthropicModel
        from pydantic_ai.providers.anthropic import AnthropicProvider

        return AnthropicModel(model_name, provider=AnthropicProvider(**kwargs))
    if kind == "ollama":
        from pydantic_ai.models.openai import OpenAIChatModel
        from pydantic_ai.providers.ollama import OllamaProvider

        return OpenAIChatModel(model_name, provider=OllamaProvider(**kwargs))
    if kind == "google":
        from pydantic_ai.models.google import GoogleModel
        from pydantic_ai.providers.google import GoogleProvider

        return GoogleModel(model_name, provider=GoogleProvider(**kwargs))
    if kind == "deepseek":
        from pydantic_ai.models.openai import OpenAIChatModel
        from pydantic_ai.providers.deepseek import DeepSeekProvider

        return OpenAIChatModel(model_name, provider=DeepSeekProvider(api_key=api_key))
    if kind == "mistral":
        from pydantic_ai.models.mistral import MistralModel
        from pydantic_ai.providers.mistral import MistralProvider

        return MistralModel(model_name, provider=MistralProvider(**kwargs))
    log.warning("unknown provider kind %r", kind)
    return None


_TEST_MODEL_KWARGS = set(inspect.signature(TestModel.__init__).parameters) - {
    "self"
}


async def _bound(
    session: AsyncSession, user: User, binding: CapabilityBinding
) -> BoundModel | None:
    provider = binding.provider
    if provider is None or not provider.enabled:
        return None
    label = f"{provider.name}/{binding.model}"
    if provider.kind == "test":
        # Test-only provider kind for e2e tests without real LLM calls.
        # Its default profile claims native-tool support but _request() then
        # raises; report no native tools so capabilities use local fallbacks.
        from pydantic_ai.profiles import ModelProfile

        kwargs = {
            k: v for k, v in binding.params.items() if k in _TEST_MODEL_KWARGS
        }
        kwargs.setdefault(
            "profile", ModelProfile(supported_native_tools=frozenset())
        )
        return BoundModel(TestModel(**kwargs), label, ModelSettings())
    api_key = await resolve_ref(session, user, provider.api_key_ref)
    model = _build_model(provider, binding.model, api_key)
    if model is None:
        return None
    return BoundModel(model, label, _model_settings(binding.params))


async def get_model(
    session: AsyncSession, capability: str, user: User
) -> BoundModel | None:
    """Resolve `capability` for `user` via capability_bindings + model_providers.

    Role-specific binding wins; binding changes take effect without restart.
    """
    binding = await get_capability_binding(session, capability, user.role_id)
    if binding is None:
        return None
    return await _bound(session, user, binding)
