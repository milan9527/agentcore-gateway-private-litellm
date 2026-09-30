"""Capture read-only network configuration for the LiteLLM verification report."""
import json
import socket
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import boto3
from botocore.config import Config

root = Path(__file__).resolve().parent
state = json.loads((root / "private-litellm-state.json").read_text())
session = boto3.Session(region_name=state["region"])
cfg = Config(connect_timeout=5, read_timeout=20, retries={"total_max_attempts": 2})
ec2 = session.client("ec2", config=cfg)
elbv2 = session.client("elbv2", config=cfg)
control = session.client("bedrock-agentcore-control", config=cfg)

checks = {
    "instance": lambda: ec2.describe_instances(InstanceIds=[state["instanceId"]]),
    "security-groups": lambda: ec2.describe_security_groups(
        GroupIds=[state[k + "SecurityGroup"] for k in ["lattice", "alb", "ec2"]]),
    "lattice-network-interfaces": lambda: ec2.describe_network_interfaces(
        Filters=[{"Name": "group-id", "Values": [state["latticeSecurityGroup"]]}]),
    "internal-load-balancer": lambda: elbv2.describe_load_balancers(
        LoadBalancerArns=[state["loadBalancerArn"]]),
    "target-health": lambda: elbv2.describe_target_health(TargetGroupArn=state["targetGroupArn"]),
    "inference-target": lambda: control.get_gateway_target(
        gatewayIdentifier=state["gatewayId"], targetId=state["targetId"]),
    "dns-addresses": lambda: {"routingDomain": state["loadBalancerDns"],
        "ipv4": sorted({a[4][0] for a in socket.getaddrinfo(
            state["loadBalancerDns"], 443, family=socket.AF_INET)})},
}
with ThreadPoolExecutor(max_workers=7) as pool:
    pending = [(name, pool.submit(function)) for name, function in checks.items()]
    for name, future in pending:
        result = future.result()
        path = Path(state["artifactDirectory"]) / ("proof-" + name + ".json")
        path.write_text(json.dumps(result, indent=2, default=str) + "\n")
        print(name + ": " + str(path))
