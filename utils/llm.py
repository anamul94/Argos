"""Central LLM factory with provider switching (Ollama or Bedrock).

Uses langchain.chat_models.init_chat_model so model/provider configuration
is centralized and easy to switch through environment variables.
"""

import os

import structlog
from dotenv import load_dotenv
from langchain.chat_models import init_chat_model

load_dotenv()

log = structlog.get_logger(__name__)

_DEFAULT_PROVIDER = "ollama"
_DEFAULT_BEDROCK_MODEL_ID = "bedrock:global.anthropic.claude-sonnet-4-6"
_DEFAULT_OLLAMA_MODEL = "glm-4.7-flash:latest"
_DEFAULT_OLLAMA_BASE_URL = "http://127.0.0.1:11434"
_KNOWN_PROVIDER_PREFIXES = {
    "anthropic",
    "azure_openai",
    "bedrock",
    "cohere",
    "google_genai",
    "groq",
    "mistralai",
    "ollama",
    "openai",
    "together",
}


def get_active_llm_provider() -> str:
    """Return active provider name: 'ollama' or 'bedrock'."""
    provider = os.environ.get("LLM_PROVIDER", _DEFAULT_PROVIDER).strip().lower()
    if provider not in {"ollama", "bedrock"}:
        log.warning("invalid_llm_provider_fallback", provider=provider, fallback=_DEFAULT_PROVIDER)
        return _DEFAULT_PROVIDER
    return provider


def get_active_model_name() -> str:
    """Return the active model name without provider prefix."""
    return _strip_provider_prefix(get_active_model_ref())


def get_active_model_ref() -> str:
    """Return provider-prefixed model reference for init_chat_model.

    Examples:
      - ollama:glm-4.7-flash:latest
      - bedrock:global.anthropic.claude-sonnet-4-6
    """
    provider = get_active_llm_provider()
    explicit = os.environ.get("LLM_MODEL", "").strip()
    if explicit:
        return _with_provider_prefix(explicit, provider)

    if provider == "ollama":
        raw = os.environ.get("OLLAMA_MODEL", _DEFAULT_OLLAMA_MODEL).strip() or _DEFAULT_OLLAMA_MODEL
        return _with_provider_prefix(raw, "ollama")

    raw = os.environ.get("BEDROCK_MODEL_ID", _DEFAULT_BEDROCK_MODEL_ID).strip() or _DEFAULT_BEDROCK_MODEL_ID
    return _with_provider_prefix(raw, "bedrock")


def get_llm(structured_output_schema=None):
    """Build the active LLM (Ollama or Bedrock) with optional structured output."""
    provider = get_active_llm_provider()
    model_ref = get_active_model_ref()
    kwargs = _provider_kwargs(provider)
    llm = init_chat_model(model_ref, **kwargs)

    if structured_output_schema is not None:
        return llm.with_structured_output(structured_output_schema)
    return llm


def get_bedrock_llm(structured_output_schema=None):
    """Backward-compatible alias used across existing nodes.

    Despite the historical function name, this now returns whichever provider
    is active in `LLM_PROVIDER`.
    """
    return get_llm(structured_output_schema=structured_output_schema)


def _provider_kwargs(provider: str) -> dict:
    """Build provider-specific kwargs for init_chat_model."""
    temperature = float(os.environ.get("LLM_TEMPERATURE", "0"))
    kwargs: dict = {"temperature": temperature}
    model_ref = get_active_model_ref()

    if provider == "ollama":
        # Ensures ollama integration package is present.
        try:
            import langchain_ollama  # noqa: F401
        except Exception as e:
            raise RuntimeError(
                "Ollama provider selected but langchain-ollama is not installed. "
                "Install it with: pip install langchain-ollama"
            ) from e
        base_url = os.environ.get("OLLAMA_BASE_URL", _DEFAULT_OLLAMA_BASE_URL).strip() or _DEFAULT_OLLAMA_BASE_URL
        kwargs["base_url"] = base_url
        log.debug("llm_init", provider=provider, model=model_ref, base_url=base_url, temperature=temperature)
        return kwargs

    # Bedrock provider kwargs (keeps existing AWS behavior)
    region = os.environ.get("BEDROCK_REGION", os.environ.get("AWS_DEFAULT_REGION", "us-east-1"))
    access_key = os.environ.get("AWS_ACCESS_KEY_ID")
    secret_key = os.environ.get("AWS_SECRET_ACCESS_KEY")
    session_token = os.environ.get("AWS_SESSION_TOKEN")
    kwargs["region_name"] = region
    kwargs["model_kwargs"] = {"max_tokens": 2048}
    if access_key and secret_key:
        kwargs["aws_access_key_id"] = access_key
        kwargs["aws_secret_access_key"] = secret_key
        if session_token:
            kwargs["aws_session_token"] = session_token
    log.debug("llm_init", provider=provider, model=model_ref, region=region, has_key=bool(access_key))
    return kwargs


def _with_provider_prefix(model: str, provider: str) -> str:
    """Ensure model has provider prefix unless it already has any known prefix."""
    raw = model.strip()
    if not raw:
        return raw
    if ":" in raw:
        prefix = raw.split(":", 1)[0].lower()
        if prefix in _KNOWN_PROVIDER_PREFIXES:
            return raw
    return f"{provider}:{raw}"


def _strip_provider_prefix(model_ref: str) -> str:
    """Strip known provider prefix from model ref for display/metadata."""
    if ":" not in model_ref:
        return model_ref
    prefix, rest = model_ref.split(":", 1)
    if prefix.lower() in _KNOWN_PROVIDER_PREFIXES:
        return rest
    return model_ref
