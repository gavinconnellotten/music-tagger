"""Album-grouped dry-run/live reports (JSON + HTML)."""
import json
import logging
from pathlib import Path
from datetime import datetime

log = logging.getLogger(__name__)

_ACTION_LABEL = {
    "auto": "Auto (strong match)",
    "claude_selected": "Claude selected",
    "claude_rejected": "Claude rejected all",
    "unresolved": "No match",
    "error": "Error",
}
_ACTION_COLOR = {
    "auto": "#27ae60",
    "claude_selected": "#2980b9",
    "claude_rejected": "#e67e22",
    "unresolved": "#95a5a6",
    "error": "#c0392b",
}


def save_reports(results: list[dict], est_cost: float, model: str, dry_run: bool,
                 json_path="music_tagger_report.json", html_path="music_tagger_report.html") -> None:
    Path(json_path).write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
    Path(html_path).write_text(_html(results, est_cost, model, dry_run), encoding="utf-8")
    log.info(f"JSON report: {Path(json_path).resolve()}")
    log.info(f"HTML report: {Path(html_path).resolve()}")


def _fmt_tags(tags: dict) -> str:
    keys = ["title", "artist", "album", "albumartist", "tracknumber", "discnumber", "date", "genre"]
    parts = [f"<b>{k}:</b> {tags[k]}" for k in keys if tags.get(k)]
    return "<br>".join(parts) if parts else "<em>(none)</em>"


def _html(results: list[dict], est_cost: float, model: str, dry_run: bool) -> str:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    n_albums = len(results)
    n_files = sum(r["n_files"] for r in results)
    n_changed_files = sum(r["n_changed_files"] for r in results)

    action_counts: dict[str, int] = {}
    for r in results:
        action_counts[r["action"]] = action_counts.get(r["action"], 0) + 1
    claude_albums = sum(1 for r in results if r["action"] in ("claude_selected", "claude_rejected"))

    mode = "DRY RUN — no files modified" if dry_run else "LIVE RUN"
    mode_color = "#d35400" if dry_run else "#27ae60"

    summary_rows = "".join(
        f"<tr style='color:{_ACTION_COLOR.get(a, '#333')}'>"
        f"<td>{_ACTION_LABEL.get(a, a)}</td><td>{action_counts.get(a, 0)}</td></tr>"
        for a in ["auto", "claude_selected", "claude_rejected", "unresolved", "error"]
        if action_counts.get(a)
    )

    album_sections = []
    for r in sorted(results, key=lambda x: x["folder"]):
        chosen = r.get("chosen")
        chosen_str = (
            f"{chosen['album']} — {chosen['albumartist']} ({chosen.get('year') or '?'}) "
            f"· dist {chosen.get('distance')} · {chosen.get('album_id') or ''}"
            if chosen else "<em>none</em>"
        )
        conf = f"{r['confidence']}%" if r.get("confidence") is not None else "—"
        cache_bits = []
        if r.get("from_cache", {}).get("lookup"):
            cache_bits.append("lookup")
        if r.get("from_cache", {}).get("claude"):
            cache_bits.append("claude")
        cache_str = (" · cache: " + "+".join(cache_bits)) if cache_bits else ""

        changed_files = [f for f in r["files"] if f["changed"]]
        if changed_files:
            file_rows = "".join(
                f"<tr><td class='fn'>{f['name']}</td>"
                f"<td>{', '.join(f['changed'])}</td>"
                f"<td class='tc'>{_fmt_tags(f['current'])}</td>"
                f"<td class='tc'>{_fmt_tags(f['proposed'])}</td></tr>"
                for f in changed_files
            )
            file_table = (
                "<table class='ft'><thead><tr>"
                "<th style='width:22%'>File</th><th style='width:14%'>Changed</th>"
                "<th style='width:32%'>Current</th><th style='width:32%'>Proposed</th>"
                "</tr></thead><tbody>" + file_rows + "</tbody></table>"
            )
        else:
            file_table = "<p style='color:#7f8c8d'>No field changes proposed.</p>"

        color = _ACTION_COLOR.get(r["action"], "#333")
        album_sections.append(
            f"<div class='album'>"
            f"<h3>{r['folder']}</h3>"
            f"<p><span style='color:{color};font-weight:bold'>{_ACTION_LABEL.get(r['action'], r['action'])}</span>"
            f" · {r['n_changed_files']}/{r['n_files']} files changed · conf {conf}{cache_str}</p>"
            f"<p><b>Chosen release:</b> {chosen_str}</p>"
            f"<p class='rs'>{r.get('reasoning', '')}</p>"
            f"{file_table}</div>"
        )

    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<title>MusicTagger Report — {now}</title>
<style>
 body{{font-family:Arial,sans-serif;margin:20px;color:#333;line-height:1.45}}
 h1{{color:#1a1a2e}} h2{{color:#16213e;border-bottom:2px solid #dee2e6;padding-bottom:6px;margin-top:28px}}
 h3{{margin:6px 0 2px;font-family:Consolas,monospace;font-size:13px;color:#16213e}}
 .summary{{background:#f8f9fa;padding:14px 18px;border-radius:6px;display:inline-block}}
 .summary table{{border-collapse:collapse;min-width:340px}}
 .summary td,.summary th{{padding:5px 12px;border:1px solid #ddd}}
 .album{{border:1px solid #e1e4e8;border-radius:6px;padding:10px 14px;margin:14px 0;background:#fff}}
 .ft{{border-collapse:collapse;width:100%;table-layout:fixed;margin-top:6px}}
 .ft th,.ft td{{border:1px solid #ccc;padding:6px 8px;vertical-align:top;word-wrap:break-word}}
 .ft th{{background:#343a40;color:#fff;text-align:left}}
 .ft tr:nth-child(even){{background:#f8f9fa}}
 .fn{{font-family:Consolas,monospace;font-size:11px;word-break:break-all}}
 .tc{{font-family:Consolas,monospace;font-size:11px}} .rs{{font-style:italic;color:#555}}
</style></head><body>
<h1>MusicTagger Report</h1>
<p>Generated: {now}</p>
<div class="summary"><h2 style="border:none;margin-top:0">Summary</h2>
<p style="font-weight:bold;color:{mode_color}">{'⚠ ' if dry_run else ''}{mode}</p>
<table>
<tr><th>Metric</th><th>Count</th></tr>
<tr><td>Albums scanned</td><td><strong>{n_albums}</strong></td></tr>
<tr><td>Files scanned</td><td>{n_files}</td></tr>
<tr><td>Files with proposed changes</td><td><strong>{n_changed_files}</strong></td></tr>
<tr><td>Albums consulting Claude</td><td>{claude_albums}</td></tr>
{summary_rows}
</table>
<p style="background:#eaf4fb;padding:8px 10px;border-radius:4px;margin-top:10px;display:inline-block">
Estimated Claude cost: <strong>${est_cost:.4f}</strong> ({claude_albums} album(s) at {model})</p>
</div>
<h2>Albums</h2>
{''.join(album_sections)}
</body></html>"""
