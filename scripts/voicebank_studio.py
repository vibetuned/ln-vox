#!/usr/bin/env python3
"""Voicebank Studio — a PySide6 GUI for curating the ln-vox voicebank by ear.

See DESIGN.md §12. Four operations against `voicebank/manifest.json` +
`voicebank/clips/`:

  1. Listen to any voicebank clip and read its metadata.
  2. Import from Common Voice: browse the local `data/…/en` corpus by speaker,
     preview utterances, preview the merged reference clip, and promote a
     speaker into the voicebank (reuses lnvox.voices.common_voice.build_speaker_clip).
  3. Add a clip manually from an arbitrary wav/mp3 + hand-entered taxonomies.
  4. Erase a clip from the voicebank.

Run it:

    uv pip install PySide6           # one-time; PySide6 is dev-only, not a project dep
    uv run python scripts/voicebank_studio.py

Optional: point it at a different voicebank / corpus:

    uv run python scripts/voicebank_studio.py --voicebank voicebank --cv-root <path-to>/en
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
import tempfile
from collections import defaultdict
from pathlib import Path

# csv field size: CV sentences are short, but some metadata rows are long.
csv.field_size_limit(min(sys.maxsize, 2**31 - 1))

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC = REPO_ROOT / "src"
if SRC.is_dir() and str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

try:
    from PySide6.QtCore import Qt, QThread, QUrl, Signal
    from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
    from PySide6.QtWidgets import (
        QApplication,
        QComboBox,
        QDialog,
        QDialogButtonBox,
        QDoubleSpinBox,
        QFileDialog,
        QFormLayout,
        QGroupBox,
        QHBoxLayout,
        QHeaderView,
        QLabel,
        QLineEdit,
        QListWidget,
        QListWidgetItem,
        QMessageBox,
        QPlainTextEdit,
        QPushButton,
        QSpinBox,
        QSplitter,
        QTableWidget,
        QTableWidgetItem,
        QTabWidget,
        QVBoxLayout,
        QWidget,
    )
except ImportError as e:  # pragma: no cover - friendly bail-out
    sys.exit(
        "PySide6 is not installed. Install it with:\n"
        "    uv pip install PySide6\n"
        f"(import error: {e})"
    )

try:
    import librosa
    import soundfile as sf
except ImportError as e:  # pragma: no cover
    sys.exit(
        "Voice deps missing (soundfile/librosa). Install with:\n"
        "    uv sync --extra voice\n"
        f"(import error: {e})"
    )

from lnvox.voices import manifest as voice_manifest
from lnvox.voices.matcher import descriptor_from_clip
from lnvox.voices.common_voice import (
    _AGE_MAP,
    _GENDER_MAP,
    _eligible,
    _normalize_accent,
    build_speaker_clip,
)
from lnvox.voices.schema import BookCasting, VoiceClip

OUTPUT_SR = 24000
AGE_BANDS = ["teen", "young_adult", "adult", "elder"]
GENDERS = ["male", "female"]


# --------------------------------------------------------------------------- #
#  Paths
# --------------------------------------------------------------------------- #
def autodetect_cv_root(data_dir: Path) -> Path | None:
    """Find an extracted Common Voice locale dir (has clips/ + a *.tsv)."""
    if not data_dir.is_dir():
        return None
    for tsv in sorted(data_dir.glob("**/validated.tsv")):
        if (tsv.parent / "clips").is_dir():
            return tsv.parent
    # Fall back to any locale dir with a clips/ folder + some tsv.
    for clips in sorted(data_dir.glob("**/clips")):
        if clips.is_dir() and list(clips.parent.glob("*.tsv")):
            return clips.parent
    return None


def autodetect_casting_file(artifacts_dir: Path) -> Path | None:
    """Most-recently-modified 04_voice_assignments.json under artifacts/."""
    if not artifacts_dir.is_dir():
        return None
    files = list(artifacts_dir.glob("**/04_voice_assignments.json"))
    if not files:
        return None
    return max(files, key=lambda p: p.stat().st_mtime)


# --------------------------------------------------------------------------- #
#  Audio playback — decode anything non-wav to a temp 24k wav, play wav only.
# --------------------------------------------------------------------------- #
class AudioPlayer:
    """Thin QMediaPlayer wrapper that only ever feeds Qt a wav file."""

    def __init__(self) -> None:
        self._player = QMediaPlayer()
        self._out = QAudioOutput()
        self._out.setVolume(1.0)
        self._player.setAudioOutput(self._out)
        self._tmp = Path(tempfile.mkdtemp(prefix="vbstudio_play_"))
        self._cache: dict[str, Path] = {}

    def _playable_wav(self, path: Path) -> Path:
        if path.suffix.lower() == ".wav":
            return path
        key = f"{path}:{path.stat().st_mtime_ns}"
        cached = self._cache.get(key)
        if cached and cached.exists():
            return cached
        audio, _ = librosa.load(str(path), sr=OUTPUT_SR, mono=True)
        dst = self._tmp / f"play_{abs(hash(key))}.wav"
        sf.write(str(dst), audio, OUTPUT_SR, subtype="PCM_16")
        self._cache[key] = dst
        return dst

    def play(self, path: Path) -> None:
        wav = self._playable_wav(path)
        self._player.stop()
        self._player.setSource(QUrl.fromLocalFile(str(wav)))
        self._player.play()

    def stop(self) -> None:
        self._player.stop()


# --------------------------------------------------------------------------- #
#  Background workers
# --------------------------------------------------------------------------- #
class CVScanWorker(QThread):
    """Stream a CV TSV, group eligible rows by speaker, emit ready speakers.

    A speaker is emitted the moment it has >= min_utts eligible rows AND its
    demographics match the active filters. Scanning stops at max_speakers.
    """

    speaker_found = Signal(dict)
    progress = Signal(str)
    done = Signal(int)
    failed = Signal(str)

    def __init__(
        self,
        tsv_path: Path,
        *,
        gender: str,
        age: str,
        accent: str,
        min_utts: int,
        max_speakers: int,
    ) -> None:
        super().__init__()
        self.tsv_path = tsv_path
        self.gender = gender  # "" = any
        self.age = age  # "" = any
        self.accent = accent.strip().lower()  # "" = any (substring match)
        self.min_utts = min_utts
        self.max_speakers = max_speakers

    def _matches(self, row: dict) -> bool:
        if self.gender and _GENDER_MAP.get((row.get("gender") or "").strip()) != self.gender:
            return False
        if self.age and _AGE_MAP.get((row.get("age") or "").strip()) != self.age:
            return False
        if self.accent:
            code = _normalize_accent(row.get("accents") or row.get("variant"))
            if self.accent not in code:
                return False
        return True

    def run(self) -> None:  # noqa: D401
        try:
            by_speaker: dict[str, list[dict]] = defaultdict(list)
            emitted: set[str] = set()
            scanned = 0
            with self.tsv_path.open("r", encoding="utf-8", newline="") as f:
                reader = csv.DictReader(f, delimiter="\t")
                for row in reader:
                    if self.isInterruptionRequested():
                        break
                    scanned += 1
                    if scanned % 50_000 == 0:
                        self.progress.emit(
                            f"Scanned {scanned:,} rows · {len(emitted)}/{self.max_speakers} speakers"
                        )
                    if not _eligible(row):
                        continue
                    cid = row.get("client_id")
                    if not cid or cid in emitted:
                        continue
                    by_speaker[cid].append(row)
                    rows = by_speaker[cid]
                    if len(rows) < self.min_utts:
                        continue
                    if not self._matches(rows[0]):
                        continue
                    emitted.add(cid)
                    self.speaker_found.emit(
                        {
                            "speaker_id": cid,
                            "gender": _GENDER_MAP.get((rows[0].get("gender") or "").strip(), "?"),
                            "age": _AGE_MAP.get((rows[0].get("age") or "").strip(), "?"),
                            "accent": _normalize_accent(
                                rows[0].get("accents") or rows[0].get("variant")
                            ),
                            "rows": list(rows),
                        }
                    )
                    if len(emitted) >= self.max_speakers:
                        break
            self.done.emit(len(emitted))
        except Exception as e:  # pragma: no cover
            self.failed.emit(str(e))


class BuildWorker(QThread):
    """Build one speaker's reference clip into `dest_dir` (preview or promote)."""

    built = Signal(object)  # VoiceClip | None
    failed = Signal(str)

    def __init__(self, speaker_id: str, rows: list[dict], clips_src: Path, dest_dir: Path, tsv_name: str) -> None:
        super().__init__()
        self.speaker_id = speaker_id
        self.rows = rows
        self.clips_src = clips_src
        self.dest_dir = dest_dir
        self.tsv_name = tsv_name

    def run(self) -> None:
        try:
            clip = build_speaker_clip(
                self.speaker_id,
                self.rows,
                clips_src=self.clips_src,
                voicebank_dir=self.dest_dir,
                tsv_name=self.tsv_name,
            )
            self.built.emit(clip)
        except Exception as e:  # pragma: no cover
            self.failed.emit(str(e))


