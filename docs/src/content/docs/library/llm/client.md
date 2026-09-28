---
title: lnvox.llm.client
description: LLMClient — prompt rendering, context-aware token budgets, and the structured JSON call with its validation and transport retries, feedback, salvage and per-backend request fields.
sidebar:
  order: 1
  label: client
---

`lnvox.llm.client` (`src/lnvox/llm/client.py`) is the pipeline's only way to reach an LLM. Every LLM-backed stage (s1, s2, s3, lecture, scenario, `ingest-scenario`, voice cast, and the lecture-mode block classifier in `ingest-epub`) builds an `LLMClient`. It renders a [prompt template](/library/llm/prompts/), sizes the output budget, and calls [`structured()`](#structured) to get back a validated [pydantic schema](/library/llm/schemas/). The server is any OpenAI-compatible `/v1/chat/completions` endpoint: llama-server (default), vLLM or mlx-lm, started by the [launcher](/launcher/run-pipeline/).

## At a glance

| Member | Purpose |
| --- | --- |
| [`LLMClient(settings=None)`](#llmclient) | Wraps an `openai.OpenAI` client built from [`Settings().llm`](/library/config/#llmconfig). |
| [`render(template, **kwargs)`](#render) | Renders a Jinja template from `src/lnvox/llm/prompts/`. |
| [`budget_for(*, system, user, desired, floor=2048, reserve=1024)`](#budget_for) | Output-token budget clamped to the context window. |
| [`structured(*, system, user, schema, max_tokens=None, attempts=3, salvage=None)`](#structured) | One JSON call with validation, retries, budget growth and optional salvage. |
| [`is_transport_error(exc)`](#is_transport_error) | Module function: is this exception a server/transport failure rather than a bad answer? |
| [`_timeout_for(max_tokens)`](#_timeout_for) | Per-call HTTP timeout. |
| [`_extract_json(text)`](#_extract_json) | Strips code fences / surrounding prose before validation. |
| `_jinja`, `_PROMPTS_DIR` | [Template environment](#template-environment). |

## API

### `LLMClient`

```python
class LLMClient:
    def __init__(self, settings: Settings | None = None)
```

Stores `settings or Settings()` as `self.settings`, and builds `self.client = OpenAI(base_url=settings.llm.endpoint, api_key=settings.llm.api_key, max_retries=0)`. No global timeout is set because the right one depends on each call's `max_tokens` (an s2 beat call may want 32k tokens, a voice-match call about 1k). `structured()` computes a timeout per request.

`max_retries=0` turns off the SDK's own retries. With the SDK default (2) a timed-out generation was silently re-sent, so one attempt could wait about 3× its carefully computed timeout. `structured()` does its own, narrower [transport retries](#transport-retries) instead.

Constructing the client makes no network call. A wrong endpoint surfaces on the first `structured()`. The CLI prints `endpoint=… model=…` right after construction so you can confirm which server a stage talks to.

**Attributes:** `settings` ([`Settings`](/library/config/#settings)) and `client` (`openai.OpenAI`, with SDK defaults apart from base URL, key and `max_retries=0`).

**Class attribute:** `_TRANSPORT_RETRY_BACKOFF_S = (2.0, 5.0)`, the sleep before each transport retry. Its length is the number of retries.

### `render`

```python
def render(self, template: str, **kwargs) -> str
```

Returns `_jinja.get_template(template).render(**kwargs)`. `template` is a file name such as `"scene_beats.jinja"`. Because the environment uses `StrictUndefined`, a missing variable raises `jinja2.UndefinedError` rather than rendering an empty string. `ingest/blocks.py` receives this bound method as its `render_template` callback. See [prompt templates](/library/llm/prompts/) for each template's variables.

### `budget_for`

```python
def budget_for(
    self,
    *,
    system: str,
    user: str,
    desired: int,
    floor: int = 2048,
    reserve: int = 1024,
) -> int
```

Returns an output-token budget that keeps prompt + output inside `settings.llm.max_model_len`:

```python
est_input = int((len(system) + len(user)) / 3.5)   # _CHARS_PER_TOKEN
available = max_model_len - est_input - reserve
return max(floor, min(desired, available))
```

- `desired` — what the caller would ideally ask for.
- `floor` — lower bound, default `2048`. It wins even when it exceeds `available`, so a huge prompt can still overflow the context and get a 400. That's deliberate: a too-small budget would truncate the JSON anyway.
- `reserve` — tokens held back for chat-template overhead, default `1024`.

`_CHARS_PER_TOKEN = 3.5` is a conservative class constant (real English + JSON runs about 3.5–4 chars per token). Overestimating the input leaves headroom.

:::caution
`budget_for` is only as good as `max_model_len`. If the server's context is smaller than the client believes (the default is 65536), the clamp does nothing. The launcher's `--max-model-len` reaches the client (it exports `LNVOX_LLM_MAX_MODEL_LEN` and `LNVOX_LLM__MAX_MODEL_LEN` alongside `LNVOX_LLM_MAX_LEN`), and so does the 20480 window of a 31B/27B model on a `serve_vllm.sh` the launcher started. A default chosen inside a serve script you started yourself doesn't. See [lnvox.config](/library/config/#notes).
:::

### `_timeout_for`

```python
def _timeout_for(self, max_tokens: int) -> float
```

`timeout_base_seconds + max_tokens * timeout_seconds_per_token`, 90 s + 0.25 s/token by default. It's linear because the server may have to generate all `max_tokens` before replying. See [the timeout table](/library/config/#per-call-timeout).

### `_extract_json`

```python
def _extract_json(text: str) -> str
```

Gemma sometimes wraps JSON in fences even when told not to. If the whole text is one fenced block (optionally tagged `json`/`JSON`), the inner body is returned. Otherwise the text is sliced from the first `{` or `[` to the last `}` or `]`. If there are no brackets, it's returned stripped. It doesn't repair broken JSON.

### `structured`

```python
def structured(
    self,
    *,
    system: str,
    user: str,
    schema: Type[T],
    max_tokens: int | None = None,
    attempts: int = 3,
    salvage: Callable[[str], T | None] | None = None,
) -> T
```

Sends one system + user chat, parses the reply as JSON and validates it with `schema.model_validate_json`. It retries on invalid output and returns an instance of `schema`.

**Parameters**

- `system`, `user` — message contents. Callers put the fixed role instruction in `system` and the rendered template in `user`.
- `schema` — the pydantic model to validate against. Its `model_json_schema()` is also sent as `guided_json`.
- `max_tokens` — starting output budget. Defaults to `settings.llm.max_tokens` (8192). It may grow on retry (see below).
- `attempts` — total requests, default `3`. No caller overrides it.
- `salvage` — optional last resort. When every attempt fails, it's called with the last raw text and may return a partial `T` (for example, the complete records of a truncated array). Exceptions inside `salvage` are swallowed and count as `None`.

**Returns** a validated `schema` instance, or whatever `salvage` returned.

**Raises**

- `ValueError` once attempts are exhausted (or the budget hit the context ceiling) and salvage is absent or returned `None`. The message names the schema, the attempt count and the last validation error, plus a diagnostics line and the first 400 characters of raw output:

  ```text
  --- diagnostics ---
  finish_reason=length, prompt_tokens=…, completion_tokens=…, max_tokens=…, content_chars=…, reasoning_chars=…
  ```

  `finish_reason=length` means the budget was too small (raise it or chunk the input). `stop` with invalid JSON means the model ended mid-object, a model-quality problem that is often transient. `content_chars=0` with a large `reasoning_chars` means the model spent the whole budget in its thinking channel.
- `openai` exceptions propagate, and salvage is never tried for them:
  - `APIConnectionError` (connection refused or reset) and HTTP 503 propagate after the [transport retries](#transport-retries) run out.
  - `APITimeoutError`, `BadRequestError` (e.g. context overflow), other 5xx and every other `openai` error propagate **immediately**.

  After that, the launcher's step retry with `ensure_vllm` restarts a dead server.

#### Request shape

Every attempt sends:

```python
self.client.chat.completions.create(
    model=settings.llm.model,
    messages=[{"role": "system", "content": system},
              {"role": "user", "content": user_msg}],
    temperature=temperature,
    max_tokens=effective_max,
    response_format={"type": "json_object"},
    extra_body=extra_body,                # see below
    timeout=self._timeout_for(effective_max),
)
```

`extra_body["guided_json"]` is always the schema's JSON Schema. vLLM enforces it as a grammar, which is why `maxItems` hints in the [schemas](/library/llm/schemas/#maxitems-caps) matter there. llama-server and mlx-lm ignore it and rely on the prompt plus this retry loop.

#### Backend-specific request fields

`LNVOX_LLM_BACKEND` is read from `os.environ` on **every** call (not from `Settings`). **Unset means `llama`**, the pipeline-wide default backend. `run_pipeline.sh` always exports it. When you run a stage by hand against vLLM, export `LNVOX_LLM_BACKEND=vllm` yourself.

| Field in `extra_body` | llama (or unset) | mlx | vllm |
| --- | --- | --- | --- |
| `guided_json` | sent (ignored) | sent (ignored) | sent (enforced) |
| `repetition_penalty` (only if `repetition_penalty != 1.0`) | sent | sent (no effect on mlx-lm) | sent |
| `repeat_penalty` (only if `repetition_penalty != 1.0`) | sent (llama-server's spelling) | — | — |
| `chat_template_kwargs: {"enable_thinking": False}` | sent | sent | — |

**Why thinking is disabled.** The Gemma 4 GGUF chat template on llama-server (and the mlx-community Gemma 4 templates) enables thinking by default. On structured-JSON tasks the model then spends its entire budget in the thought channel and `content` comes back empty. The flag that works is the per-request `enable_thinking: False` (`thinking: false` is ignored). Templates without that variable ignore the field, but on vLLM the client leaves the model's template defaults alone.

#### Response parsing

The client reads `choices[0].message.content`. If that's empty, it falls back to `reasoning_content`, because some llama.cpp setups (`--jinja` with a template that declares a reasoning channel) route the whole reply there. The text is then passed through [`_extract_json`](#_extract_json) and validated.

#### Retry semantics

Each failed attempt is classified by `finish_reason`:

| Previous failure | Next attempt's budget | Next attempt's temperature | Next attempt's prompt |
| --- | --- | --- | --- |
| Schema-invalid, `finish_reason != "length"` | unchanged | `min(1.0, temperature + 0.4 × hot_retries)`, so 0.2 → 0.6 → 1.0 with defaults | `user` plus a corrective suffix (below) |
| Length cut, `finish_reason == "length"` | `min(int(effective_max × 1.5), headroom)`; if that isn't larger, **stop retrying** | unchanged (no escalation) | original `user`, untouched |

The corrective suffix quotes the first line of the previous validation error (up to 300 chars):

```text
IMPORTANT (retry N): your previous response failed JSON schema validation with: <error>
Return a corrected, complete JSON object that satisfies the schema. Escape all double quotes inside string values.
```

`headroom = max_model_len - int((len(system) + len(user_msg)) / 3.5) - 1024`. The timeout is recomputed for the grown budget.

Why the two cases differ:

- **Hot retries break deterministic resampling.** mlx-lm 0.31.3 seeds its sampler per request, so an identical prompt returns a byte-identical (invalid) completion at any temperature. Changing the prompt (the error feedback) breaks that, and the higher temperature helps on unguided backends.
- **Length cuts get a bigger budget, not heat.** The sampling was fine and the budget wasn't. Re-running a quantized model hot makes it loop: gemma-4-12B QAT dropped from 3.2 to 1.24 chars/token of repetition at temperature 1.0. Feedback text would also shift the resample, so the prompt is left alone. Growing the budget in the client means the launcher's whole-stage retries no longer replay the same too-small cap.

After the loop, `salvage(last_raw)` runs if one was given. Otherwise `ValueError` is raised.

#### Transport retries

Separate from the validation retries above, each request is re-sent (unchanged) when it fails in a way that re-sending can fix cheaply. These failures normally happen **before any generation**. A reset in the middle of one means the server died, so the retry is refused within seconds and the error propagates.

| Error | Retried? | Why |
| --- | --- | --- |
| `openai.APIConnectionError` (refused, reset, DNS) | yes | The server is restarting or its port isn't bound yet. |
| HTTP 503 | yes | llama-server's "Loading model" reply, vLLM overload. |
| `openai.APITimeoutError` | **no** | It subclasses `APIConnectionError`, but a generation that ran out its timeout would just run it out again. |
| Other HTTP errors (400, 500, …) | no | Deterministic: a context overflow or a server bug won't fix itself. |

There are `len(_TRANSPORT_RETRY_BACKOFF_S)` retries (2), after sleeping 2 s and then 5 s. Each one logs a warning on the `lnvox.llm.client` logger, which Python prints to stderr when logging isn't configured. Then the error is re-raised. A transport retry doesn't use up one of the `attempts`. The backoff is short on purpose: a server that stays down should fail the stage quickly, so the launcher's `ensure_vllm` hook can relaunch it.

Because the SDK itself never retries, one request waits at most `_timeout_for(max_tokens)`.

**Example**

```python
from lnvox.llm.client import LLMClient
from lnvox.llm.schemas import SceneDirections

client = LLMClient()
user = client.render("scene_directions.jinja", scene_id="ch01_s1", location_hint="rooftop",
                     scene_context=ctx, numbered_dialogue=lines)
budget = client.budget_for(system=SYSTEM, user=user, desired=2048)
cues = client.structured(system=SYSTEM, user=user, schema=SceneDirections, max_tokens=budget)
```

### `is_transport_error`

```python
def is_transport_error(exc: BaseException) -> bool
```

Module-level function. Returns `True` when `exc` means the LLM server is unreachable or failing rather than the model giving a bad answer: `openai.APIConnectionError` (which includes `APITimeoutError`), `openai.InternalServerError` (any 5xx) or a built-in `ConnectionError`. It's broader than the retry test above, because it answers a different question.

Stages that tolerate a bad answer (skip a cue, keep a block as prose) use it to re-raise server failures. Swallowing them would cache degraded output for every remaining chapter, and the step would never fail, so the launcher's `ensure_vllm` hook would never relaunch the server.

## Template environment

```python
_PROMPTS_DIR = Path(__file__).parent / "prompts"
_jinja = Environment(
    loader=FileSystemLoader(_PROMPTS_DIR),
    trim_blocks=True,
    lstrip_blocks=True,
    undefined=StrictUndefined,
    autoescape=False,
)
```

It's a module-level singleton shared by every client. Templates are plain text (`autoescape=False`), so JSON and quotes pass through unescaped. Details: [prompt templates](/library/llm/prompts/#loading-and-rendering).

## Callers and their budgets

How each call site sizes `max_tokens`, and what it does when `structured()` raises:

| Caller | Schema | `max_tokens` | On exception |
| --- | --- | --- | --- |
| [`s1_characters.extract_per_chapter`](/library/stages/s1-characters/) | `CharacterList` | `budget_for(desired=24576, floor=8192)`, `salvage=_salvage_character_list` | propagates |
| `s1_characters.merge_clusters` | `CharacterMergeProposal` | `budget_for(desired=4096, floor=1024)` | falls back to deterministic merge |
| [`chunker._ask_llm_for_break`](/library/llm/chunker/) | `_Choice` | `512` | middle candidate |
| [`s2_scenes._detect_boundaries`](/library/stages/s2-scenes/) | `ChapterBoundaries` | `max(1024, min(8192, 80 × paragraphs + 512))` (not clamped) | propagates |
| `s2_scenes._tag_scene_beats` | `SceneBeats` | `max(4096, min(32000, int(len(scene_text) × 2.0) + 1024))` (not clamped) | propagates |
| [`s3_director.generate_voice_profiles`](/library/stages/s3-director/) | `VoiceProfileList` | `budget_for(desired=max(4096, 400 × n + 1024), floor=4096)`, `salvage=_salvage_voice_profiles` | propagates |
| `s3_director.direct_scene` | `SceneDirections` | `budget_for(desired=max(2048, 40 × lines + 512), floor=2048)` | no cues (descriptor only) |
| [`lecture._normalize_beats`](/library/stages/lecture/) | `NormalizedBeats` | `budget_for(desired=max(2048, chars / 2 + 1024), floor=2048)` | batch stays verbatim |
| [`scenario.direct_scene`](/library/stages/scenario/) | `ScenarioDirections` | `budget_for(desired=max(2048, 50 × lines + 512), floor=2048)` | all lines `calm`, no cue |
| [`ingest.scenario.structure_chunk`](/library/ingest/scenario/) | `SceneStructure` | `budget_for(desired=max(4096, int(len(chunk) × 1.2)), floor=4096)` | propagates |
| `ingest.scenario.extract_roster` | `RosterList` | `budget_for(desired=4096, floor=2048)` | propagates |
| `ingest.scenario.extract_characters` | `CharacterList` | `budget_for(desired=max(4096, 200 × n + 1024), floor=4096)` | propagates |
| [`ingest.blocks._resolve_ambiguous`](/library/ingest/blocks/) | `BlockClass` | `256` | block treated as prose |
| [`voices.matcher.infer_target`](/library/voices/matcher/) | `VoiceTarget` | `1024` | propagates |
| `voices.matcher.rank_candidates` | `MatchResult` | `max(1024, 200 × top_n + 512)` | propagates |

"Propagates" means the stage fails. The launcher's `run_step ensure_vllm …` then relaunches a dead server and re-runs the stage, which resumes from its per-chapter cache. See [launcher functions](/launcher/functions/). The fallbacks in the "On exception" column are meant for bad answers. A caller that checks [`is_transport_error`](#is_transport_error) re-raises server failures instead of falling back; see each stage's page.

## Notes

- **No caching here.** `LLMClient` doesn't cache responses. Idempotency lives in the stages (per-chapter JSON under `artifacts/<book>/`) and in `ingest-scenario`'s content-addressed `cache/scenario/` (key = schema name + rendered prompt + model).
- **Stateless per call.** Each `structured()` is a fresh two-message conversation with no history. Retries rebuild the messages from scratch.
- **Worst-case wall time** for one `structured()` is about `attempts × timeout`, with the timeout computed for each attempt's (possibly grown) budget, plus at most 7 s of transport backoff per attempt. On the slow-host defaults a 24k-token call can still take hours; a timeout is never re-sent.
- **mlx-lm quirk.** mlx-lm 0.31.3's batched decode ignores per-request `temperature` and uses the server-wide `--temp`. `serve_mlx.sh` therefore serves at `LNVOX_LLM_TEMP` (default 0.2). Otherwise greedy decoding would reproduce the same invalid output on every retry, and the prompt-feedback change would be the only thing breaking it.
- DESIGN.md §14.4 (llama-server) and §11.2 (mlx-lm) accept "no `guided_json` enforcement" as a limitation of those backends. The feedback-and-heat retry loop above is the mitigation.
