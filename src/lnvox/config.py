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
    # Sent to vLLM as a sampling param. >1.0 discourages token repetition —
    # the lever for a weak/quantized model that loops under guided JSON
    # (emitting endless duplicate array items until it hits max_tokens). 1.0 is
    # a no-op (default, no behaviour change for well-behaved models); 1.1 is a
    # gentle, commonly-safe value to try when extraction runs away.
    repetition_penalty: float = 1.0
    # The served model's context window (vLLM `--max-model-len`). Used to clamp
    # per-call output budgets so prompt + output never exceeds it — both an
    # over-large output request (input+output > context → 400) and an
    # over-small one (output truncated mid-JSON → parse error) come from
    # ignoring this. Set `LNVOX_LLM_MAX_MODEL_LEN` to match the serve script.
    max_model_len: int = 65536
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

        max_len = os.environ.get("LNVOX_LLM_MAX_MODEL_LEN")
        if max_len and self.llm.max_model_len == LLMConfig.model_fields["max_model_len"].default:
            try:
                self.llm.max_model_len = int(max_len)
            except ValueError:
                pass

        rep = os.environ.get("LNVOX_LLM_REPETITION_PENALTY")
        if rep and self.llm.repetition_penalty == LLMConfig.model_fields["repetition_penalty"].default:
            try:
                self.llm.repetition_penalty = float(rep)
            except ValueError:
                pass