def _installed_transformers_version() -> str | None:
    try:
        from importlib.metadata import version

        return version("transformers")
    except Exception:
        return None


def _augment_engine_error(engine: str, msg: str) -> str:
    """Append the DESIGN.md §16.6 transformers-conflict hint when the error
    smells like it — Dramabox needs transformers ≥4.52 (Gemma `.model`
    submodule), VibeVoice's fork pins ==4.51.3."""
    if "has no attribute 'model'" in msg or "Gemma3" in msg:
        msg += (
            "\n\nLikely the Dramabox↔VibeVoice transformers conflict (DESIGN.md §16.6):\n"
            "Dramabox needs transformers ≥ 4.52, VibeVoice pins == 4.51.3.\n"
            f"Installed: {_installed_transformers_version() or 'unknown'}.\n\n"
            "For Dramabox:   uv pip install -U 'transformers>=4.52'\n"
            "For VibeVoice:  uv pip install -e external/VibeVoice\n"
            "…then restart the Studio."
        )
    return msg


class TtsLoadWorker(QThread):
    """Load ONE TTS engine once, off the GUI thread (DESIGN.md §2.6 / §16).

    Heavy (first run downloads 15-18 GB, then loads into VRAM), so the engine
    imports are deliberately kept inside `run()` — the Studio launches (and its
    other tabs work) with no torch / Dramabox / VibeVoice present.
    """

    loaded = Signal(object, str, str)  # (client, engine, resolved_device)
    failed = Signal(str)

    def __init__(self, engine: str, device: str) -> None:
        super().__init__()
        self.engine = engine
        self.device = device

    def run(self) -> None:
        try:
            resolved = self.device
            if resolved == "auto":
                resolved = "cuda"
                try:
                    import torch

                    if torch.backends.mps.is_available():
                        resolved = "mps"
                    elif not torch.cuda.is_available():
                        resolved = "cpu"
                except Exception:
                    pass
            if self.engine == "vibevoice":
                from lnvox.tts.vibevoice_client import VibeVoiceClient

                client = VibeVoiceClient(device=resolved)
            else:
                from lnvox.tts.dramabox_client import DramaboxClient

                client = DramaboxClient(device=resolved)
            self.loaded.emit(client, self.engine, resolved)
        except ModuleNotFoundError as e:  # pragma: no cover
            if self.engine == "vibevoice":
                self.failed.emit(
                    f"Missing Python module: {e}.\n\n"
                    "VibeVoice isn't installed in this venv. Run:\n"
                    "    ./scripts/setup_vibevoice.sh\n"
                    "(clones the community fork and pip-installs it editable — DESIGN.md §16.6)"
                )
                return
            # Dramabox's runtime deps live in external/DramaBox/requirements.txt,
            # not the project's pyproject — and `uv sync` prunes them. Point the
            # user at the torch-safe reinstall (the launcher does this in
            # prepare_tts_env; the standalone Studio doesn't).
            self.failed.emit(
                f"Missing Python module: {e}.\n\n"
                "This is a Dramabox runtime dependency (declared in "
                "external/DramaBox/requirements.txt), which `uv sync` prunes.\n\n"
                "Quick fix for this one:\n"
                f"    uv pip install \"{e.name}\"\n\n"
                "Restore all of them WITHOUT downgrading torch:\n"
                "    grep -viE '^(torch|torchaudio|torchvision)' \\\n"
                "      external/DramaBox/requirements.txt | uv pip install -r /dev/stdin"
            )
        except Exception as e:  # pragma: no cover
            self.failed.emit(_augment_engine_error(self.engine, str(e)))


_SPEAKER_RE = re.compile(r"^\s*Speaker\s+(\d+)\s*:", re.IGNORECASE)


def _to_vibevoice_script(prompt: str) -> tuple[str, int]:
    """Pass `Speaker N:` scripts through; wrap plain text as Speaker 1.

    Returns (script, highest speaker number) — the Lab clones every speaker
    from the same selected reference, so the count sizes `voice_refs`.
    """
    lines = [ln.strip() for ln in prompt.splitlines() if ln.strip()]
    numbers = [int(m.group(1)) for ln in lines if (m := _SPEAKER_RE.match(ln))]
    if not numbers:
        return "Speaker 1: " + " ".join(prompt.split()), 1
    return "\n".join(lines), max(numbers)


class TtsGenWorker(QThread):
    """Render one prompt/script to a wav with an already-loaded engine client."""

    done = Signal(str)  # output wav path
    failed = Signal(str)

    def __init__(
        self,
        client,
        *,
        engine: str,
        prompt: str,
        output_path: Path,
        voice_ref: Path | None,
        n_speakers: int,
        seed: int,
        cfg_scale: float,
        stg_scale: float,
    ) -> None:
        super().__init__()
        self.client = client
        self.engine = engine
        self.prompt = prompt
        self.output_path = output_path
        self.voice_ref = voice_ref
        self.n_speakers = n_speakers
        self.seed = seed
        self.cfg_scale = cfg_scale
        self.stg_scale = stg_scale

    def run(self) -> None:
        try:
            if self.engine == "vibevoice":
                # Every Speaker N clones the same selected reference — the Lab
                # auditions one voice; true multi-voice sessions live in s4.
                self.client.generate_session(
                    script=self.prompt,
                    voice_refs=[self.voice_ref] * self.n_speakers,
                    output_path=self.output_path,
                    seed=self.seed,
                    cfg_scale=self.cfg_scale,
                )
            else:
                self.client.generate(
                    prompt=self.prompt,
                    output_path=self.output_path,
                    voice_ref=self.voice_ref,
                    seed=self.seed,
                    cfg_scale=self.cfg_scale,
                    stg_scale=self.stg_scale,
                )
            self.done.emit(str(self.output_path))
        except Exception as e:  # pragma: no cover
            self.failed.emit(_augment_engine_error(self.engine, str(e)))


# --------------------------------------------------------------------------- #
#  Voicebank store
# --------------------------------------------------------------------------- #
class VoicebankStore:
    def __init__(self, voicebank_dir: Path) -> None:
        self.dir = voicebank_dir
        self.bank = voice_manifest.load(voicebank_dir)

    @property
    def clips(self) -> list[VoiceClip]:
        return self.bank.clips

    def ids(self) -> set[str]:
        return {c.id for c in self.bank.clips}

    def reload(self) -> None:
        self.bank = voice_manifest.load(self.dir)

    def save(self) -> None:
        voice_manifest.save(self.dir, self.bank)

    def add(self, clip: VoiceClip) -> None:
        self.bank.clips.append(clip)
        self.save()

    def remove(self, clip_id: str) -> None:
        clip = next((c for c in self.bank.clips if c.id == clip_id), None)
        if clip is None:
            return
        wav = self.dir / clip.clip_path
        if wav.exists():
            wav.unlink()
        self.bank.clips = [c for c in self.bank.clips if c.id != clip_id]
        self.save()


