"""Ad-hoc: summarize freshly-matched (non-cached) albums from the latest report."""
import json
from pathlib import Path

data = json.load(open("music_tagger_report.json", encoding="utf-8"))
new = [r for r in data if not r["from_cache"]["lookup"]]
print(f"TOTAL albums in report: {len(data)}")
print(f"NEW (freshly matched this run): {len(new)}")
print(f"NEW with proposed file changes: {sum(1 for r in new if r['n_changed_files'] > 0)}")
print("=" * 78)
for r in new:
    folder = Path(r["folder"]).name
    act = r["action"]
    conf = r.get("confidence")
    chosen = r.get("chosen") or {}
    if chosen:
        tgt = f"{chosen.get('albumartist','?')} - {chosen.get('album','?')} ({chosen.get('year','?')})"
    else:
        tgt = "(no candidate chosen)"
    tag = f"{act}/{conf}" if conf is not None else act
    print(f"[{tag}]  {folder}")
    print(f"      -> {tgt}   changed={r['n_changed_files']}/{r['n_files']} files")
