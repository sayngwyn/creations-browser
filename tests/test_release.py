"""The release build: what goes in the zip, and that the same source always gives the same zip. Builds happen in a temporary
copy of the project, never in the real tree."""

from __future__ import annotations

import importlib.util
import os
import shutil
import time
import zipfile
from pathlib import Path

import pytest

PROJECT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location("build_release", PROJECT / "scripts" / "build_release.py")
build_release = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(build_release)


@pytest.fixture
def tree(tmp_path):
    root = tmp_path / "project"
    shutil.copytree(PROJECT / "src", root / "src", ignore=shutil.ignore_patterns("__pycache__"))
    for name in build_release.ROOT_FILES:
        shutil.copy(PROJECT / name, root / name)
    return root


def names(zip_path):
    with zipfile.ZipFile(zip_path) as zf:
        return zf.namelist()


def test_the_zip_holds_the_plugin_files_and_the_root_documents_only(tree):
    (tree / "src" / "creations_browser" / "__pycache__").mkdir()
    (tree / "src" / "creations_browser" / "__pycache__" / "core.cpython-312.pyc").write_bytes(b"x")
    (tree / "src" / "creations_browser" / "notes.txt").write_text("not for release")
    (tree / "tests").mkdir()
    (tree / "tests" / "test_core.py").write_text("x")
    target = build_release.build(tree)
    version = build_release.read_version(tree)
    assert target == tree / "dist" / f"CreationsBrowser-{version}.zip" and version == "1.2.0"
    got = names(target)
    expected_py = sorted(p.name for p in (PROJECT / "src" / "creations_browser").glob("*.py"))
    assets = ["OFL.txt", "SayngwynSerif-Regular.ttf", "sayngwyn-mark.png", "check-tick.svg", "check-tick-muted.svg",
              "check-tick-white.svg"]
    assert got == sorted(["CHANGELOG.md", "LICENSE", "README.md", *[f"plugins/creations_browser/{n}" for n in expected_py],
                          *[f"plugins/creations_browser/assets/{n}" for n in assets]])
    assert {"__init__.py", "core.py", "plugin.py", "window.py", "texts.py", "theme.py"} <= set(expected_py)
    assert not any("pycache" in n or "test" in n.lower() or n.endswith(".pyc") or n == "plugins/creations_browser/notes.txt"
                   for n in got)
    with zipfile.ZipFile(target) as zf:
        assert zf.testzip() is None
        for n in got:
            compile(zf.read(n).decode("utf-8"), n, "exec") if n.endswith(".py") else None
        assert b"def createPlugin" in zf.read("plugins/creations_browser/__init__.py")
        for n in assets[1:]:  # images and fonts go in byte for byte (no line-ending rewrite)
            source = PROJECT / "src" / "creations_browser" / "assets" / n
            assert zf.read(f"plugins/creations_browser/assets/{n}") == source.read_bytes()


def test_only_the_named_assets_ship_and_text_assets_are_normalised(tree):
    assets = tree / "src" / "creations_browser" / "assets"
    (assets / "draft-notes.txt").write_text("not for release")
    (assets / "other.png").write_bytes(b"\x89PNG\r\n")
    first = build_release.build(tree).read_bytes()
    assert not any(n.endswith(("draft-notes.txt", "other.png")) for n in names(tree / "dist" / "CreationsBrowser-1.2.0.zip"))
    ofl = assets / "OFL.txt"
    ofl.write_bytes(ofl.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n"))  # a Windows checkout of the licence
    assert build_release.build(tree).read_bytes() == first


def test_a_font_never_ships_without_its_licence(tree):
    (tree / "src" / "creations_browser" / "assets" / "OFL.txt").unlink()
    with pytest.raises(SystemExit, match="OFL.txt"):
        build_release.build(tree)


def test_the_same_source_gives_the_same_zip_even_with_new_timestamps_and_line_endings(tree):
    first = build_release.build(tree).read_bytes()
    later = time.time() + 3600
    for path in (tree / "src" / "creations_browser").glob("*.py"):
        os.utime(path, (later, later))
    assert build_release.build(tree).read_bytes() == first
    readme = tree / "README.md"
    readme.write_bytes(readme.read_bytes().replace(b"\n", b"\r\n"))  # a Windows checkout of the same text
    assert build_release.build(tree).read_bytes() == first
    (tree / "src" / "creations_browser" / "texts.py").write_text("# changed\n", encoding="utf-8")
    assert build_release.build(tree).read_bytes() != first


def test_entries_have_fixed_dates_and_are_sorted(tree):
    with zipfile.ZipFile(build_release.build(tree)) as zf:
        assert [i.filename for i in zf.infolist()] == sorted(i.filename for i in zf.infolist())
        assert {i.date_time for i in zf.infolist()} == {(1980, 1, 1, 0, 0, 0)}
        assert {i.compress_type for i in zf.infolist()} == {zipfile.ZIP_STORED}


def test_the_build_writes_only_into_dist_and_leaves_no_part_file(tree):
    before = {p.relative_to(tree) for p in tree.rglob("*")}
    build_release.build(tree)
    after = {p.relative_to(tree) for p in tree.rglob("*")}
    assert {p.parts[0] for p in after - before} == {"dist"}
    assert not list((tree / "dist").glob("*.part"))


def test_a_missing_document_or_changelog_entry_stops_the_build(tree):
    (tree / "LICENSE").unlink()
    with pytest.raises(SystemExit, match="missing LICENSE"):
        build_release.build(tree)
    shutil.copy(PROJECT / "LICENSE", tree / "LICENSE")
    (tree / "CHANGELOG.md").write_text("# Changelog\n\n## 0.9.0\n", encoding="utf-8")
    with pytest.raises(SystemExit, match="no entry for 1.2.0"):
        build_release.build(tree)


def test_the_real_build_script_stays_inside_dist():
    text = (PROJECT / "scripts" / "build_release.py").read_text(encoding="utf-8")
    assert text.count('root / "dist"') == 1 and "rmtree" not in text and "unlink" not in text
    assert (PROJECT / ".gitignore").read_text().split().count("dist/") == 1
