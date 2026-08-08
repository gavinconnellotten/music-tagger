"""Make every track in an album folder share ONE album name + ONE album artist, and
clear a 'compilation=1' flag on single-artist albums — the residual splits that make
Plex scatter an album or file it under Various Artists.

Per album (matcher.cluster_albums; disc folders folded in):
  - album name      -> the majority value (tie broken toward the folder name)
  - album artist    -> the majority value; missing ones filled, IF the album is
                       single-artist or has a clear (>50%) majority albumartist
                       (genuine Various-Artists albums are left alone)
  - compilation="1" -> cleared when the album is single-artist (else left)

Reversible: snapshots each changed file's original tags to an undo log (JSON);
`--undo <log>` restores them via the tagger's restore_tags.

Usage:
  python tools/normalize_albums.py                 # dry-run, whole library
  python tools/normalize_albums.py --apply         # write + journal undo log
  python tools/normalize_albums.py --undo music_tagger_normalize_undo.json
"""
import re
import sys
import json
from collections import Counter
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from music_tagger.tags import read_existing_tags, write_tags, restore_tags, COMPILATION  # noqa: E402
from music_tagger.matcher import cluster_albums  # noqa: E402

ROOT = Path(r"Z:\Music")
UNDO_LOG = Path("music_tagger_normalize_undo.json")


def _norm(s: str) -> str:
    return "".join(ch.lower() for ch in (s or "") if ch.isalnum())


def pick_majority(counter: Counter, folder_name: str) -> str:
    """Most common value; on a tie prefer the one closest to the folder name. Used
    for album ARTIST (variants are case/quote only, so count is the right signal)."""
    if not counter:
        return ""
    top = counter.most_common()
    best_n = top[0][1]
    tied = [v for v, n in top if n == best_n]
    if len(tied) == 1:
        return tied[0]
    fn = _norm(folder_name)
    tied.sort(key=lambda v: (fn != _norm(v), _norm(v) not in fn and fn not in _norm(v)))
    return tied[0]


# Disc / edition / remaster cruft that a stray track's album name often carries
# ("… CD 2", "(1994. Digital Remaster EMI)", "OKNOTOK 1997 2017"). Names with it
# must NOT win even if they're the majority — the clean base name is canonical.
_CRUFT = re.compile(
    r"\b(cd ?\d|disc|disk|bonus|remaster(ed)?|reissue|re-issue|expanded|deluxe|"
    r"edition|ltd|digital|mono|stereo|volume|vol)\b", re.I)


def _clean_score(name: str) -> int:
    """Lower = cleaner. Penalize bracketed cruft, disc/edition markers, and years.
    Length is NOT included here — it must not override the track-count signal."""
    s = len(re.findall(r"[()\[\]]", name)) * 2
    if _CRUFT.search(name):
        s += 5
    if re.search(r"(19|20)\d\d", name):
        s += 3
    return s


def pick_album(counter: Counter, folder_name: str) -> str:
    """Cleanest album name first; then the most common; then folder-name match; then
    shorter. Avoids promoting a disc-suffixed straggler to canonical while still
    letting the majority win between two equally-clean names."""
    if not counter:
        return ""
    fn = _norm(folder_name)
    return min(counter, key=lambda v: (
        _clean_score(v), -counter[v],
        0 if (_norm(v) in fn or fn in _norm(v)) else 1, len(v)))


