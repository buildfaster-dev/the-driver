from the_driver.models import RepoData, FileInfo, CommitInfo
from the_driver.scanner import scan_repo
from datetime import datetime, timezone


def _make_repo(files=None, commits=None):
    files = files or []
    commits = commits or []
    languages = {}
    total_lines = 0
    for f in files:
        languages[f.language] = languages.get(f.language, 0) + 1
        total_lines += f.content.count("\n") + 1
    return RepoData(
        path="/fake/repo",
        files=files,
        commits=commits,
        languages=languages,
        total_files=len(files),
        total_lines=total_lines,
    )


def _make_file(path="app.py", content="print('hello')", language="Python", is_test=False):
    return FileInfo(path=path, content=content, language=language, size=len(content), is_test=is_test)


def _make_commit(message="feat: add feature", files_changed=1):
    return CommitInfo(
        hash="abc12345",
        message=message,
        author="Test Author",
        date=datetime.now(timezone.utc),
        files_changed=files_changed,
        insertions=10,
        deletions=2,
    )


class TestTestRatio:
    def test_no_files(self):
        repo = _make_repo()
        result = scan_repo(repo)
        assert result.test_ratio == 0.0

    def test_no_tests(self):
        repo = _make_repo(files=[_make_file()])
        result = scan_repo(repo)
        assert result.test_ratio == 0.0

    def test_with_tests(self):
        repo = _make_repo(files=[
            _make_file("app.py"),
            _make_file("test_app.py", is_test=True),
        ])
        result = scan_repo(repo)
        assert result.test_ratio == 1.0

    def test_ratio_calculation(self):
        repo = _make_repo(files=[
            _make_file("app.py"),
            _make_file("models.py"),
            _make_file("utils.py"),
            _make_file("test_app.py", is_test=True),
        ])
        result = scan_repo(repo)
        assert abs(result.test_ratio - 1 / 3) < 0.01


class TestCommitQuality:
    def test_good_commits(self):
        repo = _make_repo(commits=[
            _make_commit("feat: add user authentication"),
            _make_commit("fix: handle null pointer in login"),
            _make_commit("refactor: extract validation logic"),
        ])
        result = scan_repo(repo)
        assert result.commit_quality == "good"

    def test_poor_commits(self):
        repo = _make_repo(commits=[
            _make_commit("fix"),
            _make_commit("update"),
            _make_commit("wip"),
        ])
        result = scan_repo(repo)
        assert result.commit_quality == "poor"

    def test_no_commits(self):
        repo = _make_repo()
        result = scan_repo(repo)
        assert result.commit_quality == "poor"


class TestLinterDetection:
    def test_no_linter(self):
        repo = _make_repo(files=[_make_file("app.py")])
        result = scan_repo(repo)
        assert result.has_linter_config is False
        assert result.linter_configs_found == []

    def test_eslint_detected(self):
        repo = _make_repo(files=[
            _make_file(".eslintrc.json", content="{}", language="JSON"),
        ])
        result = scan_repo(repo)
        assert result.has_linter_config is True
        assert ".eslintrc.json" in result.linter_configs_found

    def test_pyproject_detected(self):
        repo = _make_repo(files=[
            _make_file("pyproject.toml", content="[tool.ruff]", language="TOML"),
        ])
        result = scan_repo(repo)
        assert result.has_linter_config is True


class TestErrorHandling:
    def test_strategic(self):
        code = """
try:
    open("file.txt")
except FileNotFoundError:
    print("not found")
except PermissionError:
    print("no access")
"""
        repo = _make_repo(files=[_make_file(content=code)])
        result = scan_repo(repo)
        assert result.error_handling == "strategic"

    def test_blanket(self):
        code = """
try:
    do_something()
except:
    pass

try:
    do_other()
except Exception:
    pass
"""
        repo = _make_repo(files=[_make_file(content=code)])
        result = scan_repo(repo)
        assert result.error_handling == "blanket"

    def test_minimal(self):
        repo = _make_repo(files=[_make_file(content="x = 1\ny = 2\n")])
        result = scan_repo(repo)
        assert result.error_handling == "minimal"


class TestSecurityScan:
    def test_no_secrets(self):
        repo = _make_repo(files=[_make_file(content="x = 1")])
        result = scan_repo(repo)
        assert result.security_flags == []

    def test_detects_api_key(self):
        code = 'API_KEY = "sk-ant-abc123def456ghi789jkl012mno345pqr678"'
        repo = _make_repo(files=[_make_file(content=code)])
        result = scan_repo(repo)
        assert len(result.security_flags) > 0

    def test_ignores_test_files(self):
        code = 'API_KEY = "sk-ant-abc123def456ghi789jkl012mno345pqr678"'
        repo = _make_repo(files=[_make_file(content=code, is_test=True)])
        result = scan_repo(repo)
        assert result.security_flags == []
