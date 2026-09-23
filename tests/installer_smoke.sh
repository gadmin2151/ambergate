#!/usr/bin/env bash
# Run only in the disposable Debian CI container, with the CI Docker socket mounted.
set -euo pipefail
bash /first-start.sh --help >/dev/null
if bash /first-start.sh --http-port 0 >/dev/null 2>&1; then exit 1; fi
if bash /first-start.sh --admin-port 80 >/dev/null 2>&1; then exit 1; fi
if bash /first-start.sh --unknown >/dev/null 2>&1; then exit 1; fi
bash /first-start.sh --no-start --with-docker --dir /tmp/gateway-install
bash /first-start.sh --check
# Verify published ports, read-only mount and idempotence, without creating a container.
python3 - <<'PY'
import json,pathlib
root=pathlib.Path('/tmp/gateway-install')
config=json.loads((root/'compose.yaml').read_text())
service=config['services']['ambergate']
assert service['ports']==['80:80','127.0.0.1:8083:8083']
assert service['volumes'][-1]['read_only'] is True
(root/'data/sentinel').write_text('preserve existing configuration')
(root/'original-compose').write_bytes((root/'compose.yaml').read_bytes())
PY
bash /first-start.sh --no-start --dir /tmp/gateway-install --http-port 8080
cmp /tmp/gateway-install/compose.yaml /tmp/gateway-install/original-compose
test "$(cat /tmp/gateway-install/data/sentinel)" = 'preserve existing configuration'
# A new install honors custom ports and IPv6; discovery stays optional.
bash /first-start.sh --no-start --dir /tmp/gateway-custom --admin-bind ::1 --admin-port 18083 --http-port 18080
python3 - <<'PY'
import json,pathlib
service=json.loads(pathlib.Path('/tmp/gateway-custom/compose.yaml').read_text())['services']['ambergate']
assert service['ports']==['18080:80','[::1]:18083:8083']
assert len(service['volumes'])==3
PY
# Legacy service names and data survive an in-place image upgrade.
python3 - <<'PYCODE'
import json,pathlib
p=pathlib.Path('/tmp/gateway-install/compose.yaml')
config=json.loads(p.read_text())
config['services']['gateway']=config['services'].pop('ambergate')
p.write_text(json.dumps(config))
PYCODE
cp /tmp/gateway-install/compose.yaml /tmp/gateway-install/legacy-compose
bash /first-start.sh --no-start --dir /tmp/gateway-install
cmp /tmp/gateway-install/compose.yaml /tmp/gateway-install/legacy-compose
mkdir -p /opt/nginx-scale-gw
cp /tmp/gateway-install/compose.yaml /opt/nginx-scale-gw/compose.yaml
if bash /first-start.sh --no-start >/tmp/legacy-install-output 2>&1; then exit 1; fi
grep -q 'existing installation' /tmp/legacy-install-output
test ! -e /opt/ambergate/compose.yaml
printf 'PASS: dependency installation, checks, existing data, custom ports and socket mounting\n'
