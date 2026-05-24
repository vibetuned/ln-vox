"""Read / write the voicebank manifest (`voicebank/manifest.json`)."""

from collections import Counter
from pathlib import Path

from lnvox.voices.schema import Voicebank


MANIFEST_FILENAME = "manifest.json"


def manifest_path(voicebank_dir: Path) -> Path:
    return voicebank_dir / MANIFEST_FILENAME


def load(voicebank_dir: Path) -> Voicebank:
    path = manifest_path(voicebank_dir)
    if not path.exists():
        return Voicebank()
    return Voicebank.model_validate_json(path.read_text(encoding="utf-8"))


def save(voicebank_dir: Path, voicebank: Voicebank) -> None:
    voicebank_dir.mkdir(parents=True, exist_ok=True)
    manifest_path(voicebank_dir).write_text(
        voicebank.model_dump_json(indent=2), encoding="utf-8"
    )


def summarize(voicebank: Voicebank) -> dict:
    """Compact summary for `lnvox voice list`."""
    by_source = Counter(c.source for c in voicebank.clips)
    by_gender_age = Counter((c.gender, c.age_band) for c in voicebank.clips)
    return {
        "total": len(voicebank.clips),
        "by_source": dict(by_source),
        "by_gender_age": {f"{g}/{a}": n for (g, a), n in by_gender_age.items()},
    }
