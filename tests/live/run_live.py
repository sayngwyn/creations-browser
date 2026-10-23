"""Run the live acceptance harness in a test MO2 and put it back as it was.

    python tests/live/run_live.py --mo2 <portable MO2 folder> --profile <name> --game fallout4|skyrimspecialedition
        --free-id N --paid-id N --search TEXT --results <json path> [--backup-dir <folder>] [--from-zip <release zip>]

Refuses if this instance's ModOrganizer.exe is running (other installations' MO2s may run beside it). Snapshots the
folder (paths and sizes), byte-copies the profile's files and ModOrganizer.ini, copies the plugin package (from ``src``,
or from the built release zip with ``--from-zip``, which is the release check) and the harness into ``plugins``, starts
MO2 and waits for it to exit, then removes what it added, restores any profile file or ModOrganizer.ini that MO2
rewrote, and prints what changed.
"""

from __future__ import annotations

import argparse
import filecmp
import json
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SKIP_DIRS = {"logs", "crashDumps", "webcache"}


def mo2_running(folder: Path) -> bool:
    """Whether a Mod Organizer 2 started from ``folder`` is running (other installations' MO2s may run beside it)."""
    out = subprocess.run(["powershell", "-NoProfile", "-Command",
                          "Get-Process ModOrganizer -ErrorAction SilentlyContinue | ForEach-Object { $_.Path }"],
                         capture_output=True, text=True).stdout
    mine = str((folder / "ModOrganizer.exe").resolve()).lower()
    return any(line.strip().lower() == mine for line in out.splitlines())


def game_path(mo2: Path) -> Path | None:
    """The game folder this MO2 points at (``gamePath`` in ModOrganizer.ini), or None."""
    for line in (mo2 / "ModOrganizer.ini").read_text(encoding="utf-8", errors="replace").splitlines():
        if line.startswith("gamePath="):
            value = line.split("=", 1)[1].strip()
            if value.startswith("@ByteArray(") and value.endswith(")"):
                value = value[len("@ByteArray("):-1]
            path = Path(value.replace("\\\\", "\\"))
            return path if path.is_dir() else None
    return None


