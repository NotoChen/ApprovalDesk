#!/usr/bin/env python3
"""Verify the actual app, ZIP and DMG without accounts, subscriptions or approvals."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
import tempfile

from build_app import MACHO_MAGIC, verify_architecture, verify_library_paths
from release_metadata import app_metadata, verify_tag


def run(command, **kwargs):
    return subprocess.run([str(item) for item in command], check=True, capture_output=True, text=True, **kwargs)


def fingerprint(app):
    result = {}
    for path in sorted(app.rglob("*")):
        if path.is_symlink():
            if not path.exists():
                raise ValueError(f"安装包中有失效链接：{path}")
            result[str(path.relative_to(app))] = "link:" + os.readlink(path)
        elif path.is_file():
            with path.open("rb") as handle:
                result[str(path.relative_to(app))] = hashlib.file_digest(handle, "sha256").hexdigest()
    return result


def check_app(app, architecture, tag=None):
    version, build = app_metadata(app / "Contents/Info.plist")
    if tag:
        verify_tag(tag, version)
    resources = app / "Contents/Resources"
    manifest = json.loads((resources / "components.json").read_text())
    assert manifest["app_version"] == version and manifest["app_build"] == build
    assert manifest["architecture"] == architecture
    run(["codesign", "--verify", "--deep", "--strict", app])
    for path in app.rglob("*"):
        if path.is_symlink() or not path.is_file():
            continue
        with path.open("rb") as handle:
            if handle.read(4) not in MACHO_MAGIC:
                continue
        verify_architecture(path, architecture)
        verify_library_paths(path)
    before = fingerprint(app)
    python = resources / "Runtime/bin/python3"
    with tempfile.TemporaryDirectory(prefix="approvaldesk-bundle-check-") as directory:
        env = os.environ.copy()
        env.update(PATH="/usr/bin:/bin:/usr/sbin:/sbin", PYTHONNOUSERSITE="1", PYTHONDONTWRITEBYTECODE="1",
                   APPROVALDESK_DATA_DIR=directory)
        for name in ("PYTHONHOME", "PYTHONPATH", "DYLD_LIBRARY_PATH", "DYLD_FRAMEWORK_PATH"):
            env.pop(name, None)
        probe = run([python, "-B", "-c", "import json, ssl, ctypes, fcntl, platform; print(platform.python_version())"],
                    cwd=directory, env=env, timeout=20)
        assert probe.stdout.strip() == manifest["python"]
        # Resolving the bundled DWS uses the production staging function, so its
        # runtime sidecars must be created outside the signed app.
        component_probe = """import json, subprocess, sys
sys.path.insert(0, sys.argv[1])
from configuration import DEFAULTS, executable
result = {}
for name in ('dws', 'glab'):
    path = executable(name, DEFAULTS)
    result[name] = subprocess.check_output([path, 'version'], text=True).strip()
print(json.dumps(result))
"""
        versions = json.loads(run([python, "-B", "-c", component_probe, resources / "Engine"],
                                  cwd=directory, env=env, timeout=40).stdout)
        for name, value in versions.items():
            assert value == manifest["components"][name]["version"], (name, value)
        worker = run([python, "-B", "-u", resources / "Engine/worker.py"],
                     input=json.dumps({"command": "shutdown"}) + "\n", cwd=directory, env=env, timeout=20)
        events = [json.loads(line) for line in worker.stdout.splitlines() if line.strip()]
        assert any(event.get("type") == "hello" and event.get("version") == version for event in events), events
        assert not any(event.get("type") == "error" for event in events), events
    assert fingerprint(app) == before, "运行组件后安装包发生了修改"
    run(["codesign", "--verify", "--deep", "--strict", app])
    print(f"安装包验证通过：{app.name} · {version} ({build}) · {architecture}", flush=True)
    return before


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app", type=Path, required=True)
    parser.add_argument("--arch", choices=["arm64", "x86_64"], default=platform.machine())
    parser.add_argument("--tag")
    parser.add_argument("--archives-dir", type=Path)
    args = parser.parse_args()
    app = args.app.resolve()
    expected = check_app(app, args.arch, args.tag)
    if not args.archives_dir:
        return
    version, _ = app_metadata(app / "Contents/Info.plist")
    prefix = f"ApprovalDesk-{version}-macos-{args.arch}"
    archives = args.archives_dir.resolve()
    with tempfile.TemporaryDirectory(prefix="approvaldesk-archive-check-") as directory:
        root = Path(directory)
        extracted = root / "zip"
        run(["ditto", "-x", "-k", archives / f"{prefix}.zip", extracted])
        assert check_app(extracted / app.name, args.arch, args.tag) == expected, "ZIP 与原始 App 不一致"
        mount = root / "dmg"
        mount.mkdir()
        run(["hdiutil", "attach", "-readonly", "-nobrowse", "-mountpoint", mount, archives / f"{prefix}.dmg"], timeout=45)
        try:
            assert (mount / "Applications").is_symlink()
            assert os.readlink(mount / "Applications") == "/Applications"
            assert (mount / "安装说明.txt").is_file()
            assert check_app(mount / app.name, args.arch, args.tag) == expected, "DMG 与原始 App 不一致"
        finally:
            run(["hdiutil", "detach", mount], timeout=20)
    print("ZIP 解压与 DMG 挂载后的应用均通过独立运行检查", flush=True)


if __name__ == "__main__":
    main()
