"""
FreeClawRouter – config.py
Loads and validates config.yaml (with optional config.local.yaml overlay).
Environment variables are substituted for ${VAR_NAME} placeholders in api_key fields.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml
from dotenv import load_dotenv

load_dotenv()  # load .env from cwd or any parent directory

_ENV_VAR_RE = re.compile(r"^\$\{([^}]+)\}$")


def _resolve_env(value: str) -> str:
    """Expand ${VAR_NAME} → os.environ value, or return '' if unset."""
    m = _ENV_VAR_RE.match(value or "")
    if m:
        return os.environ.get(m.group(1), "")
    return value


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class ModelConfig:
    id: str
    context_window: int
    description: str = ""
    priority: int = 5       # 1 = highest priority
    tags: list[str] = field(default_factory=list)
    # True if the model allocates internal chain-of-thought tokens before
    # replying (e.g. DeepSeek R1, Gemini 2.5 Pro thinking, Qwen3 thinking).
    # The router uses this to prefer reasoning models for complex tasks and
    # avoid wasting their slower throughput on trivial requests.
    is_reasoning: bool = False


@dataclass
class RateLimits:
    rpm: Optional[int] = None   # requests per minute
    rpd: Optional[int] = None   # requests per day
    tpm: Optional[int] = None   # tokens per minute
    tpd: Optional[int] = None   # tokens per day


@dataclass
class ProviderConfig:
    name: str
    base_url: str
    api_key: str
    models: list[ModelConfig]
    rate_limits: RateLimits
    priority: int = 5
    enabled: bool = True


@dataclass
class OllamaConfig:
    base_url: str = "http://ollama:11434"
    router_model: str = "gpt-oss:20b"
    fallback_model: str = "gpt-oss:20b"
    fallback_enabled: bool = True


@dataclass
class ProxyConfig:
    port: int = 8765
    host: str = "0.0.0.0"
    log_level: str = "info"
    output_token_reserve: int = 4096
    memory_warning_threshold: float = 0.80
    memory_critical_threshold: float = 0.90
    local_only_threshold: str = "simple"  # disabled | simple | moderate | always


@dataclass
class AppConfig:
    providers: list[ProviderConfig]
    local: OllamaConfig
    proxy: ProxyConfig

    def get_provider(self, name: str) -> Optional[ProviderConfig]:
        for p in self.providers:
            if p.name == name:
                return p
        return None

    def get_model(self, provider_name: str, model_id: str) -> Optional[ModelConfig]:
        p = self.get_provider(provider_name)
        if p:
            for m in p.models:
                if m.id == model_id:
                    return m
        return None

    def all_provider_models(self) -> list[tuple[ProviderConfig, ModelConfig]]:
        """Return all (provider, model) pairs sorted by provider priority then model priority."""
        pairs = []
        for p in self.providers:
            for m in p.models:
                pairs.append((p, m))
        pairs.sort(key=lambda pm: (pm[0].priority, pm[1].priority))
        return pairs


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------

def _load_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    with open(path) as f:
        return yaml.safe_load(f) or {}


def _deep_merge(base: dict, overlay: dict) -> dict:
    """Recursively merge overlay into base (overlay wins on conflicts)."""
    result = dict(base)
    for k, v in overlay.items():
        if k in result and isinstance(result[k], dict) and isinstance(v, dict):
            result[k] = _deep_merge(result[k], v)
        else:
            result[k] = v
    return result


def load_config(config_dir: str | Path = ".") -> AppConfig:
    config_dir = Path(config_dir)
    raw = _deep_merge(
        _load_yaml(config_dir / "config.yaml"),
        _load_yaml(config_dir / "config.local.yaml"),
    )

    providers: list[ProviderConfig] = []
    for name, pdata in raw.get("providers", {}).items():
        if not pdata.get("enabled", True):
            continue

        api_key = _resolve_env(pdata.get("api_key", ""))
        if not api_key:
            # Skip providers without a configured API key; log at startup
            continue

        models = [
            ModelConfig(
                id=m["id"],
                context_window=m.get("context_window", 32768),
                description=m.get("description", ""),
                priority=m.get("priority", 5),
                tags=m.get("tags", []),
                is_reasoning=m.get("is_reasoning", False),
            )
            for m in pdata.get("models", [])
        ]

        rl = pdata.get("rate_limits", {})
        rate_limits = RateLimits(
            rpm=rl.get("rpm"),
            rpd=rl.get("rpd"),
            tpm=rl.get("tpm"),
            tpd=rl.get("tpd"),
        )

        providers.append(ProviderConfig(
            name=name,
            base_url=pdata["base_url"].rstrip("/"),
            api_key=api_key,
            models=models,
            rate_limits=rate_limits,
            priority=pdata.get("priority", 5),
            enabled=True,
        ))

    providers.sort(key=lambda p: p.priority)

    local_raw = raw.get("local", {}).get("ollama", {})
    local = OllamaConfig(
        base_url=local_raw.get("base_url", "http://ollama:11434"),
        router_model=local_raw.get("router_model", "gpt-oss:20b"),
        fallback_model=local_raw.get("fallback_model", "gpt-oss:20b"),
        fallback_enabled=local_raw.get("fallback_enabled", True),
    )

    proxy_raw = raw.get("proxy", {})
    proxy = ProxyConfig(
        port=proxy_raw.get("port", 8765),
        host=proxy_raw.get("host", "0.0.0.0"),
        log_level=proxy_raw.get("log_level", "info"),
        output_token_reserve=proxy_raw.get("output_token_reserve", 4096),
        memory_warning_threshold=proxy_raw.get("memory_warning_threshold", 0.80),
        memory_critical_threshold=proxy_raw.get("memory_critical_threshold", 0.90),
        local_only_threshold=proxy_raw.get("local_only_threshold", "simple"),
    )

    return AppConfig(providers=providers, local=local, proxy=proxy)
