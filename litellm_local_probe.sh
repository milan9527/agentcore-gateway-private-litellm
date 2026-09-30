#!/bin/bash
set -euo pipefail
/opt/litellm/venv/bin/python - <<'PY'
import json, time, urllib.request, urllib.error, uuid
from pathlib import Path
for attempt in range(40):
    try:
        urllib.request.urlopen("http://127.0.0.1:4000/health/liveliness", timeout=2)
        break
    except Exception:
        time.sleep(3)
else:
    raise RuntimeError("LiteLLM not listening")
settings = dict(line.split("=", 1) for line in
                Path("/etc/litellm/environment").read_text().splitlines())
try:
    urllib.request.urlopen("http://127.0.0.1:4000/v1/models", timeout=5)
    raise AssertionError("Unauthenticated model list unexpectedly allowed")
except urllib.error.HTTPError as e:
    assert e.code == 401, e.code
    print("Unauthenticated /v1/models correctly rejected: HTTP 401")
for model in ["private-network-check", "private-gpt-oss"]:
    probe = "local-" + uuid.uuid4().hex[:12]
    payload = {"model": model, "messages": [{
        "role": "user", "content": "Reply with exactly: PRIVATE_LITELLM_OK"}],
        "max_tokens": 256}
    request = urllib.request.Request(
        "http://127.0.0.1:4001/v1/chat/completions",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer " + settings["LITELLM_MASTER_KEY"],
                 "x-probe-id": probe})
    try:
        response = urllib.request.urlopen(request, timeout=120)
    except urllib.error.HTTPError as e:
        print(json.dumps({"model": model, "probeId": probe, "status": e.code,
                          "response": e.read().decode()}))
        raise SystemExit(1)
    parsed = json.load(response)
    print(json.dumps({"model": model, "probeId": probe, "status": response.status,
                      "response": parsed}))
    assert parsed["choices"][0]["message"].get("content"), "Missing model text"
PY
