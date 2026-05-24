import re
from pathlib import Path
from typing import Type, TypeVar

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
    ) -> T:
        effective_max = max_tokens or self.settings.llm.max_tokens
        response = self.client.chat.completions.create(
            model=self.settings.llm.model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            temperature=self.settings.llm.temperature,
            max_tokens=effective_max,
            response_format={"type": "json_object"},
            extra_body={"guided_json": schema.model_json_schema()},
            timeout=self._timeout_for(effective_max),
        )
        raw = response.choices[0].message.content or ""
        cleaned = _extract_json(raw)
        try:
            return schema.model_validate_json(cleaned)
        except Exception as e:
            preview = cleaned[:400] + ("…" if len(cleaned) > 400 else "")
            raise ValueError(
                f"LLM returned content that failed schema validation for "
                f"{schema.__name__}: {e}\n--- raw (first 400 chars) ---\n{preview}"
            ) from e
