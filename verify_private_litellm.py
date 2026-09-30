"""Invoke the retained Gateway with SigV4; save responses without credentials."""
import argparse
import datetime
import json
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

import boto3
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest

ROOT = Path(__file__).resolve().parent
state = json.loads((ROOT / "private-litellm-state.json").read_text())
session = boto3.Session(region_name=state["region"])
out = Path(state["artifactDirectory"])


def invoke(label, model, stream=False):
    probe = f"{label}-{uuid.uuid4().hex[:12]}"
    payload = {
        "model": "litellm-private/" + model,
        "messages": [{"role": "user", "content":
                      f"Reply with exactly this text and nothing else: PRIVATE_PATH_OK_{probe}"}],
        "max_tokens": 256,
        "stream": stream,
    }
    body = json.dumps(payload).encode()
    url = state["gatewayUrl"].rstrip("/") + "/inference/v1/chat/completions"
    request = AWSRequest(method="POST", url=url, data=body, headers={
        "Content-Type": "application/json", "x-probe-id": probe,
    })
    SigV4Auth(session.get_credentials().get_frozen_credentials(),
              "bedrock-agentcore", state["region"]).add_auth(request)
    sent = datetime.datetime.now(datetime.timezone.utc).isoformat()
    start = time.monotonic()
    try:
        response = urllib.request.urlopen(
            urllib.request.Request(url, data=body, headers=dict(request.headers), method="POST"),
            timeout=120)
    except urllib.error.HTTPError as error:
        response = error
    raw = response.read().decode()
    result = {
        "probeId": probe, "sentAt": sent, "durationSeconds": time.monotonic() - start,
        "url": url, "request": payload, "httpStatus": response.status,
        "headers": dict(response.headers), "body": raw,
    }
    path = out / f"{probe}.json"
    path.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2), flush=True)
    if response.status != 200:
        raise RuntimeError(f"Gateway returned HTTP {response.status}: {path}")
    if stream:
        assert "[DONE]" in raw, "SSE stream did not finish"
        chunks = [json.loads(line[6:]) for line in raw.splitlines()
                  if line.startswith("data: ") and line[6:] != "[DONE]"]
        content = "".join(c.get("choices", [{}])[0].get("delta", {}).get("content") or ""
                          for c in chunks if c.get("choices"))
        assert content, "No streamed model text"
        print("STREAMED_CONTENT: " + content)
    else:
        parsed = json.loads(raw)
        content = parsed["choices"][0]["message"].get("content")
        assert content, "No model text"
        if model == "private-network-check":
            assert content == "LITELLM_PRIVATE_NETWORK_OK", "Unexpected mock response"
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--test", choices=["mock", "real", "stream", "all"], default="all")
    args = parser.parse_args()
    if args.test in ("mock", "all"):
        invoke("network", "private-network-check")
    if args.test in ("real", "all"):
        invoke("inference", "private-gpt-oss")
    if args.test in ("stream", "all"):
        invoke("stream", "private-gpt-oss", stream=True)
