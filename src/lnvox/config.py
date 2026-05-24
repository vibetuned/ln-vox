import os
from pathlib import Path

from pydantic import BaseModel
from pydantic_settings import BaseSettings, SettingsConfigDict


class LLMConfig(BaseModel):
    endpoint: str = "http://localhost:8000/v1"
    model: str = "google/gemma-4-E4B-it"
    api_key: str = "EMPTY"
    temperature: float = 0.2
    max_tokens: int = 8192
    # HTTP request timeout. Computed per-call as:
    #   timeout = base_seconds + max_tokens * seconds_per_token
    # Defaults are tuned for the slow case (Gemma 4 31B on DGX Spark at
    # ~6 tok/s). For faster setups (e.g. E4B on a 4090 at 100+ tok/s) you
    # can lower `timeout_seconds_per_token` to avoid sitting on dead
    # connections; the floor `timeout_base_seconds` still applies.
    timeout_base_seconds: float = 90.0
    timeout_seconds_per_token: float = 0.25


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="LNVOX_",
        env_nested_delimiter="__",
        env_file=".env",
        extra="ignore",
    )

    artifacts_dir: Path = Path("artifacts")
    llm: LLMConfig = LLMConfig()

    def model_post_init(self, _ctx) -> None:
        # Shorthand env-var aliases so the same vars work for both
        # `serve_vllm.sh` and the python client. Pydantic-settings normally
        # expects nested config via double-underscore (`LNVOX_LLM__MODEL`),
        # but the serve script (and most users) write single-underscore.
        # The shorthand wins only if the nested form wasn't explicitly set.
        for shorthand, attr in (
            ("LNVOX_LLM_MODEL", "model"),
            ("LNVOX_LLM_ENDPOINT", "endpoint"),
            ("LNVOX_LLM_API_KEY", "api_key"),
        ):
            value = os.environ.get(shorthand)
            if value and getattr(self.llm, attr) == LLMConfig.model_fields[attr].default:
                setattr(self.llm, attr, value)
