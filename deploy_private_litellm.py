"""Create and retain an isolated private LiteLLM data-plane test in the existing VPC.

Install requirements.txt and run with Python 3.12.
State is saved after each creation; reruns resume from the same state.
No credentials are written to artifacts or EC2 user data.
"""
import argparse
import datetime
import json
import re
import secrets
import time
import uuid
from pathlib import Path

import boto3
from botocore.config import Config

ROOT = Path(__file__).resolve().parent
STATE = ROOT / "private-litellm-state.json"
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--config", type=Path, default=ROOT / "deployment-config.json",
                    help="Deployment JSON; use a fresh directory for a new deployment.")
args = parser.parse_args()
try:
    settings = json.loads(args.config.read_text())
except (OSError, ValueError) as error:
    parser.error(f"Cannot read config: {error}. Copy deployment-config.example.json and edit it first.")
required = ["expectedAccountId", "region", "vpcId", "subnetIds", "domainSuffix"]
if not isinstance(settings, dict) or any(key not in settings for key in required):
    parser.error("Config must contain " + ", ".join(required))
REGION = settings["region"]
VPC = settings["vpcId"]
SUBNETS = settings["subnetIds"]
EXPECTED_ACCOUNT = settings["expectedAccountId"]
DOMAIN_SUFFIX = str(settings["domainSuffix"]).rstrip(".")
if not isinstance(EXPECTED_ACCOUNT, str) or not re.fullmatch(r"\d{12}", EXPECTED_ACCOUNT):
    parser.error("expectedAccountId must be a 12-digit string.")
if not isinstance(SUBNETS, list) or len(SUBNETS) < 2 or len(set(SUBNETS)) != len(SUBNETS):
    parser.error("Provide distinct subnetIds in at least two availability zones.")
if DOMAIN_SUFFIX == "example.com" or not re.fullmatch(r"[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+", DOMAIN_SUFFIX):
    parser.error("Replace domainSuffix with a DNS domain you control.")
# Set this only when Route 53 is the authoritative public DNS provider.
# With another DNS provider, print the ACM validation record for manual creation.
ZONE = settings.get("hostedZoneId")
session = boto3.Session(region_name=REGION)
config = Config(connect_timeout=5, read_timeout=30, retries={"total_max_attempts": 3})
clients = {}


def client(service):
    if service not in clients:
        clients[service] = session.client(service, config=config)
    return clients[service]


def save():
    STATE.write_text(json.dumps(state, indent=2, default=str) + "\n")


def evidence(name, data):
    (out / (name + ".json")).write_text(json.dumps(data, indent=2, default=str) + "\n")


def note(message):
    print(message, flush=True)


def remember(key, function):
    if key not in state:
        state[key] = function()
        save()
        note(f"Created {key}: {state[key]}")
    return state[key]


def policy(statements):
    return json.dumps({"Version": "2012-10-17", "Statement": statements})


def allow(actions, resources):
    return {"Effect": "Allow", "Action": actions, "Resource": resources}


def role(name, principal):
    response = client("iam").create_role(
        RoleName=name,
        AssumeRolePolicyDocument=policy([{
            "Effect": "Allow", "Principal": {"Service": principal},
            "Action": "sts:AssumeRole",
            **({"Condition": {"StringEquals": {"aws:SourceAccount": account},
                             "ArnLike": {"aws:SourceArn":
                                         f"arn:aws:bedrock-agentcore:{REGION}:{account}:gateway/*"}}}
               if principal == "bedrock-agentcore.amazonaws.com" else {}),
        }]),
        Tags=tags,
    )
    return response["Role"]["Arn"]


if STATE.exists():
    state = json.loads(STATE.read_text())
else:
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    state = {"name": f"litellm-vpc-{uuid.uuid4().hex[:6]}", "createdAt": stamp}
    save()
name = state["name"]
out = ROOT / "artifacts" / f"{state['createdAt']}-{name}"
out.mkdir(parents=True, exist_ok=True)
account = client("sts").get_caller_identity()["Account"]
if account != EXPECTED_ACCOUNT:
    raise RuntimeError(f"Unexpected AWS account: {account}; expected {EXPECTED_ACCOUNT}")
