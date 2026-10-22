"""Make the next commit of the public history: the current commit's tree without the private project files.

    python scripts/export_public.py 1.1.0
    python scripts/export_public.py 1.1.0 "README: clearer install steps"   (a docs-only update, same version)

The working repository keeps everything (the session record, its instructions and internal reports). The public
repository gets only the product: this script builds a tree from HEAD minus PRIVATE (with git's own plumbing and a
temporary index, so the working tree and the real index are never touched), commits it on the local branch ``public``
(parented on that branch's previous commit, so the public history is one commit per release) and prints the commit.
Push it with ``git push <remote> public:main`` and tag that commit ``v<version>``.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
BRANCH = "public"
PRIVATE = ["STATUS.md", "CLAUDE.md", "reports", "scripts/release_preview"]  # project records and the private draft preview


def git(*args: str, env: dict | None = None) -> str:
    return subprocess.run(["git", *args], cwd=REPO, check=True, capture_output=True, text=True,
                          env={**os.environ, **(env or {})}).stdout.strip()


def main() -> int:
    if len(sys.argv) not in (2, 3) or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", sys.argv[1]):
        sys.exit("usage: export_public.py <version, e.g. 1.1.0> [subject of a docs-only update]")
    version = sys.argv[1]
    subject = sys.argv[2] if len(sys.argv) == 3 else f"Creations Browser {version}"
    if git("status", "--porcelain", "--untracked-files=no"):
        sys.exit("commit or stash your changes first: the export takes HEAD as it is committed")
    changelog = (REPO / "CHANGELOG.md").read_text(encoding="utf-8")
    if not re.search(rf"^## {re.escape(version)}\b", changelog, re.M):
        sys.exit(f"CHANGELOG.md has no entry for {version}")
    source = git("rev-parse", "HEAD")
    with tempfile.TemporaryDirectory() as tmp:
        env = {"GIT_INDEX_FILE": str(Path(tmp) / "index")}
        git("read-tree", source, env=env)
        for path in PRIVATE:
            git("rm", "-r", "--cached", "--quiet", "--ignore-unmatch", "--", path, env=env)
        tree = git("write-tree", env=env)
    parent = subprocess.run(["git", "rev-parse", "--verify", "--quiet", f"refs/heads/{BRANCH}"], cwd=REPO,
                            capture_output=True, text=True).stdout.strip()
    if parent and git("rev-parse", f"{parent}^{{tree}}") == tree:
        print(f"{BRANCH} already holds this tree: {parent}")
        return 0
    message = f"{subject}\n\nPublic {version} tree built from {source[:12]}."
    commit = git("commit-tree", tree, *(["-p", parent] if parent else []), "-m", message)
    git("update-ref", f"refs/heads/{BRANCH}", commit, *([parent] if parent else ["0" * 40]))
    print(f"{BRANCH} -> {commit}")
    print("Files:", git("ls-tree", "-r", "--name-only", commit).replace("\n", ", "))
    return 0


if __name__ == "__main__":
    sys.exit(main())
