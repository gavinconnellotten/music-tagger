"""Ad-hoc: find why a player splits an album — dump every grouping-relevant tag
(album, albumartist(+sort), the MusicBrainz IDs, compilation flag, disc totals)
and report which ones are inconsistent across the album's tracks."""
import sys
from collections import Counter
from pathlib import Path
from mutagen import File as MFile

root = Path(r"Z:\Music")
needle = sys.argv[1].lower()
folders = sorted([p for p in root.iterdir() if p.is_dir() and needle in p.name.lower()])
if not folders:
    print("no match for", needle); sys.exit(0)

# Substrings of tag keys that influence album/artist grouping in players.
INTEREST = ("album", "artist", "musicbrainz", "compilation", "disc", "grouping", "tcmp")


def norm_keys(af):
    """Return {lowercased-key: 'v1|v2'} for the tags we care about, ID3/Vorbis/MP4 alike."""
    out = {}
    if af is None or af.tags is None:
        return out
    for k, v in af.tags.items():
        kl = k.lower()
        # ID3 frames look like 'TXXX:MusicBrainz Album Id' / 'TPE2' — keep the readable part.
        key = kl.split(":", 1)[1] if kl.startswith("txxx:") else kl
        if any(s in key for s in INTEREST):
            if hasattr(v, "text"):
                v = v.text
            vals = v if isinstance(v, list) else [v]
            out[key] = "|".join(str(x) for x in vals)
    return out


for folder in folders:
    files = sorted([p for p in folder.rglob("*") if p.suffix.lower() in (".flac", ".mp3")])
    print("=" * 90)
    print(f"{folder.name}   ({len(files)} files)")
    print("-" * 90)
    per_key = {}
    for p in files:
        d = norm_keys(MFile(str(p)))
        for k, v in d.items():
            per_key.setdefault(k, Counter())[v] += 1
        # also record absence
    n = len(files)
    for k in sorted(per_key):
        c = per_key[k]
        present = sum(c.values())
        missing = n - present
        distinct = len(c)
        if distinct > 1 or missing:
            parts = []
            for val, cnt in c.most_common(6):
                disp = val if len(val) <= 50 else val[:47] + "..."
                parts.append(f"{disp!r}×{cnt}")
            if missing:
                parts.append(f"<MISSING>×{missing}")
            print(f"  ⚠ {k}: {distinct} distinct -> " + ", ".join(parts))
        else:
            (val,) = c
            disp = val if len(val) <= 60 else val[:57] + "..."
            print(f"    {k}: {disp!r}")
