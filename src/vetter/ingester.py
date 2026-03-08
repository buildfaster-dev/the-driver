import os
import click
from git import Repo, InvalidGitRepositoryError, NoSuchPathError
from vetter.models import RepoData, FileInfo, CommitInfo


BINARY_EXTENSIONS = {
    ".png", ".jpg", ".jpeg", ".gif", ".ico", ".svg", ".bmp", ".webp",
    ".woff", ".woff2", ".ttf", ".eot", ".otf",
    ".pdf", ".zip", ".tar", ".gz", ".rar",
    ".exe", ".dll", ".so", ".dylib",
    ".pyc", ".pyo", ".class",
    ".db", ".sqlite", ".sqlite3",
    ".mp3", ".mp4", ".avi", ".mov",
}

SKIP_DIRS = {
    ".git", "node_modules", "__pycache__", ".venv", "venv",
    "dist", "build", ".next", ".nuxt", "vendor",
    ".idea", ".vscode", "coverage", ".tox", "egg-info",
}

LANGUAGE_MAP = {
    ".py": "Python",
    ".js": "JavaScript",
    ".ts": "TypeScript",
    ".jsx": "React JSX",
    ".tsx": "React TSX",
    ".java": "Java",
    ".go": "Go",
    ".rs": "Rust",
    ".rb": "Ruby",
    ".php": "PHP",
    ".cs": "C#",
    ".cpp": "C++",
    ".c": "C",
    ".swift": "Swift",
    ".kt": "Kotlin",
    ".scala": "Scala",
    ".html": "HTML",
    ".css": "CSS",
    ".scss": "SCSS",
    ".sql": "SQL",
    ".sh": "Shell",
    ".yml": "YAML",
    ".yaml": "YAML",
    ".json": "JSON",
    ".toml": "TOML",
    ".md": "Markdown",
}

TEST_PATTERNS = [
    "test_", "_test.", ".test.", ".spec.",
    "/tests/", "/test/", "/__tests__/",
]

MAX_FILE_SIZE = 100 * 1024  # 100KB
MAX_COMMITS = 500


def _detect_language(path: str) -> str:
    ext = os.path.splitext(path)[1].lower()
    return LANGUAGE_MAP.get(ext, "Other")


def _is_test_file(path: str) -> bool:
    path_lower = path.lower()
    return any(pattern in path_lower for pattern in TEST_PATTERNS)


def _should_skip(path: str) -> bool:
    parts = path.split(os.sep)
    return any(part in SKIP_DIRS for part in parts)


def _is_binary(path: str) -> bool:
    ext = os.path.splitext(path)[1].lower()
    return ext in BINARY_EXTENSIONS


def ingest_repo(repo_path: str) -> RepoData:
    try:
        repo = Repo(repo_path)
    except InvalidGitRepositoryError:
        raise click.ClickException(f"Not a Git repository: {repo_path}")
    except NoSuchPathError:
        raise click.ClickException(f"Path does not exist: {repo_path}")

    files: list[FileInfo] = []
    languages: dict[str, int] = {}
    total_lines = 0

    for root, dirs, filenames in os.walk(repo_path):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]

        for filename in filenames:
            full_path = os.path.join(root, filename)
            rel_path = os.path.relpath(full_path, repo_path)

            if _should_skip(rel_path) or _is_binary(rel_path):
                continue

            size = os.path.getsize(full_path)
            if size > MAX_FILE_SIZE:
                continue

            try:
                with open(full_path, "r", encoding="utf-8", errors="ignore") as f:
                    content = f.read()
            except (OSError, UnicodeDecodeError):
                continue

            language = _detect_language(rel_path)
            is_test = _is_test_file(rel_path)
            line_count = content.count("\n") + 1

            languages[language] = languages.get(language, 0) + 1
            total_lines += line_count

            files.append(FileInfo(
                path=rel_path,
                content=content,
                language=language,
                size=size,
                is_test=is_test,
            ))

    commits: list[CommitInfo] = []
    for commit in repo.iter_commits(max_count=MAX_COMMITS):
        stats = commit.stats.total
        commits.append(CommitInfo(
            hash=commit.hexsha[:8],
            message=commit.message.strip(),
            author=str(commit.author),
            date=commit.committed_datetime,
            files_changed=stats.get("files", 0),
            insertions=stats.get("insertions", 0),
            deletions=stats.get("deletions", 0),
        ))

    return RepoData(
        path=repo_path,
        files=files,
        commits=commits,
        languages=languages,
        total_files=len(files),
        total_lines=total_lines,
    )