# --------------------------------------------------------------------------- #
#  Manual-add dialog
# --------------------------------------------------------------------------- #
class ManualAddDialog(QDialog):
    def __init__(self, store: VoicebankStore, parent=None) -> None:
        super().__init__(parent)
        self.store = store
        self.setWindowTitle("Add a clip manually")
        self.setMinimumWidth(520)
        self.src_path: Path | None = None

        form = QFormLayout(self)

        file_row = QHBoxLayout()
        self.file_label = QLabel("(no file chosen)")
        browse = QPushButton("Choose audio…")
        browse.clicked.connect(self._choose)
        file_row.addWidget(self.file_label, 1)
        file_row.addWidget(browse)
        form.addRow("Audio file:", self._wrap(file_row))

        self.id_edit = QLineEdit()
        form.addRow("Clip id:", self.id_edit)

        self.gender = QComboBox()
        self.gender.addItems(GENDERS)
        form.addRow("Gender:", self.gender)

        self.age = QComboBox()
        self.age.addItems(AGE_BANDS)
        form.addRow("Age band:", self.age)

        self.accent = QLineEdit("any")
        form.addRow("Accent:", self.accent)

        self.sentences = QPlainTextEdit()
        self.sentences.setPlaceholderText("One sample sentence per line (up to 3)")
        self.sentences.setFixedHeight(70)
        form.addRow("Sample sentences:", self.sentences)

        self.license = QLineEdit("unknown")
        form.addRow("License:", self.license)

        self.notes = QLineEdit("Added manually via Voicebank Studio")
        form.addRow("Notes:", self.notes)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    @staticmethod
    def _wrap(layout) -> QWidget:
        w = QWidget()
        w.setLayout(layout)
        return w

    def _choose(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Choose audio", str(Path.home()), "Audio (*.wav *.mp3 *.flac *.ogg *.m4a)"
        )
        if not path:
            return
        self.src_path = Path(path)
        self.file_label.setText(self.src_path.name)
        if not self.id_edit.text().strip():
            self.id_edit.setText(f"manual_{self.src_path.stem}")

    def _accept(self) -> None:
        if not self.src_path:
            QMessageBox.warning(self, "Missing file", "Choose an audio file first.")
            return
        clip_id = self.id_edit.text().strip()
        if not clip_id:
            QMessageBox.warning(self, "Missing id", "Enter a clip id.")
            return
        if clip_id in self.store.ids():
            QMessageBox.warning(self, "Duplicate id", f"'{clip_id}' is already in the voicebank.")
            return
        try:
            audio, _ = librosa.load(str(self.src_path), sr=OUTPUT_SR, mono=True)
            if audio.size == 0:
                raise ValueError("decoded audio is empty")
            rel = Path("clips") / f"{clip_id}.wav"
            (self.store.dir / "clips").mkdir(parents=True, exist_ok=True)
            sf.write(str(self.store.dir / rel), audio, OUTPUT_SR, subtype="PCM_16")
            sentences = [s.strip() for s in self.sentences.toPlainText().splitlines() if s.strip()][:3]
            clip = VoiceClip(
                id=clip_id,
                source="manual",
                clip_path=str(rel),
                duration_seconds=round(len(audio) / OUTPUT_SR, 2),
                gender=self.gender.currentText(),
                age_band=self.age.currentText(),
                accent=self.accent.text().strip() or "any",
                sample_sentences=sentences,
                license=self.license.text().strip() or "unknown",
                notes=self.notes.text().strip(),
            )
        except Exception as e:
            QMessageBox.critical(self, "Could not add clip", str(e))
            return
        self.store.add(clip)
        self.accept()


# --------------------------------------------------------------------------- #
#  Voicebank tab
# --------------------------------------------------------------------------- #
class VoicebankTab(QWidget):
    COLS = ["id", "gender", "age_band", "accent", "dur (s)", "source", "sample sentences"]

    def __init__(self, store: VoicebankStore, player: AudioPlayer) -> None:
        super().__init__()
        self.store = store
        self.player = player
        self.on_change = None  # set by MainWindow to fan out to other tabs

        layout = QVBoxLayout(self)

        bar = QHBoxLayout()
        self.count_label = QLabel()
        bar.addWidget(self.count_label, 1)
        for text, slot in [
            ("▶ Play", self._play),
            ("✕ Erase", self._erase),
            ("+ Add manual…", self._add_manual),
            ("⟳ Reload", self._reload),
        ]:
            b = QPushButton(text)
            b.clicked.connect(slot)
            bar.addWidget(b)
        layout.addLayout(bar)

        self.table = QTableWidget(0, len(self.COLS))
        self.table.setHorizontalHeaderLabels(self.COLS)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setSelectionMode(QTableWidget.SingleSelection)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(
            self.COLS.index("sample sentences"), QHeaderView.Stretch
        )
        self.table.doubleClicked.connect(self._play)
        layout.addWidget(self.table)

        self.refresh()

    def refresh(self) -> None:
        clips = sorted(self.store.clips, key=lambda c: c.id)
        self.table.setRowCount(len(clips))
        for r, c in enumerate(clips):
            values = [
                c.id,
                c.gender,
                c.age_band,
                c.accent,
                f"{c.duration_seconds:.2f}",
                c.source,
                " · ".join(c.sample_sentences),
            ]
            for col, val in enumerate(values):
                item = QTableWidgetItem(val)
                if col == 0:
                    item.setData(Qt.UserRole, c.id)
                self.table.setItem(r, col, item)
        self.table.resizeColumnsToContents()
        self.table.horizontalHeader().setSectionResizeMode(
            self.COLS.index("sample sentences"), QHeaderView.Stretch
        )
        self.count_label.setText(f"{len(clips)} clip(s) in {self.store.dir}")

    def _selected_clip(self) -> VoiceClip | None:
        rows = self.table.selectionModel().selectedRows()
        if not rows:
            return None
        clip_id = self.table.item(rows[0].row(), 0).data(Qt.UserRole)
        return next((c for c in self.store.clips if c.id == clip_id), None)

    def _play(self) -> None:
        clip = self._selected_clip()
        if not clip:
            return
        wav = self.store.dir / clip.clip_path
        if not wav.exists():
            QMessageBox.warning(self, "Missing wav", f"{wav} not found.")
            return
        self.player.play(wav)

    def _erase(self) -> None:
        clip = self._selected_clip()
        if not clip:
            return
        if (
            QMessageBox.question(
                self,
                "Erase clip",
                f"Remove '{clip.id}' from the voicebank and delete its wav?",
            )
            == QMessageBox.Yes
        ):
            self.player.stop()
            self.store.remove(clip.id)
            self._changed()

    def _add_manual(self) -> None:
        dlg = ManualAddDialog(self.store, self)
        if dlg.exec() == QDialog.Accepted:
            self._changed()

    def _reload(self) -> None:
        self.store.reload()
        self._changed()

    def _changed(self) -> None:
        if self.on_change:
            self.on_change()
        else:
            self.refresh()


