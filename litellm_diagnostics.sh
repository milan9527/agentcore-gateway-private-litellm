#!/bin/bash
set -u
cloud-init status || true
systemctl is-active litellm nginx || true
test -e /opt/litellm/bootstrap-complete && echo BOOTSTRAP_COMPLETE
tail -n 18 /var/log/cloud-init-output.log
journalctl -u litellm -n 30 --no-pager
curl -sS -o /dev/null -w 'LiteLLM without API key: HTTP %{http_code}\n' http://127.0.0.1:4000/v1/models || true
tail -n 30 /var/log/nginx/private-proof.log 2>/dev/null || true
