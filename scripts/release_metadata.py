#!/usr/bin/env python3
"""Keep the tag, app version, release notes, and asset names in agreement."""
import argparse
import hashlib
from pathlib import Path
import plistlib
import re

ROOT = Path(__file__).resolve().parents[1]


def app_metadata(plist=None):
    with (plist or ROOT / "Resources/Info.plist").open("rb") as handle:
        info = plistlib.load(handle)
    version = info["CFBundleShortVersionString"]
    build = info["CFBundleVersion"]
    if not re.fullmatch(r"\d+\.\d+\.\d+", version) or not re.fullmatch(r"\d+", build):
        raise ValueError("Info.plist 需要三段版本号和整数构建号")
    return version, build


def verify_tag(tag, version):
    if tag != f"v{version}":
        raise ValueError(f"标签 {tag!r} 与应用版本 {version} 不一致；预期 v{version}")


def release_notes(version):
    changelog = (ROOT / "CHANGELOG.md").read_text()
    match = re.search(rf"^## {re.escape(version)}(?:[ \t]+[^\n]*)?\n(.*?)(?=^## |\Z)", changelog, re.M | re.S)
    if not match or not match[1].strip():
        raise ValueError(f"CHANGELOG.md 缺少 {version} 的发行说明")
    return match[1].strip() + "\n"


def write_checksums(directory, version):
    expected = {f"ApprovalDesk-{version}-macos-{arch}.{suffix}"
                for arch in ("arm64", "x86_64") for suffix in ("dmg", "zip")}
    actual = {path.name for path in directory.iterdir() if path.suffix in {".dmg", ".zip"}}
    if actual != expected:
        raise ValueError(f"发行资产不完整：缺少 {sorted(expected - actual)}，多出 {sorted(actual - expected)}")
    lines = []
    for name in sorted(expected):
        path = directory / name
        if path.stat().st_size == 0:
            raise ValueError(f"安装包为空：{name}")
        with path.open("rb") as handle:
            digest = hashlib.file_digest(handle, "sha256").hexdigest()
        lines.append(f"{digest}  {name}\n")
    (directory / "SHA256SUMS.txt").write_text("".join(lines))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag")
    parser.add_argument("--notes", action="store_true")
    parser.add_argument("--assets-dir", type=Path)
    args = parser.parse_args()
    version, build = app_metadata()
    if args.tag:
        verify_tag(args.tag, version)
    notes = release_notes(version)
    if args.assets_dir:
        write_checksums(args.assets_dir, version)
    print(notes if args.notes else f"ApprovalDesk {version} (Build {build})", end="" if args.notes else "\n")


if __name__ == "__main__":
    main()
