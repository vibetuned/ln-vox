"""LLM-driven character → voice clip casting."""

import json
import re
from typing import Iterable

from lnvox.llm.client import LLMClient
from lnvox.llm.schemas import Character, VoiceProfileList
from lnvox.voices.schema import (
    BookCasting,
    CharacterCasting,
    MatchResult,
    VoiceClip,
    Voicebank,
    VoiceTarget,
    _RankedChoice,
)


# Defaults applied to the Narrator when there's no --narrator-clip, no prior
# volume to inherit from, and no existing profile. Picked to give first-time
# users a sensible audiobook voice without forcing them to choose up front.
DEFAULT_NARRATOR_GENDER = "female"
DEFAULT_NARRATOR_AGE = "adult"
DEFAULT_NARRATOR_ACCENT = "england"


# Cheap heuristic so we can cast the Narrator without an extra LLM call to
# extract gender/age. The voice_descriptor written by s3 already contains
# phrases like "mid-thirties male" or "young adult female".
_FEMALE_HINTS = re.compile(
    r"\b(female|woman|girl|soprano|alto|mezzo)\b", re.IGNORECASE
)
_AGE_HINTS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\b(teen|teenage|teenager)\b", re.IGNORECASE), "teen"),
    (re.compile(r"\b(child|kid|young child)\b", re.IGNORECASE), "child"),
    (re.compile(r"\b(elder|elderly|old|sixties|seventies|eighties|nineties)\b", re.IGNORECASE), "elder"),
    (
        re.compile(r"\b(young adult|twenties|thirties|mid-?20s|mid-?30s|mid-thirties|mid-twenties)\b", re.IGNORECASE),
        "young_adult",
    ),
    (
        re.compile(r"\b(adult|middle-?aged|fourties|forties|fifties)\b", re.IGNORECASE),
        "adult",
    ),
]


def _derive_narrator_demographics(voice_descriptor: str) -> tuple[str, str]:
    """Pull (gender, age_band) out of a free-text descriptor like
    'mid-thirties male, smooth baritone'. Falls back to ('male', 'adult')."""
    gender = "female" if _FEMALE_HINTS.search(voice_descriptor) else "male"
    age = "adult"
    for pat, band in _AGE_HINTS:
        if pat.search(voice_descriptor):
            age = band
            break
    return gender, age


# Common Voice accent code → English adjective for descriptor text.
_ACCENT_ADJ: dict[str, str] = {
    "us": "American",
    "england": "British",
    "scotland": "Scottish",
    "wales": "Welsh",
    "ireland": "Irish",
    "australia": "Australian",
    "canada": "Canadian",
    "indian": "Indian",
    "newzealand": "New Zealand",
    "african": "African",
    "philippines": "Filipino",
    "hongkong": "Hong Kong",
    "singapore": "Singaporean",
    "malaysia": "Malaysian",
    "bermuda": "West Indian",
    # "any" / "other" / unknown → no adjective.
}


def descriptor_from_clip(
    clip: VoiceClip, role_tail: str = "warm clear voice, measured and engaging storyteller"
) -> str:
    """Compose a Dramabox-ready descriptor purely from clip metadata.

    Used for the Narrator (no character personality to draw from) and any
    other character where a manual override should lock the descriptor in.
    """
    age = clip.age_band.replace("_", " ")
    accent_adj = _ACCENT_ADJ.get(clip.accent, "")
    gender_word = "female" if clip.gender == "female" else "male"
    parts: list[str] = [age]
    if accent_adj:
        parts.append(accent_adj)
    parts.append(gender_word)
    parts.append(role_tail)
    return ", ".join(parts)


SYSTEM = (
    "You match audiobook characters to voice-bank reference clips. "
    "Output a single raw JSON object that matches the requested schema. "
    "Do NOT wrap the output in markdown code fences. "
    "Do NOT include any prose, commentary, or explanation around the JSON."
)


def _norm_name(s: str) -> str:
    return " ".join(s.lower().strip().split())


