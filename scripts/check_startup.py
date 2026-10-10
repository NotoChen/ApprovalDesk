#!/usr/bin/env python3
"""Check the real native startup path with an installed app's bundled components."""
import argparse
import json
from pathlib import Path
import plistlib
import subprocess
import tempfile
from build_app import sdk_path

root = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser()
parser.add_argument("--app", type=Path, default=Path.home() / "Applications/审批台.app")
parser.add_argument("--config", type=Path, default=Path.home() / "Library/Application Support/ApprovalDesk/settings.json")
parser.add_argument("--groups", action="store_true", help="Also read candidates for both configured group selectors")
args = parser.parse_args()
resources = args.app.resolve() / "Contents/Resources"
if not (resources / "Engine/worker.py").is_file():
    parser.error("App 内的执行引擎不存在")
probe = root / ".cache/account-startup-probe.app/Contents"
binary = probe / "MacOS/AccountStartupProbe"
binary.parent.mkdir(parents=True, exist_ok=True)
link = probe / "Resources"
if link.is_symlink():
    link.unlink()
link.symlink_to(resources, target_is_directory=True)
(probe / "Info.plist").write_bytes(plistlib.dumps({
    "CFBundleExecutable": binary.name,
    "CFBundleIdentifier": "local.approvaldesk.startup-probe",
    "CFBundlePackageType": "APPL",
}))
subprocess.run(["swiftc", "-sdk", sdk_path(), "-parse-as-library",
                *[root / "Sources/ApprovalDesk" / name for name in ("Models.swift", "EngineClient.swift", "LoginItemManager.swift", "AppModel.swift")],
                root / "Tools/CheckAccountStartup.swift", "-o", binary], check=True)
# Defaults now start listening. Probe a disposable snapshot, while the native
# command gate still rejects every non-read-only engine operation.
settings = json.loads(args.config.expanduser().resolve().read_text())
settings.update(start_on_launch=False, launch_at_login=False, show_workbench_on_launch=False)
with tempfile.TemporaryDirectory(prefix="approvaldesk-account-check-") as directory:
    snapshot = Path(directory) / "settings.json"
    snapshot.write_text(json.dumps(settings, ensure_ascii=False))
    subprocess.run([binary, snapshot, *(["--groups"] if args.groups else [])], check=True)
