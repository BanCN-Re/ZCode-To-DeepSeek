import json, subprocess, sys

out = subprocess.run(['gh', 'api', 'repos/BanCN-Re/ZCode-To-DeepSeek/contents'],
                     capture_output=True, text=True, encoding='utf-8', errors='replace')
try:
    data = json.loads(out.stdout)
except Exception:
    print('raw:', out.stdout[:500], out.stderr[:300])
    sys.exit(1)
for f in sorted(data, key=lambda x: x['name']):
    print('%-8s %8d  %s' % (f['type'], f['size'], f['name']))
print()
print('total', len(data), 'entries')