class PriorCastIndex:
    """Cross-volume casting memory: identity keys → most recent prior casting.

    s1's canonical names drift between volumes ("Kamijou Touma" / "Kamijou" /
    "Touma Kamijou"), and series casts rotate so characters routinely skip a
    volume. Exact-name matching against only the latest volume loses the voice
    on either event. This index folds EVERY prior volume in (call `add_volume`
    oldest → newest; newer entries win per key) and keys each casting by every
    name and alias the character was known by, plus token-sorted variants so a
    flipped name order still matches.
    """

    def __init__(self) -> None:
        # key → (casting, key_is_the_entry's_canonical_name)
        self._by_key: dict[str, tuple[CharacterCasting, bool]] = {}

    @staticmethod
    def _keys(name: str, aliases: Iterable[str] = ()) -> list[tuple[str, bool]]:
        """(key, from_canonical_name) pairs in priority order: the canonical
        name (and its token-sorted form, so "mikoto misaka" == "misaka mikoto")
        first, then each alias."""
        keys: list[tuple[str, bool]] = []
        seen: set[str] = set()
        for i, nm in enumerate((name, *aliases)):
            base = _norm_name(nm)
            if not base:
                continue
            for k in (base, " ".join(sorted(base.split()))):
                if k not in seen:
                    seen.add(k)
                    keys.append((k, i == 0))
        return keys

    def add_volume(
        self, casting: BookCasting, cast: Iterable[Character] = ()
    ) -> None:
        """Fold one prior volume's assignments in.

        `cast` is that volume's character list — the only source of aliases
        (casting entries store just the canonical name). A key claimed by two
        different characters in the same volume identifies neither, so it's
        dropped for that volume rather than resolved arbitrarily.
        """
        aliases_by_name: dict[str, list[str]] = {c.name: c.aliases for c in cast}
        claimed: dict[str, str] = {}
        volume_keys: dict[str, tuple[CharacterCasting, bool]] = {}
        ambiguous: set[str] = set()
        for entry in casting.castings:
            if not entry.assigned_clip_id:
                continue  # unresolved casting: nothing worth inheriting
            keys = self._keys(
                entry.character_name, aliases_by_name.get(entry.character_name, [])
            )
            for k, canonical in keys:
                owner = claimed.get(k)
                if owner is not None and owner != entry.character_name:
                    ambiguous.add(k)
                    continue
                claimed[k] = entry.character_name
                volume_keys[k] = (entry, canonical)
        for k in ambiguous:
            volume_keys.pop(k, None)
        self._by_key.update(volume_keys)

    def __len__(self) -> int:
        return len(self._by_key)

    def lookup(
        self, name: str, aliases: Iterable[str] = (), gender: str = ""
    ) -> CharacterCasting | None:
        """Most recent prior casting for this identity, or None.

        A match is trusted as-is only when the key is the canonical name on
        BOTH sides. Any alias-mediated match requires explicit gender
        agreement instead: s1 sometimes attaches a companion's name as an
        alias, and a cross-gender "match" through such an alias is exactly how
        one character steals another's voice.
        """
        for k, cur_canonical in self._keys(name, aliases):
            hit = self._by_key.get(k)
            if hit is None:
                continue
            entry, prior_canonical = hit
            if cur_canonical and prior_canonical:
                return entry
            if gender in ("male", "female") and entry.target.gender == gender:
                return entry
        return None


def infer_target(
    client: LLMClient,
    character: Character,
    voice_descriptor: str = "",
    profile_accent: str = "any",
) -> VoiceTarget:
    user = client.render(
        "voice_target.jinja",
        name=character.name,
        gender=character.gender,
        approx_age=character.approx_age,
        description=character.description,
        voice_descriptor=voice_descriptor or "",
        profile_accent=profile_accent or "any",
    )
    return client.structured(
        system=SYSTEM, user=user, schema=VoiceTarget, max_tokens=1024
    )


def _hard_filter(voicebank: Voicebank, target: VoiceTarget) -> list[VoiceClip]:
    """Apply non-negotiable filters: same gender + same age_band."""
    return [
        c
        for c in voicebank.clips
        if c.gender == target.gender and c.age_band == target.age_band
    ]


