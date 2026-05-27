import re
from pathlib import Path
from typing import Callable, Type, TypeVar

from jinja2 import Environment, FileSystemLoader, StrictUndefined
from openai import OpenAI
from pydantic import BaseModel

from lnvox.config import Settings


T = TypeVar("T", bound=BaseModel)

_PROMPTS_DIR = Path(__file__).parent / "prompts"
_jinja = Environment(
    loader=FileSystemLoader(_PROMPTS_DIR),
    trim_blocks=True,
    lstrip_blocks=True,
    undefined=StrictUndefined,
    autoescape=False,
)

_FENCE_RE = re.compile(r"^\s*```(?:json|JSON)?\s*\n?(.*?)\n?```\s*$", re.DOTALL)


def _extract_json(text: str) -> str:
    """Strip markdown code fences and surrounding whitespace.

    Gemma's chat-tuned outputs sometimes wrap JSON in ```json ... ``` fences
    even when asked not to. If a fence is present we keep only the inner body;
    otherwise we return the trimmed text untouched.
    """
    if not text:
        return text
    m = _FENCE_RE.match(text)
    if m:
        return m.group(1).strip()
    # Fallback: find the first { or [ and last } or ]
    stripped = text.strip()
    start = min(
        (i for i in (stripped.find("{"), stripped.find("[")) if i != -1),
        default=-1,
    )
    end = max(stripped.rfind("}"), stripped.rfind("]"))
    if start != -1 and end > start:
        return stripped[start : end + 1]
    return stripped


class LLMClient:
    def __init__(self, settings: Settings | None = None):
        self.settings = settings or Settings()
        # We do NOT set a global timeout on the client because the right
        # timeout varies wildly with the requested max_tokens (e.g. an s2
        # segment_chapter wanting 28k tokens vs. a voice_match wanting 800).
        # Per-call timeouts are computed in `structured()` from the config.
        self.client = OpenAI(
            base_url=self.settings.llm.endpoint,
            api_key=self.settings.llm.api_key,
        )

    def render(self, template: str, **kwargs) -> str:
        return _jinja.get_template(template).render(**kwargs)

    # Rough chars-per-token for English + JSON. Deliberately conservative (real
    # ratio is ~3.5-4); over-estimating input leaves more headroom.
    _CHARS_PER_TOKEN = 3.5

    def budget_for(
        self,
        *,
        system: str,
        user: str,
        desired: int,
        floor: int = 2048,
        reserve: int = 1024,
    ) -> int:
        """Output-token budget clamped so prompt + output fits the context.

        Returns the smaller of `desired` and the tokens left after the
        (estimated) prompt and a `reserve` for chat-template overhead, but never
        below `floor`. A too-large request 400s (input+output > context); a
        too-small one truncates the JSON mid-parse — this avoids both.
        """
        est_input = int((len(system) + len(user)) / self._CHARS_PER_TOKEN)
        available = self.settings.llm.max_model_len - est_input - reserve
        return max(floor, min(desired, available))

    def _timeout_for(self, max_tokens: int) -> float:
        """HTTP timeout that accommodates slow models.

        Linear in max_tokens because the model has to generate up to that
        many tokens before returning. At 6 tok/s (Gemma 31B on DGX Spark)
        producing 28k output tokens takes ~78 min, so the default
        seconds_per_token=0.25 yields ~7000s = ~2h with a 90s base
        overhead — enough headroom even with retries / queuing.
        """
        cfg = self.settings.llm
        return cfg.timeout_base_seconds + max_tokens * cfg.timeout_seconds_per_token

    def structured(
        self,
        *,
        system: str,
        user: str,
        schema: Type[T],
        max_tokens: int | None = None,
        attempts: int = 3,
        salvage: Callable[[str], T | None] | None = None,
    ) -> T:
        """Call the model and validate its JSON against `schema`.

        `salvage`, if given, is a last resort: when every attempt fails to
        parse (e.g. the model ran past `max_tokens` mid-array), it is handed the
        raw text to recover whatever complete records it can, instead of raising.
        """
        effective_max = max_tokens or self.settings.llm.max_tokens
        extra_body: dict = {"guided_json": schema.model_json_schema()}
        if self.settings.llm.repetition_penalty != 1.0:
            extra_body["repetition_penalty"] = self.settings.llm.repetition_penalty
        last_error: Exception | None = None
        last_raw = ""
        diag = ""
        attempt = 0
        for attempt in range(attempts):
            response = self.client.chat.completions.create(
                model=self.settings.llm.model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                temperature=self.settings.llm.temperature,
                max_tokens=effective_max,
                response_format={"type": "json_object"},
                extra_body=extra_body,
                timeout=self._timeout_for(effective_max),
            )
            choice = response.choices[0]
            last_raw = choice.message.content or ""
            try:
                return schema.model_validate_json(_extract_json(last_raw))
            except Exception as e:
                last_error = e
                finish = getattr(choice, "finish_reason", "?")
                usage = getattr(response, "usage", None)
                ctok = getattr(usage, "completion_tokens", "?") if usage else "?"
                ptok = getattr(usage, "prompt_tokens", "?") if usage else "?"
                # finish_reason="length" → hit max_tokens (raise the budget /
                # chunk the input); "stop" with invalid JSON → model emitted an
                # end token mid-object (a decoding/model-quality problem, often
                # transient — hence the retry).
                diag = (
                    f"finish_reason={finish}, prompt_tokens={ptok}, "
                    f"completion_tokens={ctok}, max_tokens={effective_max}, "
                    f"raw_chars={len(last_raw)}"
                )
                if finish == "length":
                    # Retrying won't help a hard length cut; fail fast.
                    break

        if salvage is not None:
            try:
                recovered = salvage(last_raw)
            except Exception:
                recovered = None
            if recovered is not None:
                return recovered

        preview = last_raw[:400] + ("…" if len(last_raw) > 400 else "")
        raise ValueError(
            f"LLM returned content that failed schema validation for "
            f"{schema.__name__} after {attempt + 1} attempt(s): {last_error}\n"
            f"--- diagnostics ---\n{diag}\n--- raw (first 400 chars) ---\n{preview}"
        ) from last_error
