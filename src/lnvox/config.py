import os
from pathlib import Path
from typing import Any, Callable, ClassVar

from dotenv import dotenv_values
from pydantic import BaseModel
from pydantic_settings import BaseSettings, SettingsConfigDict


class LLMConfig(BaseModel):
    endpoint: str = "http://localhost:8000/v1"
    # Matches the default backend (llama.cpp, serve_llama.sh). llama-server
    # and mlx ignore the request's model name; vLLM validates it strictly —
    # the launcher exports the right LNVOX_LLM_MODEL per backend.
    model: str = "google/gemma-4-12B-it-qat-q4_0-gguf"
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
    # ignoring this. The launcher's `--max-model-len` exports all three
    # spellings (LNVOX_LLM_MAX_LEN, LNVOX_LLM_MAX_MODEL_LEN and nested
    # LNVOX_LLM__MAX_MODEL_LEN); the serve scripts' `LNVOX_LLM_MAX_LEN` is
    # still picked up as a fallback, so a serve script started standalone
    # and the client agree without setting two names.
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
    # DESIGN.md §17.5: the voicebank (manifest.json + clips/) is selected
    # once via `LNVOX_VOICEBANK` (e.g. `voicebank-fr`), not per-command
    # flags. A Settings field rather than a raw os.environ read so `.env`
    # works for it like every other LNVOX_ variable.
    voicebank: Path = Path("voicebank")
    llm: LLMConfig = LLMConfig()

    # Shorthand env-var aliases → (llm field, parser). Names are tried in
    # order; LNVOX_LLM_MAX_LEN is the serve scripts' spelling of the context
    # window, so it is the fallback when the client-side name isn't set. (The
    # launcher's `--max-model-len` exports it alongside LNVOX_LLM_MAX_MODEL_LEN
    # and LNVOX_LLM__MAX_MODEL_LEN; the fallback matters for a serve script
    # run standalone with only LNVOX_LLM_MAX_LEN exported.)
    _SHORTHANDS: ClassVar[tuple[tuple[tuple[str, ...], str, Callable[[str], Any]], ...]] = (
        (("LNVOX_LLM_MODEL",), "model", str),
        (("LNVOX_LLM_ENDPOINT",), "endpoint", str),
        (("LNVOX_LLM_API_KEY",), "api_key", str),
        (("LNVOX_LLM_MAX_MODEL_LEN", "LNVOX_LLM_MAX_LEN"), "max_model_len", int),
        (("LNVOX_LLM_REPETITION_PENALTY",), "repetition_penalty", float),
    )

    def _dotenv_values(self) -> dict[str, str | None]:
        """The `.env` file(s) pydantic-settings itself reads, merged in order.

        pydantic-settings only maps FIELD names out of `.env` (and drops the
        rest under extra="ignore"), so the shorthands have to be read from
        the same file(s) by hand. Relative paths resolve against the CWD,
        exactly as pydantic-settings resolves them.
        """
        env_file = self.model_config.get("env_file")
        if not env_file:
            return {}
        files = [env_file] if isinstance(env_file, (str, os.PathLike)) else list(env_file)
        encoding = self.model_config.get("env_file_encoding") or "utf8"
        merged: dict[str, str | None] = {}
        for f in files:
            path = Path(f).expanduser()
            if path.is_file():
                merged.update(dotenv_values(path, encoding=encoding))
        return merged

    def model_post_init(self, _ctx) -> None:
        # Shorthand env-var aliases so the same vars work for both the serve
        # scripts and the python client. Pydantic-settings normally expects
        # nested config via double-underscore (`LNVOX_LLM__MODEL`), but the
        # serve scripts (and most users) write single-underscore.
        #
        # Precedence: an explicitly set nested field (init kwarg, env or
        # `.env` — anything pydantic recorded in `model_fields_set`) always
        # wins, even when its value equals the default; otherwise the
        # shorthand from the process environment; otherwise from `.env`
        # (process env beats `.env`, as it does for pydantic's own fields).
        # Within one source, earlier names in _SHORTHANDS win.
        explicit = set(self.llm.model_fields_set)
        dotenv: dict[str, str | None] | None = None  # read lazily, once

        def candidates(names: tuple[str, ...]):
            nonlocal dotenv
            for n in names:
                yield os.environ.get(n)
            if dotenv is None:
                dotenv = self._dotenv_values()
            for n in names:
                yield dotenv.get(n)

        for names, attr, parse in self._SHORTHANDS:
            if attr in explicit:
                continue
            for raw in candidates(names):
                if not raw:
                    continue
                try:
                    value = parse(raw)
                except ValueError:
                    continue  # unparsable → next candidate, else the default
                setattr(self.llm, attr, value)
                break
