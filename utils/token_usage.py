"""Token usage helpers for per-node and total metadata tracking."""

from copy import deepcopy

INPUT_PRICE_PER_1M_USD = 3.0
OUTPUT_PRICE_PER_1M_USD = 15.0


def empty_token_usage_metadata() -> dict:
    """Return the default token usage metadata structure."""
    return {
        "pricing": {
            "input_per_1m_usd": INPUT_PRICE_PER_1M_USD,
            "output_per_1m_usd": OUTPUT_PRICE_PER_1M_USD,
        },
        "nodes": {},
        "totals": {
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
            "tool_calls": 0,
            "total_input_token_cost_usd": 0.0,
            "total_output_token_cost_usd": 0.0,
            "total_token_cost_usd": 0.0,
        },
    }


def build_node_usage(
    *,
    model_name: str,
    input_tokens: int = 0,
    output_tokens: int = 0,
    total_tokens: int | None = None,
    tool_calls: int = 0,
    tool_calls_by_name: dict | None = None,
) -> dict:
    """Build a single node token/tool usage record."""
    input_tokens = _as_int(input_tokens)
    output_tokens = _as_int(output_tokens)
    total_tokens = _as_int(total_tokens) if total_tokens is not None else input_tokens + output_tokens
    tool_calls = _as_int(tool_calls)
    input_cost = _cost_for_tokens(input_tokens, is_input=True)
    output_cost = _cost_for_tokens(output_tokens, is_input=False)
    return {
        "model_name": model_name,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
        "tool_calls": tool_calls,
        "tool_calls_by_name": tool_calls_by_name or {},
        "input_token_cost_usd": input_cost,
        "output_token_cost_usd": output_cost,
        "total_token_cost_usd": round(input_cost + output_cost, 8),
    }


def merge_node_usage(metadata: dict | None, node_name: str, node_usage: dict) -> dict:
    """Merge/overwrite node usage and recompute totals."""
    merged = deepcopy(metadata) if isinstance(metadata, dict) else empty_token_usage_metadata()
    merged.setdefault(
        "pricing",
        {"input_per_1m_usd": INPUT_PRICE_PER_1M_USD, "output_per_1m_usd": OUTPUT_PRICE_PER_1M_USD},
    )
    nodes = merged.setdefault("nodes", {})
    nodes[node_name] = node_usage

    total_in = total_out = total_tools = 0
    for usage in nodes.values():
        total_in += _as_int(usage.get("input_tokens", 0))
        total_out += _as_int(usage.get("output_tokens", 0))
        total_tools += _as_int(usage.get("tool_calls", 0))

    # Request-level total tokens must equal summed input+output across nodes.
    total_all = total_in + total_out
    total_input_cost = _cost_for_tokens(total_in, is_input=True)
    total_output_cost = _cost_for_tokens(total_out, is_input=False)

    merged["totals"] = {
        "input_tokens": total_in,
        "output_tokens": total_out,
        "total_tokens": total_all,
        "tool_calls": total_tools,
        "total_input_token_cost_usd": total_input_cost,
        "total_output_token_cost_usd": total_output_cost,
        "total_token_cost_usd": round(total_input_cost + total_output_cost, 8),
    }
    return merged


def extract_token_usage_from_messages(messages: list, fallback_model_name: str = "unknown") -> tuple[int, int, int, str]:
    """Extract aggregate token usage from AI messages in a LangChain conversation."""
    input_tokens = output_tokens = total_tokens = 0
    model_name = fallback_model_name

    for msg in messages:
        usage = _extract_usage_dict(msg)
        if usage:
            input_tokens += _pick_usage_value(usage, "input_tokens", "prompt_tokens")
            output_tokens += _pick_usage_value(usage, "output_tokens", "completion_tokens")
            total_tokens += _pick_usage_value(usage, "total_tokens")
        model_name = _extract_model_name(msg, model_name)

    if total_tokens == 0:
        total_tokens = input_tokens + output_tokens
    return input_tokens, output_tokens, total_tokens, model_name


def count_tool_calls_by_name(tool_results: list[dict]) -> tuple[int, dict]:
    """Return total tool calls and per-tool counters from tool result entries."""
    counters: dict[str, int] = {}
    for item in tool_results:
        name = str(item.get("tool", "unknown"))
        counters[name] = counters.get(name, 0) + 1
    return sum(counters.values()), counters


def _extract_usage_dict(message) -> dict | None:
    usage = getattr(message, "usage_metadata", None)
    if isinstance(usage, dict) and usage:
        return usage

    response_meta = getattr(message, "response_metadata", None)
    if not isinstance(response_meta, dict):
        return None

    for key in ("token_usage", "usage", "usage_metadata"):
        value = response_meta.get(key)
        if isinstance(value, dict) and value:
            return value
    return None


def _extract_model_name(message, fallback: str) -> str:
    response_meta = getattr(message, "response_metadata", None)
    if isinstance(response_meta, dict):
        for key in ("model_name", "model", "model_id"):
            value = response_meta.get(key)
            if value:
                return str(value)
    return fallback


def _pick_usage_value(usage: dict, *keys: str) -> int:
    for key in keys:
        if key in usage:
            return _as_int(usage.get(key, 0))
    return 0


def _as_int(value) -> int:
    try:
        return int(value or 0)
    except Exception:
        return 0


def _cost_for_tokens(token_count: int, *, is_input: bool) -> float:
    tokens = _as_int(token_count)
    price_per_1m = INPUT_PRICE_PER_1M_USD if is_input else OUTPUT_PRICE_PER_1M_USD
    return round((tokens / 1_000_000) * price_per_1m, 8)