tags = [{"Key": "Project", "Value": name}, {"Key": "Purpose", "Value": "AgentCorePrivateInferenceTest"}]
domain = name + "." + DOMAIN_SUFFIX
for field, value in {"region": REGION, "vpcId": VPC, "subnetIds": SUBNETS, "domain": domain}.items():
    if field in state and state[field] != value:
        raise ValueError(f"Existing deployment {field} differs; use its original config or a fresh directory.")
state.update(region=REGION, vpcId=VPC, subnetIds=SUBNETS, domain=domain, artifactDirectory=str(out))
save()

cert = remember("certificateArn", lambda: client("acm").request_certificate(
    DomainName=domain, ValidationMethod="DNS", IdempotencyToken=name.replace("-", ""),
    Tags=tags)["CertificateArn"])
if "validationRecord" not in state:
    while True:
        info = client("acm").describe_certificate(CertificateArn=cert)["Certificate"]
        records = [x.get("ResourceRecord") for x in info.get("DomainValidationOptions", [])]
        if records and records[0]:
            record = records[0]
            if ZONE:
                client("route53").change_resource_record_sets(
                    HostedZoneId=ZONE, ChangeBatch={"Changes": [{"Action": "CREATE",
                        "ResourceRecordSet": {"Name": record["Name"], "Type": record["Type"],
                                              "TTL": 60, "ResourceRecords": [{"Value": record["Value"]}]}}]})
            state["validationRecord"] = record
            save()
            break
        time.sleep(3)
note("ACM validation CNAME (create at authoritative DNS): " + json.dumps(state["validationRecord"]))

for component in ["lattice", "alb", "ec2"]:
    remember(component + "SecurityGroup", lambda component=component:
             client("ec2").create_security_group(
                 GroupName=f"{name}-{component}", Description=f"{name} {component}",
                 VpcId=VPC, TagSpecifications=[{"ResourceType": "security-group", "Tags": tags}]
             )["GroupId"])

if not state.get("ingressConfigured"):
    for destination, source, port in [
        ("alb", "lattice", 443), ("ec2", "alb", 4001)
    ]:
        try:
            client("ec2").authorize_security_group_ingress(
                GroupId=state[destination + "SecurityGroup"],
                IpPermissions=[{"IpProtocol": "tcp", "FromPort": port, "ToPort": port,
                                "UserIdGroupPairs": [{"GroupId": state[source + "SecurityGroup"],
                                                     "Description": name}]}])
        except client("ec2").exceptions.ClientError as error:
            if error.response["Error"]["Code"] != "InvalidPermission.Duplicate":
                raise
    state["ingressConfigured"] = True
    save()

secret = remember("secretArn", lambda: client("secretsmanager").create_secret(
    Name=f"{name}/litellm-master-key",
    SecretString=json.dumps({"apiKey": "sk-" + secrets.token_urlsafe(36)}),
    Tags=tags)["ARN"])
if not state.get("secretJsonFormat"):
    current = client("secretsmanager").get_secret_value(SecretId=secret)["SecretString"]
    if not current.startswith("{"):
        client("secretsmanager").put_secret_value(
            SecretId=secret, SecretString=json.dumps({"apiKey": current}))
    state["secretJsonFormat"] = True
    save()
provider = remember("credentialProviderArn", lambda:
    client("bedrock-agentcore-control").create_api_key_credential_provider(
        name=name, apiKeySecretSource="EXTERNAL",
        apiKeySecretConfig={"secretId": secret, "jsonKey": "apiKey"},
        tags={x["Key"]: x["Value"] for x in tags})["credentialProviderArn"])

ec2role = remember("ec2RoleArn", lambda: role(name + "-ec2", "ec2.amazonaws.com"))
client("iam").attach_role_policy(RoleName=name + "-ec2",
    PolicyArn="arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore")
