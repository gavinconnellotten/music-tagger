import json
from pathlib import Path

p = Path('sample_acoustid_green_day.json')
if not p.exists():
    raise SystemExit('sample_acoustid_green_day.json not found')

with p.open('r', encoding='utf-8') as f:
    data = json.load(f)

records = data['records']
blank_top = [r for r in records if r['top_candidate'] is None or not r['top_candidate'].get('title') or not r['top_candidate'].get('artist')]
print('blank_top_count', len(blank_top))
for r in blank_top[:20]:
    print(r['file'])
    print('  result_count=', r['result_count'])
    print('  top_candidate=', r['top_candidate'])
    print('  candidates sample=', r['candidates'][:5])
    print()
