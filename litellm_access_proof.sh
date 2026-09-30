#!/bin/bash
set -euo pipefail
/opt/litellm/venv/bin/python - <<'PY'
import importlib.metadata, json
from pathlib import Path
print(json.dumps({"litellmVersion": importlib.metadata.version("litellm")}))
rows = []
for line in Path("/var/log/nginx/private-proof.log").read_text().splitlines():
    try:
        row = json.loads(line)
    except ValueError:
        continue
    if row.get("probe_id") or row.get("uri") in ("/v1/models", "/models"):
        rows.append(row)
for row in rows[-100:]:
    print(json.dumps(row))
PY
systemctl is-active litellm nginx