def _soft_filter_by_accent(
    candidates: list[VoiceClip], target: VoiceTarget, max_keep: int = 80
) -> list[VoiceClip]:
    """If accent keywords specified and we have more candidates than we want
    to send to the LLM, prefer accent matches first.

    Both `target.accent_keywords` and `clip.accent` are normalized to
    Common Voice accent codes (us / england / indian / canada / ...), so an
    exact case-insensitive match is enough — no aliasing or substring rules.
    """
    if not target.accent_keywords or "any" in target.accent_keywords:
        return candidates[:max_keep]
    wanted = {k.lower() for k in target.accent_keywords}
    preferred = [c for c in candidates if c.accent.lower() in wanted]
    rest = [c for c in candidates if c.accent.lower() not in wanted]
    return (preferred + rest)[:max_keep]


def _offered_choices(
    ranked: MatchResult, candidates: list[VoiceClip]
) -> list[_RankedChoice]:
    """The ranked choices whose clip_id is one of the candidates offered.

    The ranking LLM only ever sees `candidates`, so any other id is invented
    (or copied from the prompt's example). Committing it would pass here and
    fail only at s4, which refuses a casting that names unknown clips — after
    the rest of the LLM phase has been paid for.
    """
    offered = {c.id for c in candidates}
    return [rc for rc in ranked.ranked_choices if rc.clip_id in offered]


def _fallback_choice(
    ranked: MatchResult, candidates: list[VoiceClip]
) -> tuple[_RankedChoice, str]:
    """Deterministic pick when the ranking offered nothing usable: the first
    candidate in filter order (accent matches first, then manifest order).
    Returns the choice and an `audit_note` recording why it was made."""
    n = len(ranked.ranked_choices)
    why = (
        "ranking returned no choice"
        if n == 0
        else f"none of the {n} ranked clip id(s) was among the {len(candidates)} offered"
    )
    choice = _RankedChoice(
        clip_id=candidates[0].id,
        score=0.0,
        reason=f"Fallback: {why}; first candidate in filter order.",
    )
    return choice, f"voice cast: {why}; fell back to the first candidate"


def rank_candidates(
    client: LLMClient,
    character: Character,
    target: VoiceTarget,
    candidates: list[VoiceClip],
    top_n: int = 3,
) -> MatchResult:
    if not candidates:
        return MatchResult(ranked_choices=[])

    char_payload = {
        "name": character.name,
        "gender": character.gender,
        "approx_age": character.approx_age,
        "description": character.description,
    }
    cand_payload = [
        {
            "id": c.id,
            "accent": c.accent,
            "sample_sentence": (c.sample_sentences[0] if c.sample_sentences else "")[:160],
            "duration": c.duration_seconds,
        }
        for c in candidates
    ]

    user = client.render(
        "voice_match.jinja",
        character_json=json.dumps(char_payload, ensure_ascii=False, indent=2),
        target_json=json.dumps(target.model_dump(), ensure_ascii=False, indent=2),
        candidates_json=json.dumps(cand_payload, ensure_ascii=False, indent=2),
        candidate_count=len(candidates),
        top_n=top_n,
    )

    # Budget: ~120 tokens per ranked choice.
    budget = max(1024, 200 * top_n + 512)
    return client.structured(
        system=SYSTEM, user=user, schema=MatchResult, max_tokens=budget
    )


