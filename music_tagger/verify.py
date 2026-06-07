"""Claude verification for low/medium-confidence albums.

Claude's job is narrow: pick which beets/MusicBrainz candidate (if any) is the
correct release for this album. Proposed tag *values* come from the chosen
candidate's MusicBrainz data — Claude selects, it does not invent tags. This
keeps the task bounded (cheap, Haiku-suitable) and the output grounded.
"""
import json
import re
import logging
from pathlib import Path

log = logging.getLogger(__name__)

# Bounded selection/verification task → Haiku is the right tier and ~3x cheaper
# than Sonnet. Override via the --model CLI flag if you want Sonnet/Opus.
DEFAULT_MODEL = "claude-haiku-4-5"

# Per-call token estimate for the cost projection (selection prompt is small).
EST_INPUT_TOKENS = 700
EST_OUTPUT_TOKENS = 150

_PRICING = {  # USD per token (input, output)
    "claude-haiku-4-5": (1 / 1_000_000, 5 / 1_000_000),
    "claude-sonnet-4-6": (3 / 1_000_000, 15 / 1_000_000),
    "claude-opus-4-8": (5 / 1_000_000, 25 / 1_000_000),
}


def estimate_cost(num_albums: int, model: str = DEFAULT_MODEL) -> float:
    cin, cout = _PRICING.get(model, _PRICING["claude-haiku-4-5"])
    return round(num_albums * (EST_INPUT_TOKENS * cin + EST_OUTPUT_TOKENS * cout), 4)


def _parse_json(raw: str) -> dict:
    cleaned = re.sub(r"^```(?:json)?\s*", "", raw.strip())
    cleaned = re.sub(r"\s*```$", "", cleaned).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", cleaned, re.DOTALL)
        if m:
            return json.loads(m.group(0))
        raise


def _build_prompt(folder: str, current: dict, candidates: list[dict]) -> str:
    # Compact current tracklist
    cur_lines = []
    for path, tags in sorted(current.items()):
        cur_lines.append(
            f"  {Path(path).name}: "
            f"track={tags.get('tracknumber', '?')} "
            f"title={tags.get('title', '?')!r} artist={tags.get('artist', '?')!r}"
        )
    cur_block = "\n".join(cur_lines) if cur_lines else "  (no readable tags)"

    cand_lines = []
    for i, c in enumerate(candidates):
        cand_lines.append(
            f"  [{i}] album={c.get('album')!r} artist={c.get('albumartist')!r} "
            f"year={c.get('year')} country={c.get('country')} media={c.get('media')} "
            f"matched={c.get('num_matched')} extra_items={c.get('extra_items')} "
            f"extra_tracks={c.get('extra_tracks')} distance={c.get('distance')} "
            f"mbid={c.get('album_id')}"
        )
    cand_block = "\n".join(cand_lines)

    return f"""You are a music-metadata expert resolving an ambiguous album match.

FOLDER: {folder}

CURRENT FILES AND TAGS:
{cur_block}

CANDIDATE RELEASES (from MusicBrainz; distance: lower = closer match):
{cand_block}

Pick the single candidate that best matches this folder of files, or reject all
if none is a credible match. Weigh track count alignment (matched vs extra),
distance, and consistency with the current artist/title tags. Be skeptical of
candidates with many extra/unmatched tracks.

Respond ONLY with JSON, no prose:
{{
  "chosen_index": <integer index of best candidate, or null to reject all>,
  "confidence": <integer 0-100>,
  "reasoning": "<one sentence>"
}}"""


def verify_album(folder: str, current: dict, candidates: list[dict], client, model: str = DEFAULT_MODEL) -> dict:
    """Ask Claude to select among candidates. Returns {chosen_index, confidence, reasoning}."""
    prompt = _build_prompt(folder, current, candidates)
    resp = client.messages.create(
        model=model,
        max_tokens=300,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = next((b.text for b in resp.content if b.type == "text"), "").strip()
    try:
        verdict = _parse_json(raw)
    except Exception as e:  # noqa: BLE001
        log.error(f"Could not parse Claude response for {folder}: {e}\nRaw: {raw[:200]}")
        return {"chosen_index": None, "confidence": 0, "reasoning": f"parse error: {e}"}

    idx = verdict.get("chosen_index")
    if idx is not None and not (isinstance(idx, int) and 0 <= idx < len(candidates)):
        idx = None
    return {
        "chosen_index": idx,
        "confidence": int(verdict.get("confidence", 0) or 0),
        "reasoning": str(verdict.get("reasoning", "")),
    }
