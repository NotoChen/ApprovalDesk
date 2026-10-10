#!/usr/bin/env python3
"""Run the native model checks with Command Line Tools, without XCTest."""
from pathlib import Path
import subprocess
from build_app import sdk_path

root = Path(__file__).resolve().parents[1]
binary = root / ".cache/model-checks"
binary.parent.mkdir(parents=True, exist_ok=True)
sources = sorted(p for p in (root / "Sources/ApprovalDesk").glob("*.swift") if p.name != "ApprovalDeskApp.swift")
subprocess.run(["swiftc", "-sdk", sdk_path(), "-parse-as-library", *sources,
                *sorted((root / "Tests/ApprovalDeskTests").glob("*.swift")),
                "-o", binary], check=True)
subprocess.run([binary], check=True)