# --------------------------------------------------------------------------- #
#  Import-from-Common-Voice tab
# --------------------------------------------------------------------------- #
class ImportTab(QWidget):
    SPK_COLS = ["speaker", "gender", "age", "accent", "#utts"]

    def __init__(self, store: VoicebankStore, player: AudioPlayer, cv_root: Path | None, on_change) -> None:
        super().__init__()
        self.store = store
        self.player = player
        self.cv_root = cv_root
        self.on_change = on_change
        self.scan: CVScanWorker | None = None
        self.builder: BuildWorker | None = None
        self._preview_dir = Path(tempfile.mkdtemp(prefix="vbstudio_preview_"))
        self._speakers: dict[str, dict] = {}

        layout = QVBoxLayout(self)

        # --- corpus + filters --------------------------------------------- #
        root_row = QHBoxLayout()
        root_row.addWidget(QLabel("CV root:"))
        self.root_edit = QLineEdit(str(cv_root) if cv_root else "")
        root_row.addWidget(self.root_edit, 1)
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse_root)
        root_row.addWidget(browse)
        self.tsv_combo = QComboBox()
        root_row.addWidget(QLabel("TSV:"))
        root_row.addWidget(self.tsv_combo)
        layout.addLayout(root_row)

        filt = QHBoxLayout()
        self.gender_f = QComboBox()
        self.gender_f.addItems(["any", *GENDERS])
        self.age_f = QComboBox()
        self.age_f.addItems(["any", *AGE_BANDS])
        self.accent_f = QLineEdit()
        self.accent_f.setPlaceholderText("accent contains… (e.g. us)")
        self.accent_f.setMaximumWidth(180)
        self.min_utts = QSpinBox()
        self.min_utts.setRange(1, 50)
        self.min_utts.setValue(8)
        self.max_spk = QSpinBox()
        self.max_spk.setRange(1, 2000)
        self.max_spk.setValue(100)
        for lbl, w in [
            ("Gender:", self.gender_f),
            ("Age:", self.age_f),
            ("Accent:", self.accent_f),
            ("Min utts:", self.min_utts),
            ("Max speakers:", self.max_spk),
        ]:
            filt.addWidget(QLabel(lbl))
            filt.addWidget(w)
        filt.addStretch(1)
        self.scan_btn = QPushButton("Scan")
        self.scan_btn.clicked.connect(self._toggle_scan)
        filt.addWidget(self.scan_btn)
        layout.addLayout(filt)

        # --- speakers | utterances ---------------------------------------- #
        splitter = QSplitter(Qt.Horizontal)

        self.spk_table = QTableWidget(0, len(self.SPK_COLS))
        self.spk_table.setHorizontalHeaderLabels(self.SPK_COLS)
        self.spk_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.spk_table.setSelectionMode(QTableWidget.SingleSelection)
        self.spk_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.spk_table.verticalHeader().setVisible(False)
        self.spk_table.itemSelectionChanged.connect(self._on_speaker_selected)
        splitter.addWidget(self.spk_table)

        right = QWidget()
        right_l = QVBoxLayout(right)
        utt_box = QGroupBox("Utterances (double-click to play)")
        utt_l = QVBoxLayout(utt_box)
        self.utt_list = QListWidget()
        self.utt_list.itemDoubleClicked.connect(self._play_utterance)
        utt_l.addWidget(self.utt_list)
        right_l.addWidget(utt_box, 1)

        act = QHBoxLayout()
        self.preview_btn = QPushButton("▶ Preview merged")
        self.preview_btn.clicked.connect(self._preview_merged)
        self.promote_btn = QPushButton("➜ Promote to voicebank")
        self.promote_btn.clicked.connect(self._promote)
        act.addWidget(self.preview_btn)
        act.addWidget(self.promote_btn)
        right_l.addLayout(act)
        splitter.addWidget(right)
        splitter.setSizes([420, 360])
        layout.addWidget(splitter, 1)

        self.status = QLabel("Set filters and press Scan.")
        layout.addWidget(self.status)

        self._refresh_tsv_combo()
        self._set_busy(False)

    # -- corpus helpers ---------------------------------------------------- #
    def _browse_root(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Choose CV locale dir (has clips/ + *.tsv)")
        if path:
            self.root_edit.setText(path)
            self._refresh_tsv_combo()

    def _refresh_tsv_combo(self) -> None:
        self.tsv_combo.clear()
        root = Path(self.root_edit.text().strip())
        if root.is_dir():
            tsvs = sorted(p.name for p in root.glob("*.tsv"))
            ordered = [t for t in ("validated.tsv", "train.tsv", "dev.tsv", "test.tsv") if t in tsvs]
            ordered += [t for t in tsvs if t not in ordered]
            self.tsv_combo.addItems(ordered)

    def _clips_src(self) -> Path:
        return Path(self.root_edit.text().strip()) / "clips"

    # -- scanning ---------------------------------------------------------- #
    def _toggle_scan(self) -> None:
        if self.scan and self.scan.isRunning():
            self.scan.requestInterruption()
            self.scan_btn.setText("Stopping…")
            self.scan_btn.setEnabled(False)
            return
        root = Path(self.root_edit.text().strip())
        tsv = root / self.tsv_combo.currentText()
        if not tsv.is_file():
            QMessageBox.warning(self, "No TSV", f"Not found: {tsv}")
            return
        if not self._clips_src().is_dir():
            QMessageBox.warning(self, "No clips/", f"Missing clips dir: {self._clips_src()}")
            return

        self.spk_table.setRowCount(0)
        self.utt_list.clear()
        self._speakers.clear()
        self.scan = CVScanWorker(
            tsv,
            gender="" if self.gender_f.currentText() == "any" else self.gender_f.currentText(),
            age="" if self.age_f.currentText() == "any" else self.age_f.currentText(),
            accent=self.accent_f.text(),
            min_utts=self.min_utts.value(),
            max_speakers=self.max_spk.value(),
        )
        self.scan.speaker_found.connect(self._add_speaker)
        self.scan.progress.connect(self.status.setText)
        self.scan.done.connect(self._scan_done)
        self.scan.failed.connect(self._scan_failed)
        self.scan.start()
        self.scan_btn.setText("Stop")
        self.status.setText("Scanning…")

    def _add_speaker(self, info: dict) -> None:
        self._speakers[info["speaker_id"]] = info
        r = self.spk_table.rowCount()
        self.spk_table.insertRow(r)
        values = [info["speaker_id"][:14] + "…", info["gender"], info["age"], info["accent"], str(len(info["rows"]))]
        for col, val in enumerate(values):
            item = QTableWidgetItem(val)
            if col == 0:
                item.setData(Qt.UserRole, info["speaker_id"])
            self.spk_table.setItem(r, col, item)
        self.spk_table.resizeColumnsToContents()

    def _scan_done(self, n: int) -> None:
        self.scan_btn.setText("Scan")
        self.scan_btn.setEnabled(True)
        self.status.setText(f"Found {n} speaker(s). Select one to preview / promote.")

    def _scan_failed(self, msg: str) -> None:
        self.scan_btn.setText("Scan")
        self.scan_btn.setEnabled(True)
        QMessageBox.critical(self, "Scan failed", msg)

    # -- speaker / utterance ----------------------------------------------- #
    def _current_speaker(self) -> dict | None:
        rows = self.spk_table.selectionModel().selectedRows()
        if not rows:
            return None
        sid = self.spk_table.item(rows[0].row(), 0).data(Qt.UserRole)
        return self._speakers.get(sid)

    def _on_speaker_selected(self) -> None:
        self.utt_list.clear()
        info = self._current_speaker()
        if not info:
            return
        for row in info["rows"]:
            text = (row.get("sentence") or "(no transcript)").strip()
            item = QListWidgetItem(text)
            item.setData(Qt.UserRole, row.get("path"))
            self.utt_list.addItem(item)

    def _play_utterance(self, item: QListWidgetItem) -> None:
        rel = item.data(Qt.UserRole)
        if not rel:
            return
        mp3 = self._clips_src() / rel
        if not mp3.exists():
            QMessageBox.warning(self, "Missing mp3", f"{mp3} not found.")
            return
        self.player.play(mp3)

    # -- build (preview / promote) ----------------------------------------- #
    def _start_build(self, dest_dir: Path, on_built) -> bool:
        info = self._current_speaker()
        if not info:
            QMessageBox.information(self, "No speaker", "Select a speaker first.")
            return False
        if self.builder and self.builder.isRunning():
            return False
        self._set_busy(True)
        self.status.setText("Building merged clip… (decoding mp3s)")
        self.builder = BuildWorker(
            info["speaker_id"],
            info["rows"],
            self._clips_src(),
            dest_dir,
            self.tsv_combo.currentText(),
        )
        self.builder.built.connect(on_built)
        self.builder.failed.connect(self._build_failed)
        self.builder.start()
        return True

    def _build_failed(self, msg: str) -> None:
        self._set_busy(False)
        self.status.setText("Build failed.")
        QMessageBox.critical(self, "Build failed", msg)

    def _preview_merged(self) -> None:
        self._start_build(self._preview_dir, self._on_preview_built)

    def _on_preview_built(self, clip) -> None:
        self._set_busy(False)
        if clip is None:
            self.status.setText("Not enough clean speech to build a clip (try a higher Min utts).")
            QMessageBox.information(self, "Too short", "This speaker yielded < 8 s of clean speech.")
            return
        wav = self._preview_dir / clip.clip_path
        self.status.setText(f"Preview: {clip.duration_seconds:.1f}s · {clip.gender}/{clip.age_band}/{clip.accent}")
        self.player.play(wav)

    def _promote(self) -> None:
        info = self._current_speaker()
        if not info:
            QMessageBox.information(self, "No speaker", "Select a speaker first.")
            return
        clip_id = f"cv_{info['speaker_id'][:12]}"
        if clip_id in self.store.ids():
            QMessageBox.information(self, "Already present", f"'{clip_id}' is already in the voicebank.")
            return
        self._start_build(self.store.dir, self._on_promote_built)

    def _on_promote_built(self, clip) -> None:
        self._set_busy(False)
        if clip is None:
            self.status.setText("Not enough clean speech to build a clip.")
            QMessageBox.information(self, "Too short", "This speaker yielded < 8 s of clean speech.")
            return
        if clip.id in self.store.ids():
            self.status.setText(f"'{clip.id}' already present; not re-added.")
            return
        self.store.add(clip)
        self.on_change()
        self.status.setText(f"Promoted '{clip.id}' ({clip.duration_seconds:.1f}s) to the voicebank.")

    def _set_busy(self, busy: bool) -> None:
        self.preview_btn.setEnabled(not busy)
        self.promote_btn.setEnabled(not busy)


# --------------------------------------------------------------------------- #
#  Casting tab — assign voicebank clips to characters in a 04_voice_assignments
# --------------------------------------------------------------------------- #
class CastingTab(QWidget):
    CHAR_COLS = ["character", "target", "assigned clip", "ok?"]
    VB_COLS = ["id", "gender", "age", "accent"]

    def __init__(self, store: VoicebankStore, player: AudioPlayer, casting_file: Path | None) -> None:
        super().__init__()
        self.store = store
        self.player = player
        self.casting: BookCasting | None = None
        self.path: Path | None = None
        self.dirty = False

        layout = QVBoxLayout(self)

        bar = QHBoxLayout()
        self.open_btn = QPushButton("Open assignments…")
        self.open_btn.clicked.connect(self._open)
        self.save_btn = QPushButton("💾 Save")
        self.save_btn.clicked.connect(self._save)
        self.reload_btn = QPushButton("⟳ Reload")
        self.reload_btn.clicked.connect(self._reload)
        self.file_label = QLabel("(no file loaded)")
        bar.addWidget(self.open_btn)
        bar.addWidget(self.reload_btn)
        bar.addWidget(self.save_btn)
        bar.addWidget(self.file_label, 1)
        layout.addLayout(bar)

        splitter = QSplitter(Qt.Horizontal)

        # -- characters ---------------------------------------------------- #
        self.char_table = QTableWidget(0, len(self.CHAR_COLS))
        self.char_table.setHorizontalHeaderLabels(self.CHAR_COLS)
        self.char_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.char_table.setSelectionMode(QTableWidget.SingleSelection)
        self.char_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.char_table.verticalHeader().setVisible(False)
        self.char_table.itemSelectionChanged.connect(self._on_char_selected)
        splitter.addWidget(self.char_table)

        # -- LLM ranked candidates ----------------------------------------- #
        mid = QWidget()
        mid_l = QVBoxLayout(mid)
        self.target_label = QLabel("Select a character.")
        self.target_label.setWordWrap(True)
        mid_l.addWidget(self.target_label)
        cand_box = QGroupBox("LLM ranked candidates (double-click to play)")
        cand_l = QVBoxLayout(cand_box)
        self.cand_list = QListWidget()
        self.cand_list.itemDoubleClicked.connect(lambda it: self._play_clip(it.data(Qt.UserRole)))
        self.cand_list.currentItemChanged.connect(self._on_cand_changed)
        cand_l.addWidget(self.cand_list)
        self.reason_label = QLabel("")
        self.reason_label.setWordWrap(True)
        self.reason_label.setStyleSheet("color: gray;")
        cand_l.addWidget(self.reason_label)
        self.assign_cand_btn = QPushButton("➜ Assign this candidate")
        self.assign_cand_btn.clicked.connect(self._assign_candidate)
        cand_l.addWidget(self.assign_cand_btn)
        mid_l.addWidget(cand_box, 1)
        splitter.addWidget(mid)

        # -- full voicebank, cast anything --------------------------------- #
        vb = QWidget()
        vb_l = QVBoxLayout(vb)
        self.vb_filter = QLineEdit()
        self.vb_filter.setPlaceholderText("filter voicebank… (id / gender / age / accent)")
        self.vb_filter.textChanged.connect(self._apply_vb_filter)
        vb_l.addWidget(self.vb_filter)
        self.vb_table = QTableWidget(0, len(self.VB_COLS))
        self.vb_table.setHorizontalHeaderLabels(self.VB_COLS)
        self.vb_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.vb_table.setSelectionMode(QTableWidget.SingleSelection)
        self.vb_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.vb_table.verticalHeader().setVisible(False)
        self.vb_table.doubleClicked.connect(
            lambda: self._play_clip(self._selected_vb_id())
        )
        vb_l.addWidget(self.vb_table, 1)
        self.assign_vb_btn = QPushButton("➜ Cast selected clip to character")
        self.assign_vb_btn.clicked.connect(self._assign_voicebank)
        vb_l.addWidget(self.assign_vb_btn)
        splitter.addWidget(vb)

        splitter.setSizes([360, 360, 360])
        layout.addWidget(splitter, 1)

        self.status = QLabel("")
        layout.addWidget(self.status)

        self.refresh_voicebank()
        if casting_file:
            self._load(casting_file)
        self._update_buttons()

    # -- file I/O ---------------------------------------------------------- #
    def _open(self) -> None:
        start = str(self.path.parent if self.path else (REPO_ROOT / "artifacts"))
        path, _ = QFileDialog.getOpenFileName(
            self, "Open voice assignments", start, "Voice assignments (*.json)"
        )
        if path:
            if not self._confirm_discard():
                return
            self._load(Path(path))

    def _load(self, path: Path) -> None:
        try:
            self.casting = BookCasting.model_validate_json(path.read_text(encoding="utf-8"))
        except Exception as e:
            QMessageBox.critical(self, "Could not load", f"{path}\n\n{e}")
            return
        self.path = path
        self.dirty = False
        self.file_label.setText(f"{path}  ·  {self.casting.book_id}")
        self._refresh_chars()
        self.status.setText(f"Loaded {len(self.casting.castings)} character(s).")
        self._update_buttons()

    def _reload(self) -> None:
        if self.path and self._confirm_discard():
            self._load(self.path)

    def _save(self) -> None:
        if not (self.casting and self.path):
            return
        try:
            self.path.write_text(self.casting.model_dump_json(indent=2), encoding="utf-8")
        except Exception as e:
            QMessageBox.critical(self, "Could not save", str(e))
            return
        self.dirty = False
        self.status.setText(f"Saved {self.path}")
        self._update_buttons()

    def _confirm_discard(self) -> bool:
        if not self.dirty:
            return True
        return (
            QMessageBox.question(
                self, "Discard changes?", "There are unsaved casting changes. Discard them?"
            )
            == QMessageBox.Yes
        )

    # -- voicebank pane ---------------------------------------------------- #
    def refresh_voicebank(self) -> None:
        clips = sorted(self.store.clips, key=lambda c: c.id)
        self.vb_table.setRowCount(len(clips))
        for r, c in enumerate(clips):
            for col, val in enumerate([c.id, c.gender, c.age_band, c.accent]):
                item = QTableWidgetItem(val)
                if col == 0:
                    item.setData(Qt.UserRole, c.id)
                self.vb_table.setItem(r, col, item)
        self.vb_table.resizeColumnsToContents()
        self._apply_vb_filter()

    def _apply_vb_filter(self) -> None:
        needle = self.vb_filter.text().strip().lower()
        for r in range(self.vb_table.rowCount()):
            hay = " ".join(self.vb_table.item(r, c).text() for c in range(self.vb_table.columnCount())).lower()
            self.vb_table.setRowHidden(r, bool(needle) and needle not in hay)

    def _selected_vb_id(self) -> str | None:
        rows = self.vb_table.selectionModel().selectedRows()
        if not rows:
            return None
        return self.vb_table.item(rows[0].row(), 0).data(Qt.UserRole)

    # -- characters / candidates ------------------------------------------ #
    def _clip_by_id(self, clip_id: str | None) -> VoiceClip | None:
        if not clip_id:
            return None
        return next((c for c in self.store.clips if c.id == clip_id), None)

    def _refresh_chars(self) -> None:
        if not self.casting:
            return
        row = self.char_table.currentRow()
        self.char_table.setRowCount(len(self.casting.castings))
        for r, cast in enumerate(self.casting.castings):
            clip = self._clip_by_id(cast.assigned_clip_id)
            ok = "✓" if clip else ("—" if not cast.assigned_clip_id else "missing!")
            values = [
                cast.character_name,
                f"{cast.target.gender}/{cast.target.age_band}",
                cast.assigned_clip_id or "(none)",
                ok,
            ]
            for col, val in enumerate(values):
                self.char_table.setItem(r, col, QTableWidgetItem(val))
        self.char_table.resizeColumnsToContents()
        if 0 <= row < self.char_table.rowCount():
            self.char_table.selectRow(row)

    def _current_cast(self):
        if not self.casting:
            return None
        rows = self.char_table.selectionModel().selectedRows()
        if not rows:
            return None
        idx = rows[0].row()
        if 0 <= idx < len(self.casting.castings):
            return self.casting.castings[idx]
        return None

    def _on_char_selected(self) -> None:
        self.cand_list.clear()
        self.reason_label.setText("")
        cast = self._current_cast()
        if not cast:
            self.target_label.setText("Select a character.")
            return
        t = cast.target
        kw = ", ".join(t.accent_keywords + t.timbre_keywords + t.manner_keywords) or "—"
        desc = f"\nDescriptor: {cast.voice_descriptor}" if cast.voice_descriptor else ""
        self.target_label.setText(
            f"<b>{cast.character_name}</b> — target {t.gender}/{t.age_band}<br>keywords: {kw}{desc}"
        )
        for rc in cast.ranked:
            mark = "★ " if rc.clip_id == cast.assigned_clip_id else ""
            item = QListWidgetItem(f"{mark}{rc.score:.2f}  {rc.clip_id}")
            item.setData(Qt.UserRole, rc.clip_id)
            item.setData(Qt.UserRole + 1, rc.reason)
            self.cand_list.addItem(item)

    def _on_cand_changed(self, cur, _prev) -> None:
        self.reason_label.setText(cur.data(Qt.UserRole + 1) if cur else "")

    def _play_clip(self, clip_id: str | None) -> None:
        clip = self._clip_by_id(clip_id)
        if not clip:
            self.status.setText(f"Clip '{clip_id}' is not in the current voicebank.")
            return
        wav = self.store.dir / clip.clip_path
        if not wav.exists():
            QMessageBox.warning(self, "Missing wav", f"{wav} not found.")
            return
        self.player.play(wav)

    def _assign(self, clip_id: str) -> None:
        cast = self._current_cast()
        if not cast:
            QMessageBox.information(self, "No character", "Select a character first.")
            return
        if cast.assigned_clip_id == clip_id:
            self.status.setText(f"{cast.character_name} already cast to {clip_id}.")
            return
        clip = self._clip_by_id(clip_id)
        if clip and (clip.gender != cast.target.gender or clip.age_band != cast.target.age_band):
            if (
                QMessageBox.question(
                    self,
                    "Mismatch",
                    f"{clip_id} is {clip.gender}/{clip.age_band} but {cast.character_name}'s "
                    f"target is {cast.target.gender}/{cast.target.age_band}.\nCast it anyway?",
                )
                != QMessageBox.Yes
            ):
                return
        cast.assigned_clip_id = clip_id
        self.dirty = True
        self._refresh_chars()
        self._on_char_selected()
        self.status.setText(f"Cast {cast.character_name} → {clip_id} (unsaved).")
        self._update_buttons()

    def _assign_candidate(self) -> None:
        item = self.cand_list.currentItem()
        if not item:
            QMessageBox.information(self, "No candidate", "Select a ranked candidate first.")
            return
        self._assign(item.data(Qt.UserRole))

    def _assign_voicebank(self) -> None:
        clip_id = self._selected_vb_id()
        if not clip_id:
            QMessageBox.information(self, "No clip", "Select a voicebank clip first.")
            return
        self._assign(clip_id)

    def _update_buttons(self) -> None:
        loaded = self.casting is not None
        self.save_btn.setEnabled(loaded and self.dirty)
        self.reload_btn.setEnabled(self.path is not None)
        self.save_btn.setText("💾 Save *" if self.dirty else "💾 Save")


# --------------------------------------------------------------------------- #
#  TTS Lab tab — load Dramabox once, A/B-test how prompts/directions sound
# --------------------------------------------------------------------------- #
class TtsLabTab(QWidget):
    """Audition TTS prompts against a chosen voice clip — Dramabox or VibeVoice.

    The model is loaded ONCE, manually, via the "Load model" button — never on
    construction or when other tabs are used (either engine is 15-18 GB in
    VRAM). Only ONE engine can be loaded per Studio session: the two need
    conflicting transformers versions (Dramabox ≥4.52, VibeVoice ==4.51.3 —
    DESIGN.md §16.6), and the Load button warns which one the venv supports.

    Dramabox: use the format buttons to hear how the same description + line
    compose into different prompt shapes (`desc, "line"` vs `[desc] "line"` …).
    VibeVoice: the prompt box is a `Speaker N:` script (plain text auto-wraps
    as Speaker 1); voice identity comes ONLY from the reference clip, so one
    is required, and description / stg_scale / format buttons don't apply.
    Keep the seed fixed to isolate the prompt.
    """

    CFG_DEFAULTS = {"dramabox": 2.5, "vibevoice": 1.3}

    # (label, template using {desc} and {line})
    FORMATS = [
        ("comma", '{desc}, "{line}"'),
        ("brackets", '[{desc}] "{line}"'),
        ("brackets+NL", '[{desc}]\n"{line}"'),
        ("period", '{desc}. "{line}"'),
        ("colon", '{desc}: "{line}"'),
        ("dash", '{desc} — "{line}"'),
        ("line only", '"{line}"'),
    ]

    def __init__(self, store: VoicebankStore, player: AudioPlayer) -> None:
        super().__init__()
        self.store = store
        self.player = player
        self.client = None  # DramaboxClient | VibeVoiceClient once loaded
        self.engine: str | None = None  # set on successful load
        self.loader: TtsLoadWorker | None = None
        self.gen: TtsGenWorker | None = None
        self._tts_dir = Path(tempfile.mkdtemp(prefix="vbstudio_tts_"))
        self._gen_count = 0

        layout = QVBoxLayout(self)

        # --- model load row ---------------------------------------------- #
        load_box = QGroupBox("Model")
        load_l = QHBoxLayout(load_box)
        load_l.addWidget(QLabel("Engine:"))
        self.engine_combo = QComboBox()
        self.engine_combo.addItems(["dramabox", "vibevoice"])
        load_l.addWidget(self.engine_combo)
        load_l.addWidget(QLabel("Device:"))
        self.device_combo = QComboBox()
        self.device_combo.addItems(["auto", "cuda", "mps", "cpu"])
        load_l.addWidget(self.device_combo)
        self.load_btn = QPushButton("⚙ Load model")
        self.load_btn.clicked.connect(self._load_model)
        load_l.addWidget(self.load_btn)
        self.model_status = QLabel("Not loaded — the engine loads only when you click Load (one per session).")
        self.model_status.setStyleSheet("color: gray;")
        load_l.addWidget(self.model_status, 1)
        layout.addWidget(load_box)

        # --- voice selection --------------------------------------------- #
        voice_row = QHBoxLayout()
        voice_row.addWidget(QLabel("Voice:"))
        self.voice_combo = QComboBox()
        self.voice_combo.currentIndexChanged.connect(self._on_voice_changed)
        voice_row.addWidget(self.voice_combo, 1)
        self.play_ref_btn = QPushButton("▶ Play reference")
        self.play_ref_btn.clicked.connect(self._play_reference)
        voice_row.addWidget(self.play_ref_btn)
        layout.addLayout(voice_row)

        # --- prompt composer --------------------------------------------- #
        compose = QGroupBox("Compose")
        compose_l = QFormLayout(compose)
        self.desc_edit = QLineEdit()
        self.desc_edit.setPlaceholderText("voice / performance description (e.g. adult, British, male, weary)")
        desc_row = QHBoxLayout()
        desc_row.addWidget(self.desc_edit, 1)
        from_clip = QPushButton("↩ from clip")
        from_clip.setToolTip("Fill the description from the selected voice clip's metadata")
        from_clip.clicked.connect(self._desc_from_clip)
        desc_row.addWidget(from_clip)
        compose_l.addRow("Description:", self._wrap(desc_row))

        self.line_edit = QLineEdit()
        self.line_edit.setPlaceholderText("the spoken line, e.g. The duke turned from the window.")
        compose_l.addRow("Line:", self.line_edit)

        fmt_row = QHBoxLayout()
        fmt_row.addWidget(QLabel("Insert as:"))
        for label, template in self.FORMATS:
            b = QPushButton(label)
            b.setToolTip(template.replace("\n", "\\n"))
            b.clicked.connect(lambda _=False, t=template: self._apply_format(t))
            fmt_row.addWidget(b)
        fmt_row.addStretch(1)
        compose_l.addRow("", self._wrap(fmt_row))
        layout.addWidget(compose)

        # --- the actual prompt sent to the engine ------------------------- #
        self.prompt_label = QLabel("Prompt sent to Dramabox (edit freely):")
        layout.addWidget(self.prompt_label)
        self.prompt_edit = QPlainTextEdit()
        self.prompt_edit.setPlaceholderText('e.g.  adult, British, male, weary, "You\'re late."')
        self.prompt_edit.setFixedHeight(80)
        layout.addWidget(self.prompt_edit)

        # --- generation params ------------------------------------------- #
        params = QHBoxLayout()
        self.seed_spin = QSpinBox()
        self.seed_spin.setRange(0, 2_147_483_647)
        self.seed_spin.setValue(42)
        self.cfg_spin = QDoubleSpinBox()
        self.cfg_spin.setRange(0.0, 10.0)
        self.cfg_spin.setSingleStep(0.5)
        self.cfg_spin.setValue(2.5)
        self.stg_spin = QDoubleSpinBox()
        self.stg_spin.setRange(0.0, 10.0)
        self.stg_spin.setSingleStep(0.5)
        self.stg_spin.setValue(1.5)
        for lbl, w, tip in [
            ("Seed:", self.seed_spin, "Keep fixed to compare prompts fairly."),
            ("cfg_scale:", self.cfg_spin, "Prompt adherence."),
            ("stg_scale:", self.stg_spin, "Spatial/temporal guidance."),
        ]:
            label = QLabel(lbl)
            label.setToolTip(tip)
            params.addWidget(label)
            params.addWidget(w)
        params.addStretch(1)
        self.gen_btn = QPushButton("▶ Generate & play")
        self.gen_btn.clicked.connect(self._generate)
        params.addWidget(self.gen_btn)
        layout.addLayout(params)

        # --- history ----------------------------------------------------- #
        hist_box = QGroupBox("History (double-click to replay)")
        hist_l = QVBoxLayout(hist_box)
        self.history = QListWidget()
        self.history.itemDoubleClicked.connect(self._replay)
        hist_l.addWidget(self.history)
        layout.addWidget(hist_box, 1)

        self.status = QLabel("")
        layout.addWidget(self.status)

        # Connected last: the handler touches cfg/stg/prompt widgets above.
        self.engine_combo.currentTextChanged.connect(self._on_engine_changed)

        self.refresh_voices()
        self._update_enabled()

    @staticmethod
    def _wrap(layout) -> QWidget:
        w = QWidget()
        w.setLayout(layout)
        return w

    # -- engine selection ---------------------------------------------------- #
    def _on_engine_changed(self, engine: str) -> None:
        self.cfg_spin.setValue(self.CFG_DEFAULTS[engine])
        self.stg_spin.setEnabled(engine == "dramabox")
        if engine == "vibevoice":
            self.prompt_label.setText("Script sent to VibeVoice (edit freely):")
            self.prompt_edit.setPlaceholderText(
                "e.g.  Speaker 1: You're late.\n"
                "Plain text auto-wraps as Speaker 1; every Speaker N clones the "
                "selected reference. Description / stg_scale / format buttons are Dramabox-only."
            )
            self.status.setText(
                "VibeVoice: a reference clip is required (it IS the voice); cfg default 1.3."
            )
        else:
            self.prompt_label.setText("Prompt sent to Dramabox (edit freely):")
            self.prompt_edit.setPlaceholderText('e.g.  adult, British, male, weary, "You\'re late."')
            self.status.setText("")

    def _confirm_engine(self, engine: str) -> bool:
        """One-engine-per-session warning, keyed to the installed transformers.

        The engines need conflicting versions (DESIGN.md §16.6): Dramabox's
        Gemma encoder needs ≥4.52, VibeVoice's fork pins ==4.51.3 — so the
        venv's current transformers decides which one this session can load.
        """
        ver = _installed_transformers_version()
        try:
            major_minor = tuple(int(p) for p in ver.split(".")[:2])
        except Exception:
            major_minor = (0, 0)
        if engine == "dramabox":
            compatible = major_minor >= (4, 52)
            fix = "uv pip install -U 'transformers>=4.52'"
        else:
            compatible = (0, 0) < major_minor < (4, 52)
            fix = "uv pip install -e external/VibeVoice"
        base = (
            "Only one TTS engine can be loaded per Studio session — the installed\n"
            "transformers version decides which one works (DESIGN.md §16.6):\n"
            "  •  dramabox needs transformers ≥ 4.52\n"
            "  •  vibevoice pins transformers == 4.51.3\n\n"
            f"Installed transformers: {ver or 'not found'}"
        )
        if compatible:
            QMessageBox.information(
                self, "One engine per session", f"{base} — compatible with {engine}."
            )
            return True
        return (
            QMessageBox.warning(
                self,
                "Engine / transformers mismatch",
                f"{base} — {engine} will likely FAIL to run.\n\n"
                f"Fix, then restart the Studio:\n    {fix}\n\nTry to load anyway?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            == QMessageBox.Yes
        )

    # -- voices ------------------------------------------------------------ #
    def refresh_voices(self) -> None:
        current = self.voice_combo.currentData()
        self.voice_combo.blockSignals(True)
        self.voice_combo.clear()
        self.voice_combo.addItem("(no reference — Dramabox default voice; VibeVoice requires one)", None)
        for c in sorted(self.store.clips, key=lambda c: c.id):
            self.voice_combo.addItem(
                f"{c.id}  ·  {c.gender}/{c.age_band}/{c.accent}", c.id
            )
        # Restore prior selection if it survived a reload.
        idx = self.voice_combo.findData(current) if current else 0
        self.voice_combo.setCurrentIndex(max(0, idx))
        self.voice_combo.blockSignals(False)

    def _selected_clip(self) -> VoiceClip | None:
        clip_id = self.voice_combo.currentData()
        if not clip_id:
            return None
        return next((c for c in self.store.clips if c.id == clip_id), None)

    def _voice_ref(self) -> Path | None:
        clip = self._selected_clip()
        if not clip:
            return None
        wav = self.store.dir / clip.clip_path
        return wav if wav.exists() else None

    def _on_voice_changed(self) -> None:
        # Prefill the description from the clip the first time, if still blank.
        if not self.desc_edit.text().strip():
            self._desc_from_clip()

    def _desc_from_clip(self) -> None:
        clip = self._selected_clip()
        if clip:
            self.desc_edit.setText(descriptor_from_clip(clip))

    def _play_reference(self) -> None:
        ref = self._voice_ref()
        if not ref:
            self.status.setText("No reference clip selected (or its wav is missing).")
            return
        self.player.play(ref)

    # -- compose ----------------------------------------------------------- #
    def _apply_format(self, template: str) -> None:
        desc = self.desc_edit.text().strip()
        line = self.line_edit.text().strip()
        if not line:
            self.status.setText("Enter a line first.")
            return
        self.prompt_edit.setPlainText(template.format(desc=desc, line=line))

    # -- model load -------------------------------------------------------- #
    def _load_model(self) -> None:
        if self.client is not None or (self.loader and self.loader.isRunning()):
            return
        engine = self.engine_combo.currentText()
        if not self._confirm_engine(engine):
            return
        self.load_btn.setEnabled(False)
        self.device_combo.setEnabled(False)
        self.engine_combo.setEnabled(False)
        self.model_status.setText(f"Loading {engine}… (first run downloads 15-18 GB)")
        self.loader = TtsLoadWorker(engine, self.device_combo.currentText())
        self.loader.loaded.connect(self._on_model_loaded)
        self.loader.failed.connect(self._on_model_failed)
        self.loader.start()

    def _on_model_loaded(self, client, engine: str, device: str) -> None:
        self.client = client
        self.engine = engine
        self.model_status.setText(f"✓ {engine} loaded on {device} — ready.")
        self.model_status.setStyleSheet("color: green;")
        self._update_enabled()

    def _on_model_failed(self, msg: str) -> None:
        self.load_btn.setEnabled(True)
        self.device_combo.setEnabled(True)
        self.engine_combo.setEnabled(True)
        self.model_status.setText("Load failed.")
        self.model_status.setStyleSheet("color: #b00;")
        QMessageBox.critical(self, "TTS engine load failed", msg)

    # -- generate ---------------------------------------------------------- #
    def _generate(self) -> None:
        if self.client is None:
            QMessageBox.information(self, "Model not loaded", "Click 'Load model' first.")
            return
        if self.gen and self.gen.isRunning():
            return
        prompt = self.prompt_edit.toPlainText().strip()
        if not prompt:
            self.status.setText("Enter or compose a prompt first.")
            return
        engine = self.engine or "dramabox"
        voice_ref = self._voice_ref()
        n_speakers = 1
        if engine == "vibevoice":
            if voice_ref is None:
                QMessageBox.information(
                    self,
                    "Reference required",
                    "VibeVoice has no default voice — the reference clip IS the "
                    "voice (zero-shot cloning). Select a voicebank clip first.",
                )
                return
            prompt, n_speakers = _to_vibevoice_script(prompt)
        self._gen_count += 1
        out = self._tts_dir / f"gen_{self._gen_count:03d}.wav"
        self.gen_btn.setEnabled(False)
        self.status.setText("Generating… (this can take a while)")
        self.gen = TtsGenWorker(
            self.client,
            engine=engine,
            prompt=prompt,
            output_path=out,
            voice_ref=voice_ref,
            n_speakers=n_speakers,
            seed=self.seed_spin.value(),
            cfg_scale=self.cfg_spin.value(),
            stg_scale=self.stg_spin.value(),
        )
        self.gen.done.connect(self._on_generated)
        self.gen.failed.connect(self._on_gen_failed)
        self.gen.start()

    def _on_generated(self, wav_path: str) -> None:
        self.gen_btn.setEnabled(True)
        wav = Path(wav_path)
        prompt = self.prompt_edit.toPlainText().strip()
        ref = self._voice_ref()
        ref_name = ref.stem if ref else "default"
        oneline = prompt.replace("\n", "\\n")
        item = QListWidgetItem(
            f"#{self._gen_count}  [{self.engine or 'dramabox'} · {ref_name} · seed {self.seed_spin.value()}]  {oneline}"
        )
        item.setData(Qt.UserRole, wav_path)
        item.setToolTip(prompt)
        self.history.insertItem(0, item)
        self.status.setText(f"Generated #{self._gen_count} → playing.")
        self.player.play(wav)

    def _on_gen_failed(self, msg: str) -> None:
        self.gen_btn.setEnabled(True)
        self.status.setText("Generation failed.")
        QMessageBox.critical(self, "Generation failed", msg)

    def _replay(self, item: QListWidgetItem) -> None:
        wav = item.data(Qt.UserRole)
        if wav and Path(wav).exists():
            self.player.play(Path(wav))

    def _update_enabled(self) -> None:
        ready = self.client is not None
        self.gen_btn.setEnabled(ready)


# --------------------------------------------------------------------------- #
#  Main window
# --------------------------------------------------------------------------- #
class MainWindow(QWidget):
    def __init__(self, store: VoicebankStore, cv_root: Path | None, casting_file: Path | None) -> None:
        super().__init__()
        self.setWindowTitle("ln-vox · Voicebank Studio")
        self.resize(1180, 700)
        self.player = AudioPlayer()

        self.tabs = QTabWidget()
        self.vb_tab = VoicebankTab(store, self.player)
        self.casting_tab = CastingTab(store, self.player, casting_file)
        self.import_tab = ImportTab(store, self.player, cv_root, on_change=self._on_voicebank_changed)
        self.tts_tab = TtsLabTab(store, self.player)
        self.vb_tab.on_change = self._on_voicebank_changed
        self.tabs.addTab(self.vb_tab, "Voicebank")
        self.tabs.addTab(self.import_tab, "Import from Common Voice")
        self.tabs.addTab(self.casting_tab, "Casting")
        self.tabs.addTab(self.tts_tab, "TTS Lab")

        layout = QVBoxLayout(self)
        layout.addWidget(self.tabs)

    def _on_voicebank_changed(self) -> None:
        self.vb_tab.refresh()
        self.casting_tab.refresh_voicebank()
        self.casting_tab._refresh_chars()
        self.tts_tab.refresh_voices()


def main() -> None:
    parser = argparse.ArgumentParser(description="ln-vox Voicebank Studio")
    parser.add_argument("--voicebank", default=str(REPO_ROOT / "voicebank"), help="Voicebank directory")
    parser.add_argument("--cv-root", default=None, help="Common Voice locale dir (has clips/ + *.tsv)")
    parser.add_argument("--data", default=str(REPO_ROOT / "data"), help="Where to auto-detect the CV corpus")
    parser.add_argument("--casting", default=None, help="A 04_voice_assignments.json to open in the Casting tab")
    parser.add_argument(
        "--artifacts", default=str(REPO_ROOT / "artifacts"), help="Where to auto-detect a casting file"
    )
    args = parser.parse_args()

    cv_root = Path(args.cv_root).expanduser() if args.cv_root else autodetect_cv_root(Path(args.data))
    casting_file = (
        Path(args.casting).expanduser() if args.casting else autodetect_casting_file(Path(args.artifacts))
    )

    app = QApplication(sys.argv)
    store = VoicebankStore(Path(args.voicebank).expanduser())
    win = MainWindow(store, cv_root, casting_file)
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
