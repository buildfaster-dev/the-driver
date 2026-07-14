"""Guard against the nested-.git bug that recurred in phases 06 and 08.

Fixtures are git repos built on demand (`ensure_fixture_git_repo`). Having a
.git INSIDE a fixture on disk is fine — the builder recreates it. The bug is
when `git add -A` (run after the builder, before a commit) captures that .git
as a phantom submodule (mode 160000) or tracks its internals: a fresh clone
then gets the fixture EMPTY.

So the guard checks what git TRACKS, not what exists on disk, and scans every
directory under fixtures/ dynamically — future fixtures are covered without
naming them.
"""

import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _tracked_nested_git(repo_dir: Path) -> list[str]:
    """Fixture paths that git tracks as a nested repo (the clone-breaking bug).

    Parses `git ls-files -s fixtures/` (format: '<mode> <sha> <stage>\\t<path>')
    and flags any gitlink/submodule (mode 160000) or tracked .git internals.
    """
    result = subprocess.run(
        ["git", "ls-files", "-s", "fixtures/"],
        cwd=repo_dir, capture_output=True, text=True, check=True,
    )
    offenders: list[str] = []
    for line in result.stdout.splitlines():
        meta, _, path = line.partition("\t")
        mode = meta.split()[0]
        if mode == "160000":
            offenders.append(f"{path} (phantom submodule, mode 160000)")
        elif path.endswith("/.git") or "/.git/" in path:
            offenders.append(f"{path} (tracked .git internals)")
    return offenders


def test_no_fixture_is_tracked_as_a_nested_git_repo():
    offenders = _tracked_nested_git(REPO_ROOT)
    assert offenders == [], (
        "A fixture is tracked as a nested git repo; a fresh clone would get it "
        "empty. Offenders: " + "; ".join(offenders) + ". Repair: "
        "`git rm --cached <path>`, `rm -rf` the inner .git, then add the real files."
    )


def test_guard_turns_red_on_a_planted_nested_git(tmp_path):
    """Effect check — prove the guard is not a test that always passes.

    Build a throwaway repo whose fixture is itself a git repo (exactly the bug:
    the builder ran before the commit), stage it, and assert the guard flags it.
    Runs entirely in tmp_path; the real repo is untouched.
    """
    def git(*args, cwd=tmp_path):
        subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)

    git("init", "-q")
    fixture = tmp_path / "fixtures" / "badfix"
    fixture.mkdir(parents=True)
    (fixture / "app.py").write_text("x = 1\n")

    # the fixture is itself a git repo with a commit → git records a gitlink
    git("init", "-q", cwd=fixture)
    git("-c", "user.email=t@t", "-c", "user.name=t", "add", "app.py", cwd=fixture)
    git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "x", cwd=fixture)

    git("add", "-A")  # captures fixtures/badfix as mode 160000

    offenders = _tracked_nested_git(tmp_path)
    assert offenders, "guard failed to flag a planted nested-git fixture"
    assert any("badfix" in o and "160000" in o for o in offenders), offenders
