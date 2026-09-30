"""Run a local diagnostic shell file on the test EC2 using Systems Manager."""
import argparse
import json
import time
from pathlib import Path

import boto3

root = Path(__file__).resolve().parent
state = json.loads((root / "private-litellm-state.json").read_text())
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("script", type=Path)
parser.add_argument("--label", default="ssm-diagnostic")
args = parser.parse_args()
ssm = boto3.client("ssm", region_name=state["region"])
command = ssm.send_command(
    InstanceIds=[state["instanceId"]], DocumentName="AWS-RunShellScript",
    Parameters={"commands": [args.script.read_text()], "executionTimeout": ["600"]},
    Comment=args.label,
)["Command"]["CommandId"]
print("Command ID: " + command, flush=True)
while True:
    try:
        result = ssm.get_command_invocation(CommandId=command, InstanceId=state["instanceId"])
    except ssm.exceptions.InvocationDoesNotExist:
        time.sleep(2)
        continue
    if result["Status"] not in ("Pending", "InProgress", "Delayed"):
        path = Path(state["artifactDirectory"]) / (args.label + "-" + command + ".json")
        path.write_text(json.dumps(result, indent=2, default=str) + "\n")
        print(json.dumps(result, indent=2, default=str))
        if result["Status"] != "Success":
            raise SystemExit(1)
        break
    time.sleep(3)
