"""Replay the recorded private-path assertions locally, without AWS credentials."""
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

root = Path(__file__).resolve().parent
with tempfile.TemporaryDirectory(prefix="private-litellm-evidence-") as directory:
    work = Path(directory)
    shutil.copy2(root / "summarize_private_path.py", work / "summarize_private_path.py")
    shutil.copytree(root / "examples" / "evidence", work / "artifacts")
    state = json.loads((root / "examples" / "observed-resources.json").read_text())
    state["artifactDirectory"] = str(work / "artifacts")
    (work / "private-litellm-state.json").write_text(json.dumps(state))
    subprocess.run([sys.executable, str(work / "summarize_private_path.py")], check=True)
    print("Recorded evidence replay passed; this did not make a live AWS request.")
