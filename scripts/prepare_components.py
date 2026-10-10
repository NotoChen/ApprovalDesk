#!/usr/bin/env python3
"""Download the pinned, architecture-specific CLI components used by release builds."""
import argparse
import hashlib
import json
from pathlib import Path
import platform
import shutil
import tarfile
import tempfile
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[1]


def download(url, destination, expected_sha256):
    if destination.exists() and hashlib.sha256(destination.read_bytes()).hexdigest() == expected_sha256:
        return
    for attempt in range(3):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "ApprovalDesk-build"})
            with urllib.request.urlopen(request, timeout=45) as response:
                data = response.read()
            if hashlib.sha256(data).hexdigest() != expected_sha256:
                raise ValueError(f"组件校验值不匹配：{destination.name}")
            destination.write_bytes(data)
            return
        except (OSError, ValueError):
            if attempt == 2:
                raise
            time.sleep(attempt + 1)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--arch", choices=["arm64", "x86_64"], default=platform.machine())
    args = parser.parse_args()
    lock = json.loads((ROOT / "Resources/components.lock.json").read_text())
    output = ROOT / ".cache/build-components" / args.arch
    archives = ROOT / ".cache/component-downloads" / args.arch
    output.mkdir(parents=True, exist_ok=True)
    archives.mkdir(parents=True, exist_ok=True)
    for name, component in lock["components"].items():
        asset = component["macos"][args.arch]
        archive = archives / f"{name}-{component['version']}.tar.gz"
        download(asset["url"], archive, asset["sha256"])
        with tarfile.open(archive) as package:
            matches = [member for member in package.getmembers() if member.isfile() and Path(member.name).name == name]
            if len(matches) != 1:
                raise ValueError(f"{archive.name} 中应只有一个 {name} 可执行文件")
            with package.extractfile(matches[0]) as source, tempfile.NamedTemporaryFile(dir=output, delete=False) as target:
                temporary = Path(target.name)
                try:
                    shutil.copyfileobj(source, target)
                    target.flush()
                    temporary.chmod(0o755)
                    temporary.replace(output / name)
                finally:
                    temporary.unlink(missing_ok=True)
        print(f"已准备 {name} {component['version']} ({args.arch})", flush=True)
    print(f"组件目录：{output}")


if __name__ == "__main__":
    main()
