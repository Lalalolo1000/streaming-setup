#!/usr/bin/env bash
# One-time helper when upgrading an existing installation whose local nodes.json
# still uses 192.168.0.201..224. Names/URLs/quality stay unchanged.
set -euo pipefail
DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
FILE="$DIR/nodes.json"
[ -f "$FILE" ] || { echo "Keine lokale nodes.json gefunden; der neue Default verwendet bereits .101-.124."; exit 0; }
/usr/bin/python3 - "$FILE" <<'PY'
import json, pathlib, re, shutil, sys, time
p=pathlib.Path(sys.argv[1])
data=json.loads(p.read_text())
if not isinstance(data,list): raise SystemExit('nodes.json ist keine Liste')
changed=0
for n in data:
    if not isinstance(n,dict): continue
    t=str(n.get('target',''))
    m=re.fullmatch(r'([^@]+@192\.168\.0\.)(2\d\d)',t)
    if not m: continue
    octet=int(m.group(2))
    if 201 <= octet <= 224:
        n['target']=m.group(1)+str(octet-100)
        changed+=1
if not changed:
    print('Keine .201-.224 Ziele gefunden; nichts geändert.')
    raise SystemExit(0)
backup=p.with_name(f'nodes.json.bak.{int(time.time())}')
shutil.copy2(p,backup)
tmp=p.with_name(p.name+'.tmp')
tmp.write_text(json.dumps(data,indent=2,ensure_ascii=False)+'\n')
tmp.replace(p)
print(f'{changed} IPs auf .101-.124 umgestellt.')
print(f'Backup: {backup}')
PY
