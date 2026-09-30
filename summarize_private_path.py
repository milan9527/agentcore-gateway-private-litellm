"""Correlate signed Gateway requests with LiteLLM logs and actual Lattice ENIs."""
import ipaddress
import json
from pathlib import Path

root = Path(__file__).resolve().parent
state = json.loads((root / "private-litellm-state.json").read_text())
out = Path(state["artifactDirectory"])


def load(name):
    return json.loads((out / name).read_text())


interfaces = load("proof-lattice-network-interfaces.json")["NetworkInterfaces"]
lattice_ips = {address["PrivateIpAddress"]
               for interface in interfaces for address in interface["PrivateIpAddresses"]}
assert lattice_ips, "No network interfaces found for the Lattice security group"
assert all(ipaddress.ip_address(ip).is_private for ip in lattice_ips)

instance = load("proof-instance.json")["Reservations"][0]["Instances"][0]
assert not instance.get("PublicIpAddress"), "EC2 unexpectedly has a public IP"
lb = load("proof-internal-load-balancer.json")["LoadBalancers"][0]
assert lb["Scheme"] == "internal"
groups = {g["GroupId"]: g for g in load("proof-security-groups.json")["SecurityGroups"]}


def only_group_ingress(group_id, source_group, port):
    permissions = groups[group_id]["IpPermissions"]
    assert len(permissions) == 1, f"Unexpected ingress rules on {group_id}"
    rule = permissions[0]
    assert rule["IpProtocol"] == "tcp" and rule["FromPort"] == rule["ToPort"] == port
    assert not rule.get("IpRanges") and not rule.get("Ipv6Ranges") and not rule.get("PrefixListIds")
    assert {item["GroupId"] for item in rule["UserIdGroupPairs"]} == {source_group}


only_group_ingress(state["albSecurityGroup"], state["latticeSecurityGroup"], 443)
only_group_ingress(state["ec2SecurityGroup"], state["albSecurityGroup"], 4001)

logs = []
for path in out.glob("access-proof-*.json"):
    result = json.loads(path.read_text())
    assert result["Status"] == "Success"
    for line in result["StandardOutputContent"].splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if row.get("probe_id"):
            logs.append(row)

matches = []
for prefix in ["network", "inference", "stream"]:
    responses = sorted(out.glob(prefix + "-*.json"))
    responses = [json.loads(p.read_text()) for p in responses]
    responses = [r for r in responses if r.get("httpStatus") == 200]
    assert responses, f"No successful {prefix} Gateway response"
    response = max(responses, key=lambda r: r["sentAt"])
    matching_logs = [row for row in logs if row["probe_id"] == response["probeId"]]
    assert matching_logs, f"No backend log for {response['probeId']}"
    row = matching_logs[-1]
    assert row["status"] == 200 and row["upstream_status"] == "200"
    assert row["method"] == "POST" and row["uri"] == "/v1/chat/completions"
    sources = {part.strip() for part in row["forwarded_for"].split(",")}
    assert sources & lattice_ips, f"Backend source does not match Lattice ENIs: {row}"
    body = response["body"]
    if prefix == "stream":
        assert "[DONE]" in body
        events = [json.loads(line[6:]) for line in body.splitlines()
                  if line.startswith("data: ") and line[6:] != "[DONE]"]
        content = "".join(event["choices"][0].get("delta", {}).get("content") or ""
                          for event in events if event.get("choices"))
    else:
        content = json.loads(body)["choices"][0]["message"]["content"]
    assert content
    matches.append({
        "test": prefix, "probeId": response["probeId"], "httpStatus": response["httpStatus"],
        "sourceLatticeIp": sorted(sources & lattice_ips), "backendLog": row,
        "content": content, "durationSeconds": response["durationSeconds"],
    })

summary = {
    "gatewayDataPlaneVerified": True,
    "scope": "AgentCore Gateway -> VPC Lattice -> internal ALB -> EC2 LiteLLM",
    "upstreamLimitation": "LiteLLM -> Bedrock still uses the existing NAT/public service endpoint",
    "gatewayId": state["gatewayId"], "targetId": state["targetId"],
    "instanceId": state["instanceId"], "privateIp": instance["PrivateIpAddress"],
    "instancePublicIp": None, "loadBalancerScheme": lb["Scheme"],
    "latticeEniIps": sorted(lattice_ips), "tests": matches,
}
(out / "verified-private-path.json").write_text(json.dumps(summary, indent=2) + "\n")
state.update(gatewayDataPlaneVerified=True, certificateStatus="ISSUED")
(root / "private-litellm-state.json").write_text(json.dumps(state, indent=2) + "\n")
print(json.dumps(summary, indent=2))
