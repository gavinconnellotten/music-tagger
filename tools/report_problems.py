"""Render a problems-only report from music_tagger_report.json.

Surfaces only albums that need a human: files the script couldn't handle, and
low-confidence / contentious tagging. Each album is assigned to exactly one
bucket by priority.

Usage: python tools/report_problems.py [low_conf_threshold]
"""
import sys
import json
from pathlib import Path
from datetime import datetime

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from music_tagger.report import _fmt_tags  # noqa: E402

LOW_CONF = int(sys.argv[1]) if len(sys.argv) > 1 else 90
SRC = "music_tagger_report.json"
OUT = "music_tagger_problems.html"


def _unmatched(r):
    return sum(1 for f in r["files"] if not f["proposed"])


def _name(r):
    return r["folder"].split("\\")[-1].split("/")[-1]


def _changed_table(r):
    rows = []
    for f in r["files"]:
        if not f["changed"]:
            continue
        rows.append(
            f"<tr><td class='fn'>{f['name']}</td><td>{', '.join(f['changed'])}</td>"
            f"<td class='tc'>{_fmt_tags(f['current'])}</td>"
            f"<td class='tc'>{_fmt_tags(f['proposed'])}</td></tr>"
        )
    if not rows:
        return "<p style='color:#7f8c8d'>No field changes proposed.</p>"
    return ("<table class='ft'><thead><tr><th style='width:22%'>File</th>"
            "<th style='width:13%'>Changed</th><th style='width:32%'>Current</th>"
            "<th style='width:33%'>Proposed</th></tr></thead><tbody>"
            + "".join(rows) + "</tbody></table>")


def _full_block(r, badge=""):
    c = r.get("chosen")
    chosen = (f"{c['album']} — {c['albumartist']} ({c.get('year') or '?'}) · dist {c.get('distance')}"
              if c else "<em>none</em>")
    conf = f"{r['confidence']}%" if r.get("confidence") is not None else "—"
    um = _unmatched(r)
    um_str = f" · <b style='color:#c0392b'>{um} file(s) unmatched</b>" if um else ""
    return (f"<div class='album'><h3>{_name(r)}</h3>"
            f"<p>{badge} conf {conf} · {r['n_changed_files']}/{r['n_files']} changed{um_str}</p>"
            f"<p><b>Chosen:</b> {chosen}</p><p class='rs'>{r.get('reasoning','')}</p>"
            f"{_changed_table(r)}</div>")


def _compact_rows(rows_data):
    return "".join(
        f"<tr><td class='fn'>{_name(r)}</td><td style='text-align:center'>{r['n_files']}</td>"
        f"<td>{extra}</td></tr>" for r, extra in rows_data
    )


