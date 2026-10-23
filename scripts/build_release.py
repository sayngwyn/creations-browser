"""Build the release zip: ``dist\\CreationsBrowser-<version>.zip``.

The zip holds ``plugins\\creations_browser\\`` (the plugin's .py files, plus ``assets\\`` with the brand mark, the Sayngwyn
Serif font and its OFL licence, and the checkbox ticks: no tests, no caches) and README.md, LICENSE and CHANGELOG.md at the zip root. The result is
deterministic: the same source always gives the same bytes (fixed timestamps and attributes, sorted entries, text line endings
normalised to LF; images and fonts byte for byte), so the zip's hash can be compared across machines.

    python scripts/build_release.py

The build writes only into ``dist\\`` (which git ignores). Standard library only.
"""

from __future__ import annotations

import hashlib
import re
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PACKAGE = "creations_browser"
ROOT_FILES = ("README.md", "LICENSE", "CHANGELOG.md")
FIXED_TIME = (1980, 1, 1, 0, 0, 0)
ASSETS = {"OFL.txt", "SayngwynSerif-Regular.ttf", "sayngwyn-mark.png", "check-tick.svg", "check-tick-muted.svg",
          "check-tick-white.svg"}  # exactly these ship from assets\ (nothing stray)
BINARY_TYPES = (".png", ".ttf")  # copied byte for byte; everything else is text and gets its line endings normalised


def read_version(root: Path) -> str:
    text = (root / "src" / PACKAGE / "core.py").read_text(encoding="utf-8")
    found = re.search(r'^VERSION\s*=\s*"([0-9]+(?:\.[0-9]+){2})"\s*$', text, re.M)
    if not found:
        raise SystemExit("could not read VERSION (x.y.z) from src/creations_browser/core.py")
    return found.group(1)


def collect(root: Path) -> list[tuple[str, bytes]]:
    """(zip name, bytes) for every file in the release, sorted by name."""
    package = root / "src" / PACKAGE
    files: list[tuple[str, bytes]] = []
    for path in sorted(package.glob("*.py")):  # top level only: no tests, no __pycache__, no stray files
        files.append((f"plugins/{PACKAGE}/{path.name}", path.read_bytes()))
    if f"plugins/{PACKAGE}/__init__.py" not in dict(files):
        raise SystemExit(f"{package} has no __init__.py")
    assets = package / "assets"
    for path in sorted(assets.iterdir()) if assets.is_dir() else []:  # one level, the named files only
        if path.is_file() and path.name in ASSETS:
            files.append((f"plugins/{PACKAGE}/assets/{path.name}", path.read_bytes()))
    if any(n.endswith(".ttf") for n, _ in files) and f"plugins/{PACKAGE}/assets/OFL.txt" not in dict(files):
        raise SystemExit("a font ships only with its OFL.txt beside it")
    for name in ROOT_FILES:
        path = root / name
        if not path.is_file():
            raise SystemExit(f"missing {name}")
        files.append((name, path.read_bytes()))
    return sorted((name, data if name.lower().endswith(BINARY_TYPES) else data.replace(b"\r\n", b"\n"))
                  for name, data in files)


def build(root: Path = ROOT) -> Path:
    version = read_version(root)
    changelog = (root / "CHANGELOG.md").read_text(encoding="utf-8")
    if not re.search(rf"^## {re.escape(version)}\b", changelog, re.M):
        raise SystemExit(f"CHANGELOG.md has no entry for {version}")
    dist = root / "dist"
    dist.mkdir(exist_ok=True)
    target = dist / f"CreationsBrowser-{version}.zip"
    tmp = target.with_name(target.name + ".part")
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_STORED) as zf:
        for name, data in collect(root):
            info = zipfile.ZipInfo(name, date_time=FIXED_TIME)
            info.compress_type = zipfile.ZIP_STORED
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            zf.writestr(info, data)
    tmp.replace(target)
    return target


def main() -> int:
    target = build()
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    print(f"{target}\nsha256 {digest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