client("iam").put_role_policy(RoleName=name + "-ec2", PolicyName="PrivateLiteLLMTest",
    PolicyDocument=policy([
        allow(["secretsmanager:GetSecretValue"], [secret]),
        allow(["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
              [f"arn:aws:bedrock:{REGION}::foundation-model/openai.gpt-oss-20b-1:0"]),
    ]))
if "instanceProfileArn" not in state:
    profile = client("iam").create_instance_profile(InstanceProfileName=name)
    state["instanceProfileArn"] = profile["InstanceProfile"]["Arn"]
    save()
    client("iam").add_role_to_instance_profile(InstanceProfileName=name, RoleName=name + "-ec2")
    time.sleep(12)

if "instanceId" not in state:
    ami = client("ssm").get_parameter(
        Name="/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-arm64")["Parameter"]["Value"]
    bootstrap = ((ROOT / "litellm_bootstrap.sh").read_text()
                 .replace("__SECRET_ARN__", secret).replace("ap-northeast-1", REGION))
    result = client("ec2").run_instances(
        ImageId=ami, InstanceType="t4g.medium", MinCount=1, MaxCount=1, ClientToken=name,
        IamInstanceProfile={"Arn": state["instanceProfileArn"]},
        NetworkInterfaces=[{"DeviceIndex": 0, "SubnetId": SUBNETS[0],
                            "Groups": [state["ec2SecurityGroup"]], "AssociatePublicIpAddress": False}],
        MetadataOptions={"HttpTokens": "required", "HttpEndpoint": "enabled"},
        BlockDeviceMappings=[{"DeviceName": "/dev/xvda", "Ebs": {
            "VolumeSize": 20, "VolumeType": "gp3", "Encrypted": True, "DeleteOnTermination": True}}],
        UserData=bootstrap,
        TagSpecifications=[{"ResourceType": kind, "Tags": tags + [{"Key": "Name", "Value": name}]}
                           for kind in ["instance", "volume"]])
    instance = result["Instances"][0]
    state.update(instanceId=instance["InstanceId"], privateIp=instance["PrivateIpAddress"])
    save()
    evidence("ec2-created", result)
    note(f"EC2 {state['instanceId']} private address {state['privateIp']}")

if "loadBalancerArn" not in state:
    lb = client("elbv2").create_load_balancer(
        Name=name, Scheme="internal", Subnets=SUBNETS,
        SecurityGroups=[state["albSecurityGroup"]], Type="application", IpAddressType="ipv4",
        Tags=tags)["LoadBalancers"][0]
    state.update(loadBalancerArn=lb["LoadBalancerArn"], loadBalancerDns=lb["DNSName"])
    save()
    evidence("internal-alb", lb)

tg = remember("targetGroupArn", lambda: client("elbv2").create_target_group(
    Name=name, Protocol="HTTP", Port=4001, VpcId=VPC, TargetType="instance",
    HealthCheckPath="/health/liveliness", HealthCheckIntervalSeconds=10,
    HealthyThresholdCount=2, UnhealthyThresholdCount=2,
    Matcher={"HttpCode": "200"}, Tags=tags)["TargetGroups"][0]["TargetGroupArn"])
deadline = time.monotonic() + 300
while True:
    instance_status = client("ec2").describe_instances(
        InstanceIds=[state["instanceId"]])["Reservations"][0]["Instances"][0]["State"]["Name"]
    if instance_status == "running":
        break
    if instance_status not in ("pending",) or time.monotonic() > deadline:
        raise RuntimeError("EC2 not running: " + instance_status)
    note("Waiting for EC2 to run")
    time.sleep(5)
client("elbv2").register_targets(TargetGroupArn=tg, Targets=[{"Id": state["instanceId"]}])
# routingDomain supplies the internal ALB address. A public A/CNAME for the
# provider endpoint is unnecessary; only the ACM validation CNAME is required.

gwrole = remember("gatewayRoleArn", lambda: role(name + "-gateway", "bedrock-agentcore.amazonaws.com"))
client("iam").put_role_policy(RoleName=name + "-gateway", PolicyName="PrivateLiteLLMCredentials",
    PolicyDocument=policy([
        allow(["bedrock-agentcore:GetWorkloadAccessToken"],
              [f"arn:aws:bedrock-agentcore:{REGION}:{account}:workload-identity-directory/default",
               f"arn:aws:bedrock-agentcore:{REGION}:{account}:workload-identity-directory/default/workload-identity/{name}-*"]),
        allow(["bedrock-agentcore:GetResourceApiKey"],
              [provider,
               f"arn:aws:bedrock-agentcore:{REGION}:{account}:token-vault/default",
               f"arn:aws:bedrock-agentcore:{REGION}:{account}:workload-identity-directory/default",
               f"arn:aws:bedrock-agentcore:{REGION}:{account}:workload-identity-directory/default/workload-identity/{name}-*"]),
        allow(["secretsmanager:GetSecretValue"], [secret]),
    ]))
if "gatewayId" not in state:
    time.sleep(10)
    gateway = client("bedrock-agentcore-control").create_gateway(
        name=name, roleArn=gwrole, authorizerType="AWS_IAM",
        clientToken=str(uuid.uuid5(uuid.NAMESPACE_DNS, name)),
        description="Private EC2 LiteLLM inference data-plane verification",
        tags={x["Key"]: x["Value"] for x in tags})
    state.update(gatewayId=gateway["gatewayId"], gatewayUrl=gateway["gatewayUrl"])
    save()
    evidence("gateway-created", gateway)

deadline = time.monotonic() + 900
while "listenerArn" not in state:
    status = client("acm").describe_certificate(CertificateArn=cert)["Certificate"]["Status"]
    if status == "ISSUED":
        state["certificateStatus"] = status
        save()
        remember("listenerArn", lambda: client("elbv2").create_listener(
            LoadBalancerArn=state["loadBalancerArn"], Protocol="HTTPS", Port=443,
            Certificates=[{"CertificateArn": cert}],
            SslPolicy="ELBSecurityPolicy-TLS13-1-2-2021-06",
            DefaultActions=[{"Type": "forward", "TargetGroupArn": tg}], Tags=tags
        )["Listeners"][0]["ListenerArn"])
        break
    note("Waiting for certificate: " + status)
    if status == "FAILED" or time.monotonic() > deadline:
        raise RuntimeError("Certificate not issued")
    time.sleep(15)

note("Infrastructure created; waiting for healthy LiteLLM before target registration.")
deadline = time.monotonic() + 900
while True:
    health = client("elbv2").describe_target_health(TargetGroupArn=tg)
    statuses = [h["TargetHealth"]["State"] for h in health["TargetHealthDescriptions"]]
    note("LiteLLM ALB health: " + str(statuses))
    if statuses == ["healthy"]:
        evidence("healthy-backend", health)
        break
    if time.monotonic() > deadline:
        raise TimeoutError("LiteLLM backend not healthy; inspect EC2 cloud-init and systemd logs")
    time.sleep(15)

if "targetId" not in state:
    request = {
        "gatewayIdentifier": state["gatewayId"], "name": "litellm-private",
        "clientToken": str(uuid.uuid5(uuid.NAMESPACE_DNS, name + "-target")),
        "description": "Private-only LiteLLM with API_KEY injection",
        "targetConfiguration": {"inference": {"provider": {
            "endpoint": "https://" + domain,
            "operations": [{"path": "/v1/chat/completions",
                            "models": [{"model": "private-gpt-oss"}, {"model": "private-network-check"}]}]}}},
        "credentialProviderConfigurations": [{"credentialProviderType": "API_KEY", "credentialProvider": {
            "apiKeyCredentialProvider": {"providerArn": provider, "credentialLocation": "HEADER",
                                         "credentialParameterName": "Authorization", "credentialPrefix": "Bearer"}}}],
        "metadataConfiguration": {"allowedRequestHeaders": ["x-probe-id"]},
        "privateEndpoint": {"managedVpcResource": {
            "vpcIdentifier": VPC, "subnetIds": SUBNETS, "endpointIpAddressType": "IPV4",
            "securityGroupIds": [state["latticeSecurityGroup"]],
            "routingDomain": state["loadBalancerDns"],
        }},
    }
    evidence("create-target-request", request)
    response = client("bedrock-agentcore-control").create_gateway_target(**request)
    state["targetId"] = response["targetId"]
    save()
    evidence("create-target-response", response)

deadline = time.monotonic() + 900
while True:
    target = client("bedrock-agentcore-control").get_gateway_target(
        gatewayIdentifier=state["gatewayId"], targetId=state["targetId"])
    evidence("target-latest", target)
    note("Inference target: " + target["status"] + " " + str(target.get("statusReasons", [])))
    if target["status"] == "READY":
        state["targetStatus"] = "READY"
        state["privateEndpointManagedResources"] = target.get("privateEndpointManagedResources", [])
        save()
        break
    if target["status"] == "FAILED" or time.monotonic() > deadline:
        raise RuntimeError("Target did not become ready")
    time.sleep(15)
note("Ready for signed inference invocation. All test resources retained.")
