"""Tests for Settings env / `.env` loading and the shorthand aliases.

Runnable directly (`python tests/test_config_env.py`) or under pytest.
Each test runs in a fresh temp CWD (so `./.env` is whatever the test writes)
with every LNVOX_* variable removed from the process environment.
"""

import contextlib
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from lnvox.config import LLMConfig, Settings

DEFAULT_MODEL = LLMConfig.model_fields["model"].default


@contextlib.contextmanager
def _isolated(env: dict[str, str] | None = None, dotenv: str | None = None):
    """Temp CWD holding an optional `.env`, and a process env with no LNVOX_*
    variables except `env`. Restores both afterwards."""
    saved_env = dict(os.environ)
    saved_cwd = os.getcwd()
    with tempfile.TemporaryDirectory() as tmp:
        try:
            for k in list(os.environ):
                if k.upper().startswith("LNVOX_"):
                    del os.environ[k]
            os.environ.update(env or {})
            os.chdir(tmp)
            if dotenv is not None:
                Path(tmp, ".env").write_text(dotenv, encoding="utf-8")
            yield Path(tmp)
        finally:
            os.chdir(saved_cwd)
            os.environ.clear()
            os.environ.update(saved_env)


def test_defaults_without_env():
    with _isolated():
        s = Settings()
        assert s.llm.model == DEFAULT_MODEL
        assert s.llm.max_model_len == 65536
        assert s.voicebank == Path("voicebank")


def test_shorthand_from_process_env():
    with _isolated(env={
        "LNVOX_LLM_MODEL": "env-model",
        "LNVOX_LLM_ENDPOINT": "http://h:9/v1",
        "LNVOX_LLM_API_KEY": "k",
        "LNVOX_LLM_MAX_MODEL_LEN": "32768",
        "LNVOX_LLM_REPETITION_PENALTY": "1.1",
    }):
        llm = Settings().llm
        assert (llm.model, llm.endpoint, llm.api_key) == ("env-model", "http://h:9/v1", "k")
        assert llm.max_model_len == 32768 and llm.repetition_penalty == 1.1


def test_shorthand_from_dotenv():
    # Regression: shorthands were read from os.environ only, so a `.env`
    # line like LNVOX_LLM_MODEL=… was silently dropped.
    with _isolated(dotenv=(
        "LNVOX_LLM_MODEL=dotenv-model\n"
        "LNVOX_LLM_MAX_MODEL_LEN=16384\n"
        "LNVOX_LLM_REPETITION_PENALTY=1.05\n"
    )):
        llm = Settings().llm
        assert llm.model == "dotenv-model", llm.model
        assert llm.max_model_len == 16384
        assert llm.repetition_penalty == 1.05


def test_process_env_beats_dotenv_shorthand():
    with _isolated(env={"LNVOX_LLM_MODEL": "env-model"},
                   dotenv="LNVOX_LLM_MODEL=dotenv-model\n"):
        assert Settings().llm.model == "env-model"


def test_nested_equal_to_default_still_wins():
    # Regression: "explicitly set" was decided by comparing with the default
    # value, so a nested var set to exactly the default lost to the shorthand.
    with _isolated(env={"LNVOX_LLM__MODEL": DEFAULT_MODEL,
                        "LNVOX_LLM_MODEL": "shorthand-model"}):
        assert Settings().llm.model == DEFAULT_MODEL
    with _isolated(env={"LNVOX_LLM_MODEL": "shorthand-model"},
                   dotenv=f"LNVOX_LLM__MODEL={DEFAULT_MODEL}\n"):
        assert Settings().llm.model == DEFAULT_MODEL


def test_nested_non_default_wins_over_shorthand():
    with _isolated(env={"LNVOX_LLM_MODEL": "shorthand-model"},
                   dotenv="LNVOX_LLM__MODEL=nested-model\n"):
        assert Settings().llm.model == "nested-model"
    # Init kwargs count as explicit too.
    with _isolated(env={"LNVOX_LLM_MAX_MODEL_LEN": "8192"}):
        assert Settings(llm={"max_model_len": 65536}).llm.max_model_len == 65536


def test_max_len_fallback():
    # The serve scripts / launcher `--max-model-len` export LNVOX_LLM_MAX_LEN;
    # the client must see it when its own name isn't set.
    with _isolated(env={"LNVOX_LLM_MAX_LEN": "20480"}):
        assert Settings().llm.max_model_len == 20480
    with _isolated(dotenv="LNVOX_LLM_MAX_LEN=20480\n"):
        assert Settings().llm.max_model_len == 20480
    # The client-side name wins within one source…
    with _isolated(env={"LNVOX_LLM_MAX_LEN": "20480",
                        "LNVOX_LLM_MAX_MODEL_LEN": "32768"}):
        assert Settings().llm.max_model_len == 32768
    # …but the process env (what the launcher exports) beats `.env`.
    with _isolated(env={"LNVOX_LLM_MAX_LEN": "20480"},
                   dotenv="LNVOX_LLM_MAX_MODEL_LEN=32768\n"):
        assert Settings().llm.max_model_len == 20480
    # An explicit nested value beats both shorthand names.
    with _isolated(env={"LNVOX_LLM_MAX_LEN": "20480",
                        "LNVOX_LLM__MAX_MODEL_LEN": "65536"}):
        assert Settings().llm.max_model_len == 65536


def test_unparsable_shorthand_falls_through():
    with _isolated(env={"LNVOX_LLM_MAX_MODEL_LEN": "32k",
                        "LNVOX_LLM_REPETITION_PENALTY": "high"}):
        llm = Settings().llm
        assert llm.max_model_len == 65536 and llm.repetition_penalty == 1.0
    with _isolated(env={"LNVOX_LLM_MAX_MODEL_LEN": "32k",
                        "LNVOX_LLM_MAX_LEN": "20480"}):
        assert Settings().llm.max_model_len == 20480


def test_shorthand_does_not_leak_between_instances():
    with _isolated(env={"LNVOX_LLM_MODEL": "env-model"}):
        assert Settings().llm.model == "env-model"
    with _isolated():
        s = Settings()
        assert s.llm.model == DEFAULT_MODEL
        assert "model" not in s.llm.model_fields_set


def test_voicebank_env_and_dotenv():
    # Regression: cli._voicebank_dir read os.environ directly, ignoring .env.
    from lnvox import cli

    with _isolated(env={"LNVOX_VOICEBANK": "voicebank-fr"}):
        assert cli._voicebank_dir() == Path("voicebank-fr")
    with _isolated(dotenv="LNVOX_VOICEBANK=voicebank-demo\n"):
        assert cli._voicebank_dir() == Path("voicebank-demo")
    with _isolated(env={"LNVOX_VOICEBANK": "voicebank-fr"},
                   dotenv="LNVOX_VOICEBANK=voicebank-demo\n"):
        assert cli._voicebank_dir() == Path("voicebank-fr")
    with _isolated():
        assert cli._voicebank_dir() == Path("voicebank")


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  ok: {fn.__name__}")
    print(f"\n{len(fns)} test(s) passed.")


if __name__ == "__main__":
    _run_all()
