#!/bin/bash
set -euo pipefail
dnf install -y python3.12 python3.12-pip nginx
python3.12 -m venv /opt/litellm/venv
/opt/litellm/venv/bin/pip install 'litellm[proxy]==1.103.1' 'prisma==0.15.0' 'boto3==1.43.105'
id litellm >/dev/null 2>&1 || useradd --system --home-dir /opt/litellm litellm
mkdir -p /etc/litellm
cat >/etc/litellm/config.yaml <<'YAML'
model_list:
  - model_name: private-gpt-oss
    litellm_params:
      model: bedrock/converse/openai.gpt-oss-20b-1:0
      aws_region_name: ap-northeast-1
  - model_name: private-network-check
    litellm_params:
      model: openai/network-check
      api_key: unused
      mock_response: "LITELLM_PRIVATE_NETWORK_OK"
general_settings:
  master_key: os.environ/LITELLM_MASTER_KEY
litellm_settings:
  drop_params: true
YAML
/opt/litellm/venv/bin/python - <<'PY'
import boto3, json, os
key = json.loads(boto3.client("secretsmanager", region_name="ap-northeast-1").get_secret_value(
    SecretId="__SECRET_ARN__"
)["SecretString"])["apiKey"]
fd = os.open("/etc/litellm/environment", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
with os.fdopen(fd, "w") as f:
    f.write("LITELLM_MASTER_KEY=" + key + "\n")
    f.write("AWS_DEFAULT_REGION=ap-northeast-1\n")
    f.write("LITELLM_TELEMETRY=False\nDO_NOT_TRACK=1\n")
PY
chown -R litellm:litellm /opt/litellm
cat >/etc/systemd/system/litellm.service <<'UNIT'
[Unit]
Description=Private LiteLLM gateway connectivity test
After=network-online.target
Wants=network-online.target
[Service]
User=litellm
Group=litellm
WorkingDirectory=/opt/litellm
EnvironmentFile=/etc/litellm/environment
ExecStart=/opt/litellm/venv/bin/litellm --config /etc/litellm/config.yaml --host 127.0.0.1 --port 4000
Restart=on-failure
RestartSec=5
[Install]
WantedBy=multi-user.target
UNIT
cat >/etc/nginx/nginx.conf <<'NGINX'
user nginx;
worker_processes auto;
error_log /var/log/nginx/error.log;
pid /run/nginx.pid;
events { worker_connections 1024; }
http {
  log_format proof escape=json
    '{"time":"$time_iso8601","peer":"$remote_addr","forwarded_for":"$http_x_forwarded_for","probe_id":"$http_x_probe_id","method":"$request_method","uri":"$uri","status":$status,"upstream_status":"$upstream_status","seconds":$request_time}';
  access_log /var/log/nginx/private-proof.log proof;
  server {
    listen 4001;
    location / {
      proxy_pass http://127.0.0.1:4000;
      proxy_http_version 1.1;
      proxy_set_header Host $host;
      proxy_set_header X-Forwarded-For $http_x_forwarded_for;
      proxy_buffering off;
      proxy_read_timeout 120s;
    }
  }
}
NGINX
systemctl daemon-reload
systemctl enable --now litellm nginx
touch /opt/litellm/bootstrap-complete