def snapshot(root: Path) -> dict[str, int]:
    out = {}
    for p in root.rglob("*"):
        rel = p.relative_to(root)
        if rel.parts and rel.parts[0] in SKIP_DIRS or "__pycache__" in rel.parts and "creations_browser" not in rel.parts:
            continue  # MO2 compiles its own Python plugins on start; that is not ours
        if p.is_file():
            out[str(rel)] = p.stat().st_size
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mo2", required=True, type=Path)
    ap.add_argument("--profile", required=True)
    ap.add_argument("--game", required=True)
    ap.add_argument("--free-id", required=True, type=int)
    ap.add_argument("--paid-id", required=True, type=int)
    ap.add_argument("--search", required=True)
    ap.add_argument("--results", required=True, type=Path)
    ap.add_argument("--backup-dir", type=Path)
    ap.add_argument("--timeout", type=int, default=900)
    ap.add_argument("--no-install", action="store_true", help="stop after the download (no install or removal)")
    ap.add_argument("--screenshots-dir", type=Path, help="save window screenshots here")
    ap.add_argument("--owned-expect", type=int, help="a content id the game's own Creations list must contain")
    ap.add_argument("--from-zip", type=Path, help="release check: deploy the plugin from this built release zip, not from src")
    ap.add_argument("--control-zip", type=Path, help="diagnosis: no Creations Browser; install and remove this zip only")
    ap.add_argument("--remove-after", action="store_true", help="diagnosis: delete the test mod after MO2 exits, not in MO2")
    ap.add_argument("--no-install-hook", action="store_true", help="diagnosis: deploy with the onModInstalled hook off")
    a = ap.parse_args()

    mo2 = a.mo2.resolve()
    exe, plugins = mo2 / "ModOrganizer.exe", mo2 / "plugins"
    if not exe.is_file() or not (mo2 / "portable.txt").is_file():
        sys.exit(f"not a portable MO2: {mo2}")
    if mo2_running(mo2):
        sys.exit("this Mod Organizer 2 is already running; not starting a second one on the same folder")
    pkg_dest, harness_dest, cfg_dest = plugins / "creations_browser", plugins / "cf_live_harness.py", plugins / "cf_live_harness.json"
    for p in (pkg_dest, harness_dest, cfg_dest):
        if p.exists():
            sys.exit(f"already present, refusing to overwrite: {p}")

    profile = mo2 / "profiles" / a.profile
    backup = a.backup_dir or (a.results.parent / "backup")
    if backup.exists():
        sys.exit(f"backup folder already exists: {backup}")
    backup.mkdir(parents=True)
    guarded = [mo2 / "ModOrganizer.ini"] + ([p for p in profile.iterdir() if p.is_file()] if profile.is_dir() else [])
    for p in guarded:
        shutil.copy2(p, backup / (p.name if p.parent == profile else "_root_" + p.name))
    before = snapshot(mo2)
    started = time.time() - 2  # file times can round down

    if a.from_zip and a.control_zip:
        sys.exit("--from-zip and --control-zip don't go together")
    if a.from_zip:  # the release zip's own plugins\creations_browser, exactly as a user would copy it
        prefix = "plugins/creations_browser/"
        with zipfile.ZipFile(a.from_zip) as zf:
            members = [n for n in zf.namelist() if n.startswith(prefix) and not n.endswith("/")]
            if not any(n == prefix + "__init__.py" for n in members):
                sys.exit(f"no plugins/creations_browser/__init__.py in {a.from_zip}")
            for n in members:
                target = plugins / n[len("plugins/"):]
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(zf.read(n))
        report_zip = str(a.from_zip.resolve())
    elif not a.control_zip:
        shutil.copytree(REPO / "src" / "creations_browser", pkg_dest, ignore=shutil.ignore_patterns("__pycache__"))
        report_zip = None
    else:
        report_zip = None
    if a.no_install_hook and not a.control_zip:  # diagnosis only: the deployed copy does not watch installs
        p = pkg_dest / "plugin.py"
        text = p.read_text(encoding="utf-8")
        hook = "organizer.modList().onModInstalled(self._on_mod_installed)"
        if hook not in text:
            sys.exit("the install hook line was not found in plugin.py")
        p.write_text(text.replace(hook, "pass  # hook off for this live diagnosis"), encoding="utf-8")
    shutil.copy2(Path(__file__).with_name("cf_live_harness.py"), harness_dest)
    a.results.parent.mkdir(parents=True, exist_ok=True)
    cfg_dest.write_text(json.dumps({"game": a.game, "free_id": a.free_id, "paid_id": a.paid_id, "search_text": a.search,
                                    "install": not a.no_install, "remove_in_mo2": not a.remove_after,
                                    "control_zip": str(a.control_zip.resolve()) if a.control_zip else None,
                                    "screenshots_dir": str(a.screenshots_dir.resolve()) if a.screenshots_dir else None,
                                    "owned_expect": a.owned_expect, "results": str(a.results.resolve())}), encoding="utf-8")
    report: dict = {"mo2": str(mo2), "profile": a.profile, "deployed_from": report_zip or "src"}
    try:
        proc = subprocess.Popen([str(exe), "--multiple", "-p", a.profile], cwd=str(mo2))  # other installations' MO2s may be open
        try:
            report["exit_code"] = proc.wait(timeout=a.timeout)
        except subprocess.TimeoutExpired:
            report["exit_code"] = "TIMEOUT: MO2 still running; close it by hand, then rerun cleanup"
            print(json.dumps(report, indent=1))
            return 2
    finally:
        if not mo2_running(mo2):
            for p in (harness_dest, cfg_dest):
                p.unlink(missing_ok=True)
            shutil.rmtree(pkg_dest, ignore_errors=True)
            harness_cache = plugins / "__pycache__"
            if harness_cache.is_dir():
                for c in harness_cache.glob("cf_live_harness*.pyc"):
                    c.unlink()
                if not any(harness_cache.iterdir()) and "plugins\\__pycache__" not in "".join(before):
                    harness_cache.rmdir()

    if a.results.is_file():
        left = json.loads(a.results.read_text(encoding="utf-8")).get("facts", {}).get("left_for_runner")
        if left:
            target = (mo2 / "mods" / left).resolve()
            if target.parent == (mo2 / "mods").resolve() and target.is_dir() and not mo2_running(mo2):
                meta = target / "meta.ini"  # as MO2 saved it on exit
                report["mod_meta_after_exit"] = meta.read_text(encoding="utf-8", errors="replace") if meta.is_file() else None
                shutil.rmtree(target)
                report["removed_after_exit"] = left

    game = game_path(mo2)
    if game is not None:  # the game folder (e.g. the clean Steam install) must never change during a run
        changed = [str(p.relative_to(game)) for p in game.rglob("*") if p.is_file() and p.stat().st_mtime >= started]
        report["game_folder_changes"] = changed[:50]
        if changed:
            report["game_folder_changed"] = True

    restored = []
    for p in guarded:
        saved = backup / (p.name if p.parent == profile else "_root_" + p.name)
        if not p.exists() or not filecmp.cmp(saved, p, shallow=False):
            shutil.copy2(saved, p)
            restored.append(str(p.relative_to(mo2)))
    after = snapshot(mo2)
    report.update(restored=restored, added=sorted(set(after) - set(before)), removed=sorted(set(before) - set(after)),
                  resized=sorted(k for k in set(before) & set(after) if before[k] != after[k]))
    if a.results.is_file():
        report["harness"] = json.loads(a.results.read_text(encoding="utf-8"))
    print(json.dumps(report, indent=1))
    return 0 if report.get("harness", {}).get("passed") and not report.get("game_folder_changed") else 1


if __name__ == "__main__":
    sys.exit(main())
