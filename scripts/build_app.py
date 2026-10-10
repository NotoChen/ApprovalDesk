#!/usr/bin/env python3
"""Build the native app and include its Python runtime and CLI components."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from release_metadata import app_metadata, verify_tag

ROOT = Path(__file__).resolve().parents[1]
MACHO_MAGIC = {b"\xcf\xfa\xed\xfe", b"\xce\xfa\xed\xfe", b"\xca\xfe\xba\xbe", b"\xfe\xed\xfa\xcf"}


def run(command, **kwargs):
    return subprocess.run([str(v) for v in command], check=True, **kwargs)


def copy_executable(source, target):
    temporary = target.with_name(target.name + ".building")
    shutil.copy2(source, temporary)
    temporary.chmod(temporary.stat().st_mode | 0o200)
    temporary.replace(target)


def sdk_path():
    selected = subprocess.check_output(["xcrun", "--show-sdk-path"], text=True).strip()
    fallback = Path("/Library/Developer/CommandLineTools/SDKs/MacOSX26.5.sdk")
    if "/CommandLineTools/" in selected and fallback.is_dir():
        return str(fallback)
    return selected


def ignored(_folder, names):
    return {name for name in names if name == "__pycache__" or name.endswith(".pyc") or name in {"site-packages", "test", "tests", "idlelib", "turtledemo", "ensurepip"}}


def sign(path):
    result = subprocess.run(["codesign", "--force", "--sign", "-", "--timestamp=none", str(path)], capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError(f"签名失败 {path}: {result.stderr}")


def verify_library_paths(path):
    output = subprocess.check_output(["otool", "-L", str(path)], text=True)
    own_id = None
    if path.suffix == ".dylib":
        ids = subprocess.check_output(["otool", "-D", str(path)], text=True).splitlines()
        own_id = ids[1].strip() if len(ids) > 1 else None
    for line in output.splitlines()[1:]:
        if not line.startswith("\t"):
            continue
        dependency = line.strip().split(" (", 1)[0]
        # LC_ID_DYLIB is this library's install name, not an external dependency.
        if dependency == own_id:
            continue
        if dependency.startswith("/") and not dependency.startswith(("/usr/lib/", "/System/Library/")):
            raise RuntimeError(f"组件不能独立运行：{path.name} 依赖 {dependency}。请使用 uv 的可移植 Python 构建。")


def verify_architecture(path, architecture):
    architectures = subprocess.check_output(["lipo", "-archs", str(path)], text=True).split()
    if architecture not in architectures:
        raise RuntimeError(f"{path.name} 的架构是 {architectures}，需要 {architecture}")


def create_dmg(app, destination, version):
    with tempfile.TemporaryDirectory(prefix="approvaldesk-dmg-") as directory:
        stage = Path(directory)
        run(["ditto", app, stage / app.name])
        (stage / "Applications").symlink_to("/Applications", target_is_directory=True)
        shutil.copy2(ROOT / "Resources/Install.txt", stage / "安装说明.txt")
        run(["hdiutil", "create", "-ov", "-quiet", "-volname", f"审批台 {version}",
             "-srcfolder", stage, "-fs", "HFS+", "-format", "UDZO", destination])
    run(["hdiutil", "verify", "-quiet", destination])


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--configuration", choices=["debug", "release"], default="release")
    parser.add_argument("--skip-build", action="store_true")
    parser.add_argument("--zip", action="store_true")
    parser.add_argument("--dmg", action="store_true")
    parser.add_argument("--tag", help="Verify that a release tag matches Info.plist")
    parser.add_argument("--components-dir", type=Path, help="Directory prepared by prepare_components.py")
    parser.add_argument("--output", type=Path, default=ROOT / "dist")
    args = parser.parse_args()
    if platform.system() != "Darwin" or platform.machine() not in {"arm64", "x86_64"}:
        raise RuntimeError("构建需要 Apple Silicon 或 Intel Mac")
    version, build = app_metadata()
    if args.tag:
        verify_tag(args.tag, version)
    lock = json.loads((ROOT / "Resources/components.lock.json").read_text())
    if args.components_dir and platform.python_version() != lock["python"]:
        raise RuntimeError(f"发行构建需要 Python {lock['python']}，当前为 {platform.python_version()}")
    sdk = sdk_path()
    if not args.skip_build:
        run(["swift", "build", "--sdk", sdk, "-c", args.configuration, "-j", "2"], cwd=ROOT)
    bin_path = Path(subprocess.check_output(["swift", "build", "--show-bin-path", "--sdk", sdk, "-c", args.configuration], cwd=ROOT, text=True).strip())
    binary = bin_path / "ApprovalDesk"
    if not binary.is_file():
        raise RuntimeError(f"编译产物不存在：{binary}")
    app = args.output.resolve() / "审批台.app"
    if app.exists():
        shutil.rmtree(app)
    contents = app / "Contents"
    resources = contents / "Resources"
    (contents / "MacOS").mkdir(parents=True, exist_ok=True)
    resources.mkdir(parents=True, exist_ok=True)
    copy_executable(binary, contents / "MacOS/ApprovalDesk")
    shutil.copy2(ROOT / "Resources/Info.plist", contents / "Info.plist")
    shutil.copytree(ROOT / "Engine", resources / "Engine", dirs_exist_ok=True, ignore=ignored)

    runtime = resources / "Runtime"
    runtime_source = Path(sys.base_prefix)
    verify_library_paths(Path(sys.executable).resolve())
    (runtime / "bin").mkdir(parents=True)
    executable = Path(sys.executable).resolve()
    shutil.copy2(executable, runtime / "bin" / executable.name)
    (runtime / "bin/python3").symlink_to(executable.name)
    shutil.copytree(runtime_source / "lib", runtime / "lib", symlinks=True, ignore=ignored)
    components = resources / "bin"
    components.mkdir(exist_ok=True)
    manifest = {"app_version": version, "app_build": build, "architecture": platform.machine(),
                "python": platform.python_version(), "components": {}}
    for name in ("dws", "glab"):
        location = args.components_dir / name if args.components_dir else shutil.which(name)
        if not location or not Path(location).is_file():
            raise RuntimeError(f"缺少构建组件 {name}，请先按官方方式安装")
        source = Path(location).resolve()
        verify_architecture(source, platform.machine())
        target = components / name
        copy_executable(source, target)
        component_version = run([source, "version"], capture_output=True, text=True).stdout.strip()
        expected = lock["components"][name]["version"]
        if args.components_dir and expected not in component_version:
            raise RuntimeError(f"{name} 应为 {expected}，实际返回 {component_version}")
        manifest["components"][name] = {"version": component_version,
                                         "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest()}
    (resources / "components.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")

    licenses = resources / "Licenses"
    licenses.mkdir(exist_ok=True)
    python_license = runtime_source / f"lib/python{sys.version_info.major}.{sys.version_info.minor}/LICENSE.txt"
    if python_license.exists():
        shutil.copy2(python_license, licenses / "Python-LICENSE.txt")
    urls = {("DWS" if name == "dws" else name) + "-LICENSE.txt": value["license_url"]
            for name, value in lock["components"].items()}
    for name, url in urls.items():
        target = licenses / name
        if not target.exists():
            with urllib.request.urlopen(url, timeout=30) as response:
                target.write_bytes(response.read())

    icon = ROOT / "Resources/AppIcon.icns"
    icon_sources = [ROOT / "Sources/ApprovalDesk/ApprovalMark.swift", ROOT / "scripts/create_icon.swift"]
    iconset = ROOT / ".cache/AppIcon.iconset"
    iconset.mkdir(parents=True, exist_ok=True)
    icon_generator = ROOT / ".cache/create-icon"
    run(["swiftc", "-sdk", sdk, "-parse-as-library", *icon_sources, "-o", icon_generator])
    run([icon_generator, iconset])
    run(["iconutil", "-c", "icns", iconset, "-o", icon])
    shutil.copy2(icon, resources / "AppIcon.icns")

    print("正在签名应用及内置组件", flush=True)
    for path in sorted(resources.rglob("*"), key=lambda p: len(p.parts), reverse=True):
        if path.is_symlink() or not path.is_file():
            continue
        with path.open("rb") as handle:
            magic = handle.read(4)
        if magic in MACHO_MAGIC:
            verify_architecture(path, platform.machine())
            if path.suffix == ".dylib":
                run(["install_name_tool", "-id", "@rpath/" + path.name, path], capture_output=True)
            verify_library_paths(path)
            sign(path)
    sign(contents / "MacOS/ApprovalDesk")
    verify_architecture(contents / "MacOS/ApprovalDesk", platform.machine())
    sign(app)
    run(["codesign", "--verify", "--deep", "--strict", app], stdout=subprocess.DEVNULL)
    probe = run([runtime / "bin/python3", "-B", "-c", "import json,ssl,fcntl,subprocess,threading,queue,ctypes; print('内置 Python 可用')"], env={"PATH": "/usr/bin:/bin", "PYTHONNOUSERSITE": "1"}, cwd="/tmp", capture_output=True, text=True)
    print(probe.stdout.strip())
    print(f"应用已构建：{app}")
    if args.zip:
        archive = app.parent / f"ApprovalDesk-{version}-macos-{platform.machine()}.zip"
        if archive.exists(): archive.unlink()
        run(["ditto", "-c", "-k", "--keepParent", app, archive])
        print(f"分发包：{archive}")
    if args.dmg:
        disk_image = app.parent / f"ApprovalDesk-{version}-macos-{platform.machine()}.dmg"
        create_dmg(app, disk_image, version)
        print(f"安装映像：{disk_image}")


if __name__ == "__main__": main()