def cast_book(
    client: LLMClient,
    book_id: str,
    cast: Iterable[Character],
    voicebank: Voicebank,
    *,
    profiles: VoiceProfileList | None = None,
    top_n: int = 3,
    max_candidates_per_char: int = 60,
    prior_index: PriorCastIndex | None = None,
    existing: BookCasting | None = None,
    narrator_clip_override: str | None = None,
    on_character_done=None,
) -> BookCasting:
    """Cast each character to a voicebank clip.

    Per character, the first source that applies wins:

      1. `existing` entry with `locked: true` (a deliberate hand edit)
      2. `narrator_clip_override` (Narrator only)
      3. `prior_index` match (continuity chains)
      4. `existing` entry, unlocked (keeps re-runs idempotent)
      5. fresh cast (LLM), or unassigned for a gender the bank doesn't cover

    Args:
        prior_index: If supplied (built from ALL prior volumes in the same
            series), characters whose name or aliases match a prior
            assignment keep that prior clip without an LLM call. Narrator
            also inherits unless `narrator_clip_override` is set.
        existing: This volume's own current casting (its
            04_voice_assignments.json), matched by name/alias like a prior
            volume. A locked entry beats everything, the override included:
            it is how a hand re-cast of a recurring character survives the
            next `voice cast`. An unlocked entry is the weakest layer — kept
            only when no prior volume knows the character, so continuity
            still wins but a first-appearance character isn't re-rolled.
            Whenever the assigned clip is the entry's own, its `locked`,
            `audit_note` and `voice_descriptor` carry over; a descriptor
            written for a different clip never does.
        narrator_clip_override: Explicit clip id for the Narrator. Overrides
            both the LLM auto-cast AND any prior-volume Narrator assignment.

    Raises:
        ValueError: `narrator_clip_override` is not in the voicebank. Checked
            before any LLM call — the synthesised Narrator is cast LAST, so a
            late check wastes every other character's calls.
    """

    profiles_by_name: dict = {}
    if profiles is not None:
        profiles_by_name = {p.name: p for p in profiles.profiles}

    # Voicebank lookup by clip id for resolving overrides + reuses.
    clip_by_id = {c.id: c for c in voicebank.clips}
    if narrator_clip_override and narrator_clip_override not in clip_by_id:
        raise ValueError(
            f"--narrator-clip {narrator_clip_override!r} not in voicebank."
        )

    cast = list(cast)
    # This volume's own entries, looked up the same way as a prior volume's
    # so an s1 re-run that renamed a character still finds its entry. The
    # current cast is the alias source, exactly as the entries were written.
    own_index: PriorCastIndex | None = None
    if existing is not None and existing.castings:
        own_index = PriorCastIndex()
        own_index.add_volume(existing, cast)

    # Synthesize a Narrator Character if it's not in the s1 cast already.
    # In the v2 order (voice cast before s3), there's no profiles file yet,
    # so we MUST look at every other source before falling back to defaults.
    # Same precedence as the per-character decision below:
    #   1. This volume's LOCKED Narrator → its demographics
    #   2. --narrator-clip override   → derive from the chosen clip
    #   3. Prior volume's casting (else this volume's unlocked one)
    #                                 → reuse that Narrator's demographics
    #   4. Existing profiles file     → legacy path (s3 ran before voice cast)
    #   5. Default (DEFAULT_NARRATOR_*: "female" / "adult") → first volume,
    #      no override
    if not any(c.name == "Narrator" for c in cast):
        n_gender = DEFAULT_NARRATOR_GENDER
        n_age = DEFAULT_NARRATOR_AGE
        n_desc = "Neutral audiobook narrator for a modern action/fantasy young-adult novel."
        own_n = own_index.lookup("Narrator") if own_index else None
        prior_n = prior_index.lookup("Narrator") if prior_index else None
        reuse_n = (
            own_n
            if own_n is not None
            and own_n.locked
            and own_n.assigned_clip_id in clip_by_id
            else None
        )
        if reuse_n is None and narrator_clip_override:
            override_clip = clip_by_id[narrator_clip_override]
            n_gender = override_clip.gender
            n_age = override_clip.age_band
        elif reuse_n is not None or prior_n is not None or own_n is not None:
            reuse_n = reuse_n or prior_n or own_n
            n_gender = reuse_n.target.gender or n_gender
            n_age = reuse_n.target.age_band or n_age
        elif "Narrator" in profiles_by_name:
            np = profiles_by_name["Narrator"]
            n_gender, n_age = _derive_narrator_demographics(np.voice_descriptor)
            n_desc = np.voice_descriptor
        cast.append(
            Character(
                name="Narrator",
                aliases=[],
                gender=n_gender,
                approx_age=n_age,
                description=n_desc,
                evidence=[],
            )
        )

    def _consistent(entry: CharacterCasting, character: Character) -> bool:
        """Whether a stored assignment is safe to reuse unattended.

        Its clip must still be in the bank. And a clip whose gender
        contradicts BOTH the current character and the stored target is an
        internally-inconsistent assignment (e.g. a hand-edit pointed at the
        wrong clip) — recast rather than propagate it through the series.
        """
        clip = clip_by_id.get(entry.assigned_clip_id)
        if clip is None:
            return False
        known = {character.gender, entry.target.gender} & {"male", "female"}
        return not (known and clip.gender not in known)

    def _descriptor(entry: CharacterCasting, character: Character) -> str:
        # Prefer the stored voice_descriptor, otherwise (legacy assignments)
        # derive one from the clip for the Narrator. Other characters keep an
        # empty descriptor — s3 writes theirs with full personality detail.
        if entry.voice_descriptor:
            return entry.voice_descriptor
        if character.name == "Narrator":
            return descriptor_from_clip(clip_by_id[entry.assigned_clip_id])
        return ""

    castings: list[CharacterCasting] = []

    def _emit(
        character: Character,
        casting: CharacterCasting,
        own: CharacterCasting | None,
        note: str = "",
    ) -> None:
        # Provenance survives as long as the clip does: when the clip picked
        # by another rule is the one this volume's entry already had, keep
        # that entry's note and (hand-written or clip-derived) descriptor.
        if own is not None and own.assigned_clip_id == casting.assigned_clip_id:
            if own.voice_descriptor:
                casting.voice_descriptor = own.voice_descriptor
            if own.audit_note:
                casting.audit_note = own.audit_note
        if note and not casting.audit_note:
            casting.audit_note = note
        castings.append(casting)
        if on_character_done:
            on_character_done(character, casting)

    for character in cast:
        own = (
            own_index.lookup(character.name, character.aliases, character.gender)
            if own_index
            else None
        )
        note = ""

        # --- A locked entry of this volume wins over everything -------------
        # A lock is a deliberate hand edit (ComfyUI Set Voice, or JSON),
        # so no automatic rule — prior volume, --narrator-clip, gender sanity
        # check — may undo it. The only exception is a clip that no longer
        # exists: keeping it would just fail at s4.
        if own is not None and own.locked:
            if own.assigned_clip_id in clip_by_id:
                casting = own.model_copy(
                    deep=True,
                    update={
                        "character_name": character.name,
                        "voice_descriptor": _descriptor(own, character),
                    },
                )
                _emit(character, casting, None)
                continue
            note = (
                f"voice cast: locked clip {own.assigned_clip_id} is not in "
                "the voicebank; re-cast"
            )
            own = None
        elif own is not None and not _consistent(own, character):
            own = None

        # --- Narrator override: beats everything but a lock -----------------
        if character.name == "Narrator" and narrator_clip_override:
            clip = clip_by_id[narrator_clip_override]
            _emit(
                character,
                CharacterCasting(
                    character_name="Narrator",
                    target=VoiceTarget(
                        gender=clip.gender,
                        age_band=clip.age_band,
                        accent_keywords=[clip.accent],
                    ),
                    candidates_considered=1,
                    ranked=[{
                        "clip_id": clip.id,
                        "score": 1.0,
                        "reason": "Manual --narrator-clip override.",
                    }],
                    assigned_clip_id=clip.id,
                    voice_descriptor=descriptor_from_clip(clip),
                ),
                own,
                note,
            )
            continue

        # --- Reuse prior-volume assignment if available ---------------------
        # Beats this volume's own unlocked entry: that is what keeps a voice
        # continuous across the series.
        prior = (
            prior_index.lookup(character.name, character.aliases, character.gender)
            if prior_index
            else None
        )
        if prior is not None and _consistent(prior, character):
            _emit(
                character,
                CharacterCasting(
                    character_name=character.name,
                    target=prior.target,
                    candidates_considered=prior.candidates_considered,
                    ranked=prior.ranked,
                    assigned_clip_id=prior.assigned_clip_id,
                    voice_descriptor=_descriptor(prior, character),
                ),
                own,
                note,
            )
            continue

        # --- Reuse this volume's own (unlocked) assignment ------------------
        # Weakest layer: a character first cast on an earlier run of THIS
        # volume keeps its clip instead of being re-rolled by the LLM, so a
        # re-run is idempotent and already-rendered audio stays valid.
        if own is not None:
            casting = own.model_copy(
                deep=True,
                update={
                    "character_name": character.name,
                    "voice_descriptor": _descriptor(own, character),
                },
            )
            _emit(character, casting, None, note)
            continue

        # --- Skip characters CV doesn't cover (nonbinary / unknown) ---------
        if character.gender not in ("male", "female"):
            _emit(
                character,
                CharacterCasting(
                    character_name=character.name,
                    target=VoiceTarget(
                        gender="male",  # placeholder; not used
                        age_band="adult",
                    ),
                    candidates_considered=0,
                    ranked=[],
                    assigned_clip_id="",
                ),
                None,
                note,
            )
            continue

        # --- Explicit Narrator default (no override, no prior volume) -------
        # Skip the LLM target inference: the Narrator's "personality" comes
        # from a synthetic description and inferring against it tends to drift.
        # Use a fixed `female / adult` target with `england` as an accent
        # PREFERENCE (the soft filter puts those clips first; the hard filter
        # never looks at accent), fall back to relaxed filters if the
        # voicebank is sparse, and derive the descriptor from the chosen clip.
        if character.name == "Narrator":
            primary = VoiceTarget(
                gender=DEFAULT_NARRATOR_GENDER,
                age_band=DEFAULT_NARRATOR_AGE,
                accent_keywords=[DEFAULT_NARRATOR_ACCENT],
            )
            candidates = _hard_filter(voicebank, primary)
            relaxation = "female/adult, england first"
            # Fallback ladder: drop age → drop gender. (Accent needs no rung:
            # it is only an ordering preference, so it never empties the set.)
            if not candidates:
                relaxation = "relaxed age (female/any age)"
                candidates = [
                    c for c in voicebank.clips if c.gender == DEFAULT_NARRATOR_GENDER
                ]
            if not candidates:
                relaxation = "any clip"
                candidates = list(voicebank.clips)

            soft = _soft_filter_by_accent(
                candidates, primary, max_keep=max_candidates_per_char
            )
            if not soft:
                soft = candidates[:max_candidates_per_char]

            assigned_clip: VoiceClip | None = None
            ranked_choices: list[_RankedChoice] = []
            if len(soft) == 1:
                assigned_clip = soft[0]
                ranked_choices = [
                    _RankedChoice(
                        clip_id=assigned_clip.id,
                        score=1.0,
                        reason=f"Sole candidate ({relaxation}).",
                    )
                ]
            elif len(soft) > 1:
                ranked = rank_candidates(client, character, primary, soft, top_n=top_n)
                ranked_choices = _offered_choices(ranked, soft)
                if not ranked_choices:
                    fallback, fb_note = _fallback_choice(ranked, soft)
                    ranked_choices = [fallback]
                    note = "; ".join(filter(None, (note, fb_note)))
                assigned_clip = clip_by_id[ranked_choices[0].clip_id]

            descriptor = (
                descriptor_from_clip(assigned_clip)
                if assigned_clip
                else "adult, British, female, warm clear voice, measured and engaging storyteller"
            )
            _emit(
                character,
                CharacterCasting(
                    character_name="Narrator",
                    target=primary,
                    candidates_considered=len(soft),
                    ranked=ranked_choices,
                    assigned_clip_id=assigned_clip.id if assigned_clip else "",
                    voice_descriptor=descriptor,
                ),
                None,
                note,
            )
            continue

        prof = profiles_by_name.get(character.name)
        target = infer_target(
            client,
            character,
            voice_descriptor=(prof.voice_descriptor if prof else ""),
            profile_accent=(prof.accent if prof else "any"),
        )
        hard = _hard_filter(voicebank, target)
        soft = _soft_filter_by_accent(hard, target, max_keep=max_candidates_per_char)
        ranked = rank_candidates(client, character, target, soft, top_n=top_n)
        ranked_choices = _offered_choices(ranked, soft)
        if soft and not ranked_choices:
            fallback, fb_note = _fallback_choice(ranked, soft)
            ranked_choices = [fallback]
            note = "; ".join(filter(None, (note, fb_note)))
        assigned = ranked_choices[0].clip_id if ranked_choices else ""

        # For the Narrator (auto-cast path), lock in a clip-derived descriptor
        # so s3 doesn't compose a free-text one that contradicts the chosen
        # reference voice. Other characters keep an empty descriptor here —
        # s3's LLM call writes them with full personality detail.
        narrator_descriptor = ""
        if character.name == "Narrator" and assigned in clip_by_id:
            narrator_descriptor = descriptor_from_clip(clip_by_id[assigned])

        _emit(
            character,
            CharacterCasting(
                character_name=character.name,
                target=target,
                candidates_considered=len(soft),
                ranked=ranked_choices,
                assigned_clip_id=assigned,
                voice_descriptor=narrator_descriptor,
            ),
            None,
            note,
        )

    return BookCasting(book_id=book_id, castings=castings)