def main():
    d = json.load(open(SRC, encoding="utf-8"))
    total = len(d)

    # Priority assignment — each album in exactly one bucket.
    errors, nomatch, rejected, partial, lowconf = [], [], [], [], []
    for r in d:
        a = r["action"]
        if a == "error":
            errors.append(r)
        elif a == "unresolved":
            nomatch.append(r)
        elif a == "claude_rejected":
            rejected.append(r)
        elif r.get("chosen") and _unmatched(r) > 0:
            partial.append(r)
        elif a == "claude_selected" and ((r.get("confidence") or 0) < LOW_CONF or r["recommendation"] == "low"):
            lowconf.append(r)

    n_problems = len(errors) + len(nomatch) + len(rejected) + len(partial) + len(lowconf)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    sections = []
    sections.append(f"""<div class="summary"><h2 style="border:none;margin-top:0">Problems</h2>
<p>{n_problems} of {total} albums need review (low-confidence threshold &lt; {LOW_CONF}%).</p>
<table>
<tr><th>Bucket</th><th>Albums</th></tr>
<tr style="color:#c0392b"><td>Could not read files</td><td>{len(errors)}</td></tr>
<tr style="color:#95a5a6"><td>No MusicBrainz match</td><td>{len(nomatch)}</td></tr>
<tr style="color:#e67e22"><td>Claude rejected all candidates</td><td>{len(rejected)}</td></tr>
<tr style="color:#c0392b"><td>Partial match (some files unmatched)</td><td>{len(partial)}</td></tr>
<tr style="color:#d35400"><td>Low-confidence selection (&lt;{LOW_CONF}%)</td><td>{len(lowconf)}</td></tr>
</table></div>""")

    if errors:
        sections.append("<h2>① Could not read files</h2>"
                        "<table class='ft'><thead><tr><th style='width:40%'>Album</th>"
                        "<th style='width:8%'>Files</th><th>Reason</th></tr></thead><tbody>"
                        + _compact_rows([(r, r.get("reasoning", "")[:200]) for r in errors])
                        + "</tbody></table>")

    if partial:
        sections.append(f"<h2>② Partial match — some files unmatched ({len(partial)})</h2>"
                        "<p>Reissues / bonus-track editions / multi-disc folders. Unmatched files get "
                        "no proposal; review renumbering on the matched tracks.</p>"
                        + "".join(_full_block(r, "<span style='color:#c0392b;font-weight:bold'>PARTIAL</span> ·")
                                  for r in sorted(partial, key=lambda x: -_unmatched(x))))

    if lowconf:
        sections.append(f"<h2>③ Low-confidence selections (&lt;{LOW_CONF}%) ({len(lowconf)})</h2>"
                        + "".join(_full_block(r, "<span style='color:#d35400;font-weight:bold'>LOW CONF</span> ·")
                                  for r in sorted(lowconf, key=lambda x: x.get("confidence") or 0)))

    if rejected:
        sections.append(f"<h2>④ Claude rejected all candidates ({len(rejected)})</h2>"
                        + "".join(_full_block(r, "<span style='color:#e67e22;font-weight:bold'>REJECTED</span> ·")
                                  for r in rejected))

    if nomatch:
        sections.append(f"<h2>⑤ No MusicBrainz match ({len(nomatch)})</h2>"
                        "<p>Candidates for a fingerprint fallback (beets chroma plugin).</p>"
                        "<table class='ft'><thead><tr><th style='width:46%'>Album</th>"
                        "<th style='width:8%'>Files</th><th>Current artist / album</th></tr></thead><tbody>"
                        + _compact_rows([(r, f"{r.get('cur_artist','')} / {r.get('cur_album','')}")
                                         for r in nomatch])
                        + "</tbody></table>")

    html = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<title>MusicTagger Problems — {now}</title>
<style>
 body{{font-family:Arial,sans-serif;margin:20px;color:#333;line-height:1.45}}
 h1{{color:#1a1a2e}} h2{{color:#16213e;border-bottom:2px solid #dee2e6;padding-bottom:6px;margin-top:28px}}
 h3{{margin:6px 0 2px;font-family:Consolas,monospace;font-size:13px;color:#16213e}}
 .summary{{background:#f8f9fa;padding:14px 18px;border-radius:6px;display:inline-block}}
 .summary table{{border-collapse:collapse;min-width:340px}}
 .summary td,.summary th{{padding:5px 12px;border:1px solid #ddd}}
 .album{{border:1px solid #e1e4e8;border-radius:6px;padding:10px 14px;margin:12px 0;background:#fff}}
 .ft{{border-collapse:collapse;width:100%;table-layout:fixed;margin-top:6px}}
 .ft th,.ft td{{border:1px solid #ccc;padding:6px 8px;vertical-align:top;word-wrap:break-word}}
 .ft th{{background:#343a40;color:#fff;text-align:left}}
 .ft tr:nth-child(even){{background:#f8f9fa}}
 .fn{{font-family:Consolas,monospace;font-size:11px;word-break:break-all}}
 .tc{{font-family:Consolas,monospace;font-size:11px}} .rs{{font-style:italic;color:#555}}
</style></head><body>
<h1>MusicTagger — Problems Only</h1><p>Generated: {now}</p>
{''.join(sections)}
</body></html>"""

    Path(OUT).write_text(html, encoding="utf-8")
    print(f"{n_problems}/{total} problematic albums -> {Path(OUT).resolve()}")
    print(f"  errors={len(errors)} nomatch={len(nomatch)} rejected={len(rejected)} "
          f"partial={len(partial)} lowconf={len(lowconf)}")


if __name__ == "__main__":
    main()
