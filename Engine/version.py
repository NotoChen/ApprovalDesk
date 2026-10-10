"""Read the application version from the same plist used by the macOS bundle."""
from pathlib import Path
import plistlib


def application_version():
    source = Path(__file__).resolve()
    for path in (source.parents[2] / "Info.plist", source.parents[1] / "Resources/Info.plist"):
        if path.is_file():
            with path.open("rb") as handle:
                return plistlib.load(handle)["CFBundleShortVersionString"]
    return "development"