def plan_album(folder: Path, files: list[Path], force_album: str = "") -> list[dict]:
    """Return [{path, changes:{field:newval}, current:{...}}] for files needing edits.
    force_album overrides the inferred album name (for best-of folders whose canonical
    name isn't in any track tag)."""
    currents = {str(p): read_existing_tags(str(p)) for p in files}
    n = len(files)
    albums = Counter(c["album"] for c in currents.values() if c.get("album"))
    aas = Counter(c["albumartist"] for c in currents.values() if c.get("albumartist"))
    ars = Counter(c["artist"] for c in currents.values() if c.get("artist"))

    # Only unify the album name when one name (or its clean base) already dominates
    # the folder — i.e. the others are stragglers. If NO name covers ≥50% of tracks,
    # this is a best-of/compilation where tracks legitimately carry different original
    # album tags (e.g. a "Best Of" with per-track source albums); leave those alone.
    if force_album:
        target_album = force_album
    elif len(albums) > 1:
        target_album = (pick_album(albums, folder.name)
                        if albums.most_common(1)[0][1] >= n / 2 else "")
    else:
        target_album = next(iter(albums), "") if albums else ""
    # VA only if the DOMINANT album artist is 'Various Artists' — a single stray VA
    # track in an otherwise single-artist album must NOT block the fix (that stray is
    # exactly what we want to correct).
    is_va_label = bool(aas) and "various" in aas.most_common(1)[0][0].lower()
    single_artist = len(ars) <= 1 and not is_va_label

    # Decide an album-artist target only when we're confident (single-artist, or a
    # clear majority). Never touch a genuine Various-Artists album.
    target_aa = ""
    if not is_va_label:
        if aas:
            top_aa, top_n = aas.most_common(1)[0]
            if single_artist or top_n > n / 2:
                target_aa = pick_majority(aas, folder.name)
        elif single_artist and ars:
            target_aa = ars.most_common(1)[0][0]  # no albumartist at all -> use the artist

    out = []
    for p in files:
        cur = currents[str(p)]
        changes = {}
        if target_album and cur.get("album") != target_album:
            changes["album"] = target_album
        if target_aa and cur.get("albumartist") != target_aa:
            changes["albumartist"] = target_aa
        if single_artist and str(cur.get(COMPILATION) or "") == "1":
            changes[COMPILATION] = ""  # clear -> Plex stops filing under Various Artists
        if changes:
            out.append({"path": str(p), "changes": changes, "current": cur})
    return out


def do_undo(log_path: Path) -> None:
    entries = json.loads(log_path.read_text(encoding="utf-8"))
    # Keep the EARLIEST original per path so a file edited across multiple runs is
    # restored to its pre-everything state, not an intermediate one.
    seen, deduped = set(), []
    for e in entries:
        if e["path"] in seen:
            continue
        seen.add(e["path"])
        deduped.append(e)
    print(f"Restoring {len(deduped)} file(s) from {log_path}...")
    ok = sum(1 for e in deduped if restore_tags(e["path"], e["original"]))
    print(f"Restored {ok}/{len(deduped)} file(s).")


def main() -> None:
    args = sys.argv[1:]
    if "--undo" in args:
        do_undo(Path(args[args.index("--undo") + 1]))
        return
    apply = "--apply" in args
    set_album = ""
    if "--set-album" in args:
        i = args.index("--set-album")
        set_album = args[i + 1]
        args = args[:i] + args[i + 2:]
    needles = [a for a in args if not a.startswith("--")]  # optional folder-substring scope
    if set_album and not needles:
        print("--set-album requires a folder-substring scope (safety)"); sys.exit(1)

    by_album = cluster_albums(str(ROOT))
    if needles:
        nl = needles[0].lower()
        by_album = {k: v for k, v in by_album.items() if nl in str(k).lower()}
    mode = "APPLY" if apply else "DRY-RUN"
    print(f"[{mode}] normalizing {len(by_album)} album(s)"
          + (f" matching {needles[0]!r}" if needles else "") + "...")
    undo, n_albums, n_files = [], 0, 0
    for folder in sorted(by_album):
        edits = plan_album(folder, by_album[folder], force_album=set_album)
        if not edits:
            continue
        n_albums += 1
        fields = sorted({k for e in edits for k in e["changes"]})
        print(f"  {folder.name}: {len(edits)} file(s)  [{', '.join(fields)}]")
        for e in edits:
            tgt = {k: v for k, v in e["changes"].items() if k != COMPILATION}
            comp = " +clear-compilation" if COMPILATION in e["changes"] else ""
            sample = ", ".join(f"{k}={v!r}" for k, v in tgt.items())
            print(f"      {Path(e['path']).name}: {sample}{comp}")
        if apply:
            for e in edits:
                original = read_existing_tags(e["path"])
                if write_tags(e["path"], e["changes"]):
                    undo.append({"path": e["path"], "original": original})
                    n_files += 1
                else:
                    print(f"      ! failed: {e['path']}")
    if apply:
        # Append to (not clobber) any existing undo log so prior runs stay reversible.
        prior = json.loads(UNDO_LOG.read_text(encoding="utf-8")) if UNDO_LOG.exists() else []
        UNDO_LOG.write_text(json.dumps(prior + undo, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"[APPLY] wrote {n_files} file(s) in {n_albums} album(s); "
              f"undo log -> {UNDO_LOG.resolve()} ({len(prior) + len(undo)} total entries)")
    else:
        print(f"[DRY-RUN] {n_albums} album(s) would change — re-run with --apply")


if __name__ == "__main__":
    main()
