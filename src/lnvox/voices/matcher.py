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
    narrator_clip_override: str | None = None,
    on_character_done=None,
) -> BookCasting:
    """Cast each character to a voicebank clip.

    Args:
        prior_index: If supplied (built from ALL prior volumes in the same
            series), characters whose name or aliases match a prior
            assignment keep that prior clip without an LLM call. Narrator
            also inherits unless `narrator_clip_override` is set.
        narrator_clip_override: Explicit clip id for the Narrator. Overrides
            both the LLM auto-cast AND any prior-volume Narrator assignment.
    """

    profiles_by_name: dict = {}
    if profiles is not None:
        profiles_by_name = {p.name: p for p in profiles.profiles}

    # Voicebank lookup by clip id for resolving overrides + reuses.
    clip_by_id = {c.id: c for c in voicebank.clips}

    # Synthesize a Narrator Character if it's not in the s1 cast already.
    # In the v2 order (voice cast before s3), there's no profiles file yet,
    # so we MUST look at every other source before falling back to defaults:
    #   1. --narrator-clip override   → derive from the chosen clip
    #   2. Prior volume's casting     → reuse the prior Narrator's demographics
    #   3. Existing profiles file     → legacy path (s3 ran before voice cast)
    #   4. Default ("male" / "adult") → first volume, no override
    cast = list(cast)
    if not any(c.name == "Narrator" for c in cast):
        n_gender = DEFAULT_NARRATOR_GENDER
        n_age = DEFAULT_NARRATOR_AGE
        n_desc = "Neutral audiobook narrator for a modern action/fantasy young-adult novel."
        prior_n = prior_index.lookup("Narrator") if prior_index else None
        if narrator_clip_override and narrator_clip_override in clip_by_id:
            override_clip = clip_by_id[narrator_clip_override]
            n_gender = override_clip.gender
            n_age = override_clip.age_band
        elif prior_n is not None:
            n_gender = prior_n.target.gender or n_gender
            n_age = prior_n.target.age_band or n_age
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

    castings: list[CharacterCasting] = []
    for character in cast:
        # --- Narrator override takes priority over everything else ----------
        if character.name == "Narrator" and narrator_clip_override:
            clip = clip_by_id.get(narrator_clip_override)
            if not clip:
                raise ValueError(
                    f"--narrator-clip {narrator_clip_override!r} not in voicebank."
                )
            castings.append(
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
                )
            )
            if on_character_done:
                on_character_done(character, castings[-1])
            continue

        # --- Reuse prior-volume assignment if available ---------------------
        prior = (
            prior_index.lookup(character.name, character.aliases, character.gender)
            if prior_index
            else None
        )
        if prior and prior.assigned_clip_id in clip_by_id:
            clip = clip_by_id[prior.assigned_clip_id]
            # A clip whose gender contradicts BOTH the current character and
            # the prior target is an internally-inconsistent assignment (e.g.
            # a hand-edit pointed at the wrong clip) — recast rather than
            # propagate it through the whole series.
            known = {character.gender, prior.target.gender} & {"male", "female"}
            if known and clip.gender not in known:
                prior = None
        else:
            prior = None
        if prior:
            clip = clip_by_id[prior.assigned_clip_id]
            # Prefer the prior's voice_descriptor if it was set there,
            # otherwise (legacy assignments) derive one from the clip.
            descriptor = prior.voice_descriptor or (
                descriptor_from_clip(clip)
                if character.name == "Narrator"
                else ""
            )
            castings.append(
                CharacterCasting(
                    character_name=character.name,
                    target=prior.target,
                    candidates_considered=prior.candidates_considered,
                    ranked=prior.ranked,
                    assigned_clip_id=prior.assigned_clip_id,
                    voice_descriptor=descriptor,
                )
            )
            if on_character_done:
                on_character_done(character, castings[-1])
            continue

        # --- Skip characters CV doesn't cover (nonbinary / unknown) ---------
        if character.gender not in ("male", "female"):
            castings.append(
                CharacterCasting(
                    character_name=character.name,
                    target=VoiceTarget(
                        gender="male",  # placeholder; not used
                        age_band="adult",
                    ),
                    candidates_considered=0,
                    ranked=[],
                    assigned_clip_id="",
                )
            )
            if on_character_done:
                on_character_done(character, castings[-1])
            continue

        # --- Explicit Narrator default (no override, no prior volume) -------
        # Skip the LLM target inference: the Narrator's "personality" comes
        # from a synthetic description and inferring against it tends to drift.
        # Use a fixed `female / adult / england` target, fall back to relaxed
        # filters if the voicebank is sparse, and derive the descriptor from
        # the chosen clip.
        if character.name == "Narrator":
            primary = VoiceTarget(
                gender=DEFAULT_NARRATOR_GENDER,
                age_band=DEFAULT_NARRATOR_AGE,
                accent_keywords=[DEFAULT_NARRATOR_ACCENT],
            )
            candidates = _hard_filter(voicebank, primary)
            relaxation = "exact (female/adult/england)"
            # Fallback ladder: drop accent → drop age → drop gender.
            if not candidates:
                relaxation = "relaxed accent (female/adult/any)"
                candidates = _hard_filter(
                    voicebank,
                    VoiceTarget(
                        gender=DEFAULT_NARRATOR_GENDER,
                        age_band=DEFAULT_NARRATOR_AGE,
                    ),
                )
            if not candidates:
                relaxation = "relaxed age (female/any-age/any)"
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
                ranked_choices = ranked.ranked_choices
                if ranked_choices:
                    top_id = ranked_choices[0].clip_id
                    assigned_clip = clip_by_id.get(top_id) or soft[0]
                else:
                    assigned_clip = soft[0]

            descriptor = (
                descriptor_from_clip(assigned_clip)
                if assigned_clip
                else "adult, British, female, warm clear voice, measured and engaging storyteller"
            )
            casting = CharacterCasting(
                character_name="Narrator",
                target=primary,
                candidates_considered=len(soft),
                ranked=ranked_choices,
                assigned_clip_id=assigned_clip.id if assigned_clip else "",
                voice_descriptor=descriptor,
            )
            castings.append(casting)
            if on_character_done:
                on_character_done(character, casting)
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
        assigned = ranked.ranked_choices[0].clip_id if ranked.ranked_choices else ""

        # For the Narrator (auto-cast path), lock in a clip-derived descriptor
        # so s3 doesn't compose a free-text one that contradicts the chosen
        # reference voice. Other characters keep an empty descriptor here —
        # s3's LLM call writes them with full personality detail.
        narrator_descriptor = ""
        if character.name == "Narrator" and assigned in clip_by_id:
            narrator_descriptor = descriptor_from_clip(clip_by_id[assigned])

        casting = CharacterCasting(
            character_name=character.name,
            target=target,
            candidates_considered=len(soft),
            ranked=ranked.ranked_choices,
            assigned_clip_id=assigned,
            voice_descriptor=narrator_descriptor,
        )
        castings.append(casting)
        if on_character_done:
            on_character_done(character, casting)

    return BookCasting(book_id=book_id, castings=castings)
