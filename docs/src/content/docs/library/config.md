---
title: lnvox.config
description: Settings and LLMConfig — every field and default, the LNVOX_ env mapping, shorthand aliases and their precedence, and the per-call timeout formula.
sidebar:
  order: 2
  label: lnvox.config
---

`lnvox.config` (`src/lnvox/config.py`) holds the process-wide configuration: where artifacts and the voicebank live, and how to reach the OpenAI-compatible LLM server. Every CLI command that touches the LLM builds an [`LLMClient`](/library/llm/client/), which builds a `Settings()`. `cli._book_dir()` also builds one to resolve `artifacts/<book_id>/`, and `cli._voicebank_dir()` builds one to resolve the voicebank. You change it through environment variables (or a `.env` file), never through CLI flags.

## At a glance

| Name | Kind | Purpose |
| --- | --- | --- |
| [`Settings`](#settings) | `pydantic_settings.BaseSettings` | Top-level settings: `artifacts_dir`, `voicebank` and the nested `llm` block. Reads `LNVOX_*` env vars and `.env`. |
| [`LLMConfig`](#llmconfig) | `pydantic.BaseModel` | Endpoint, model name, sampling and token-budget knobs, HTTP timeout coefficients. |
| [`Settings.model_post_init`](#shorthand-aliases-model_post_init) | hook | Applies the single-underscore shorthand env vars (`LNVOX_LLM_MODEL`, …) that the serve scripts share, from the environment or `.env`. |

## API

### `LLMConfig`

```python
class LLMConfig(BaseModel):
    endpoint: str = "http://localhost:8000/v1"
    model: str = "google/gemma-4-12B-it-qat-q4_0-gguf"
    api_key: str = "EMPTY"
    temperature: float = 0.2
    max_tokens: int = 8192
    repetition_penalty: float = 1.0
    max_model_len: int = 65536
    timeout_base_seconds: float = 90.0
    timeout_seconds_per_token: float = 0.25
```

| Field | Type | Default | Meaning |
| --- | --- | --- | --- |
| `endpoint` | `str` | `"http://localhost:8000/v1"` | Base URL handed to `openai.OpenAI(base_url=…)`. All three serve scripts listen on port 8000 by default. |
| `model` | `str` | `"google/gemma-4-12B-it-qat-q4_0-gguf"` | Sent as the request's `model`. llama-server and mlx-lm ignore it (they serve whatever they loaded); vLLM validates it strictly, so it must equal the served model id. The launcher exports the model it serves as both `LNVOX_LLM_MODEL` and `LNVOX_LLM__MODEL`. |
| `api_key` | `str` | `"EMPTY"` | Bearer token for the server. Local servers don't check it. |
| `temperature` | `float` | `0.2` | Base sampling temperature. [`LLMClient.structured`](/library/llm/client/#structured) raises it by 0.4 per schema-invalid retry, up to a cap of 1.0. |
| `max_tokens` | `int` | `8192` | Output budget used when a caller passes no `max_tokens`. Most stages pass their own budget. |
| `repetition_penalty` | `float` | `1.0` | `1.0` means off (nothing is sent). Any other value is sent as `repetition_penalty`, and also as `repeat_penalty` on llama-server. This is the fix for a quantized model that loops under guided JSON; `1.1` is a mild setting to start with. |
| `max_model_len` | `int` | `65536` | The **served** model's context window. [`budget_for`](/library/llm/client/#budget_for) and the length-cut budget growth use it to keep prompt + output inside the context. It must match the server. The launcher's `--max-model-len` sets it (it exports all three context-length names), and the serve scripts' `LNVOX_LLM_MAX_LEN` is picked up as a fallback (see [shorthand aliases](#shorthand-aliases-model_post_init) and the note below). |
| `timeout_base_seconds` | `float` | `90.0` | Fixed part of the per-call HTTP timeout. |
| `timeout_seconds_per_token` | `float` | `0.25` | Per-output-token part of the per-call HTTP timeout. |

#### Per-call timeout

The OpenAI client is built **without** a global timeout. Each request computes its own:

```text
timeout = timeout_base_seconds + max_tokens * timeout_seconds_per_token
```

`max_tokens` is the budget of *that attempt*, so the timeout grows when the client enlarges the budget after a length cut. With the defaults:

| Call | `max_tokens` | Timeout |
| --- | --- | --- |
| Chunker break choice | 512 | 218 s |
| Default budget | 8192 | 2138 s (~36 min) |
| s1 per-chapter extraction (desired) | 24576 | 6234 s (~1 h 44 min) |
| s2 beat tagging (cap) | 32000 | 8090 s (~2 h 15 min) |

The defaults were tuned for the slow case (Gemma 4 31B on a DGX Spark at about 6 tok/s). On a fast GPU you can lower `timeout_seconds_per_token` (e.g. `LNVOX_LLM__TIMEOUT_SECONDS_PER_TOKEN=0.05`) so a dead connection is noticed sooner. On a very slow host, raise it to 0.35–0.5.

:::note
`LLMClient` builds the OpenAI client with `max_retries=0`, so the SDK never re-sends a timed-out request. One request waits at most the timeout above. Connection errors and HTTP 503 are retried by `structured()` itself, with a short backoff; timeouts never are. See [retry semantics](/library/llm/client/#transport-retries).
:::

### `Settings`

```python
class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="LNVOX_",
        env_nested_delimiter="__",
        env_file=".env",
        extra="ignore",
    )

    artifacts_dir: Path = Path("artifacts")
    voicebank: Path = Path("voicebank")
    llm: LLMConfig = LLMConfig()
```

| Field | Type | Default | Meaning |
| --- | --- | --- | --- |
| `artifacts_dir` | `Path` | `Path("artifacts")` | Root of all per-book outputs; `cli._book_dir(book_id)` returns `artifacts_dir / book_id`. Relative to the current working directory. |
| `voicebank` | `Path` | `Path("voicebank")` | The voicebank directory (`manifest.json` + `clips/`) that `cli._voicebank_dir()` returns, e.g. `voicebank-fr` for a French bank (DESIGN.md §17.5). Relative to the current working directory. |
| `llm` | [`LLMConfig`](#llmconfig) | `LLMConfig()` | LLM connection and budget settings. Each `Settings()` gets its own copy, so shorthand overrides never leak into the class default. |

How the environment maps onto fields (pydantic-settings; names are case-insensitive):

| Env var | Sets |
| --- | --- |
| `LNVOX_ARTIFACTS_DIR` | `artifacts_dir` |
| `LNVOX_VOICEBANK` | `voicebank` |
| `LNVOX_LLM__ENDPOINT`, `LNVOX_LLM__MODEL`, `LNVOX_LLM__API_KEY`, `LNVOX_LLM__TEMPERATURE`, `LNVOX_LLM__MAX_TOKENS`, `LNVOX_LLM__REPETITION_PENALTY`, `LNVOX_LLM__MAX_MODEL_LEN`, `LNVOX_LLM__TIMEOUT_BASE_SECONDS`, `LNVOX_LLM__TIMEOUT_SECONDS_PER_TOKEN` | the matching `llm.*` field (nested delimiter `__`) |
| `LNVOX_LLM` | the whole `llm` block as JSON, e.g. `LNVOX_LLM='{"endpoint":"http://gpu-box:8000/v1"}'` |

A `.env` file in the **current working directory** is read too, with the same names. Unknown `LNVOX_*` names are ignored (`extra="ignore"`), so a typo fails silently.

### Shorthand aliases (`model_post_init`)

```python
def model_post_init(self, _ctx) -> None
```

The serve scripts and most users write single-underscore names (`LNVOX_LLM_MODEL`), which pydantic-settings wouldn't map to a nested field. After normal loading, `model_post_init` applies five shorthands, reading each from the process environment first and then from the `.env` file(s) named by `model_config["env_file"]`:

| Shorthand | Field | Parse |
| --- | --- | --- |
| `LNVOX_LLM_MODEL` | `llm.model` | string |
| `LNVOX_LLM_ENDPOINT` | `llm.endpoint` | string |
| `LNVOX_LLM_API_KEY` | `llm.api_key` | string |
| `LNVOX_LLM_MAX_MODEL_LEN`, then `LNVOX_LLM_MAX_LEN` | `llm.max_model_len` | `int(...)` |
| `LNVOX_LLM_REPETITION_PENALTY` | `llm.repetition_penalty` | `float(...)` |

`LNVOX_LLM_MAX_LEN` is the serve scripts' name for the context window. The launcher's `--max-model-len` exports it together with `LNVOX_LLM_MAX_MODEL_LEN` and `LNVOX_LLM__MAX_MODEL_LEN`, so under the launcher the fallback isn't needed. It matters for a serve script run standalone with only `LNVOX_LLM_MAX_LEN` exported: the client's budget clamp still sees the same value as the server.

**Precedence rule.** A nested field that was **explicitly set** always wins. Explicit means pydantic recorded it in `llm.model_fields_set`: an `LNVOX_LLM__<FIELD>` variable in the environment or `.env`, the `LNVOX_LLM` JSON block, or an init argument. This holds even when the value equals the default. Otherwise the first usable candidate wins, in this order:

1. The shorthand from the process environment (for `max_model_len`: `LNVOX_LLM_MAX_MODEL_LEN`, then `LNVOX_LLM_MAX_LEN`).
2. The same names from `.env`.
3. The `LLMConfig` default.

Empty values are skipped. So is a value that doesn't parse (e.g. `LNVOX_LLM_MAX_MODEL_LEN=32k`); the next candidate is tried, and no warning is printed. The process environment beats `.env`, as it does for pydantic's own fields. An explicit `LNVOX_LLM__MAX_MODEL_LEN` overrides both shorthands, which is why the launcher's `--max-model-len` exports the nested name too: its value then wins over anything left in your shell or `.env`.

The `.env` lookup follows `model_config["env_file"]`, not a per-call `Settings(_env_file=…)` override. Nothing in the repo passes one.

There is **no** shorthand for `temperature`, `max_tokens` or the timeout fields. Use `LNVOX_LLM__TEMPERATURE`, and so on. `LNVOX_LLM_TEMP` is read only by `scripts/serve_mlx.sh` as the *server's* sampling temperature. It doesn't change the client.

## Notes

:::caution[A context the client can't see]
The client learns the server's context window only from `LNVOX_LLM__MAX_MODEL_LEN`, `LNVOX_LLM_MAX_MODEL_LEN` or `LNVOX_LLM_MAX_LEN`. Under the launcher that is taken care of: `run_pipeline.sh --max-model-len N` exports all three, and when the launcher itself starts `serve_vllm.sh` for a 31B/27B model with no context length set, it exports that script's 20480 default under all three names. A window chosen *inside* a serve script you started yourself (or on an external `--vllm-url` server) doesn't reach the client: `serve_vllm.sh` gives a 31B/27B model 20480 tokens when `LNVOX_LLM_MAX_LEN` is unset, while the client keeps assuming 65536. Budgets are then not clamped and large calls fail with HTTP 400 "maximum context length". Export `LNVOX_LLM_MAX_LEN` for both (or pass `--max-model-len` to the launcher) whenever the server's context is smaller than 65536. See [Environment variables](/reference/environment/#llm-context-length-three-names).
:::

- There is no cached singleton. `Settings()` is rebuilt on every `LLMClient()` (unless you pass one in) and every `_book_dir()` / `_voicebank_dir()` call, so an environment change made inside the process takes effect at the next construction.
- `artifacts_dir` is honoured by the `lnvox` CLI and by `scripts/voicebank_studio.py` (`--artifacts` default, resolved through `Settings`). `scripts/run_pipeline.sh` and `scripts/s4_retry.sh` honour `LNVOX_ARTIFACTS_DIR` too, but read it from the process environment only: a value kept only in `.env` moves the stages' output while the launcher's checks still look in `artifacts/`. Export it when you use the launcher.
- `voicebank` from `.env` is honoured by the `lnvox` CLI and the Studio (`--voicebank` default, through `Settings`; unset anywhere, the Studio falls back to `<repo>/voicebank`). The ComfyUI pack reads `LNVOX_VOICEBANK` from the process environment. The launcher uses it only in its `--stop-after-audit` hint, which repeats it when it is exported; its stages inherit whatever is exported (or find it in `.env`).
- `OPENAI_API_KEY` / `OPENAI_BASE_URL` have no effect: `LLMClient` always passes `base_url` and `api_key` explicitly from this config.
- Backend selection (`LNVOX_LLM_BACKEND`) is **not** a `Settings` field. `LLMClient` reads it straight from `os.environ` on every call, so a `.env` line doesn't set it. See [lnvox.llm.client](/library/llm/client/#backend-specific-request-fields).
