import re
from the_driver.models import RepoData, ScanResult


LINTER_CONFIGS = [
    ".eslintrc", ".eslintrc.js", ".eslintrc.json", ".eslintrc.yml",
    "eslint.config.js", "eslint.config.mjs",
    ".prettierrc", ".prettierrc.json", ".prettierrc.yml",
    "prettier.config.js",
    ".pylintrc", "pylintrc", "setup.cfg", "pyproject.toml",
    ".flake8", ".ruff.toml", "ruff.toml",
    ".rubocop.yml",
    ".stylelintrc", ".stylelintrc.json",
    "biome.json", "biome.jsonc",
    ".editorconfig",
    "tslint.json",
    ".golangci.yml", ".golangci.yaml",
]

SECRET_PATTERNS = [
    re.compile(r'(?:api[_-]?key|apikey)\s*[=:]\s*["\']?[A-Za-z0-9]{16,}', re.IGNORECASE),
    re.compile(r'(?:secret|password|passwd|pwd)\s*[=:]\s*["\']?[^\s"\']{8,}', re.IGNORECASE),
    re.compile(r'(?:token)\s*[=:]\s*["\']?[A-Za-z0-9_\-]{16,}', re.IGNORECASE),
    re.compile(r'(?:aws_access_key_id|aws_secret_access_key)\s*[=:]\s*["\']?[A-Za-z0-9/+=]{16,}', re.IGNORECASE),
    re.compile(r'-----BEGIN (?:RSA |EC )?PRIVATE KEY-----'),
    re.compile(r'sk-[A-Za-z0-9]{32,}'),  # OpenAI keys
    re.compile(r'sk-ant-[A-Za-z0-9\-]{32,}'),  # Anthropic keys
    re.compile(r'ghp_[A-Za-z0-9]{36,}'),  # GitHub tokens
]

BLANKET_TRY_CATCH = [
    re.compile(r'except\s*:', re.MULTILINE),  # Python bare except
    re.compile(r'except\s+Exception\s*:', re.MULTILINE),  # Python catch-all
    re.compile(r'catch\s*\(\s*(?:e|err|error|ex|exception)?\s*\)\s*\{', re.MULTILINE),  # JS/TS catch-all
    re.compile(r'catch\s*\(\s*Exception\s+', re.MULTILINE),  # Java catch-all
]

STRATEGIC_ERROR_PATTERNS = [
    re.compile(r'except\s+\w+Error', re.MULTILINE),  # Python specific exceptions
    re.compile(r'except\s+\([\w,\s]+\)', re.MULTILINE),  # Python multiple specific
    re.compile(r'catch\s*\(\s*\w+Error', re.MULTILINE),  # JS specific errors
    re.compile(r'catch\s*\(\s*(?:IOException|SQLException|IllegalArgument)', re.MULTILINE),  # Java specific
]

DEPENDENCY_FILES = {
    "requirements.txt": "pip",
    "Pipfile": "pipenv",
    "pyproject.toml": "uv/pip",
    "package.json": "npm",
    "Gemfile": "bundler",
    "go.mod": "go",
    "Cargo.toml": "cargo",
    "pom.xml": "maven",
    "build.gradle": "gradle",
    "composer.json": "composer",
}


def _calc_test_ratio(repo_data: RepoData) -> float:
    source_files = [f for f in repo_data.files if not f.is_test and f.language not in ("Markdown", "JSON", "YAML", "TOML")]
    test_files = [f for f in repo_data.files if f.is_test]
    if not source_files:
        return 0.0
    return len(test_files) / len(source_files)


def _detect_linter_configs(repo_data: RepoData) -> list[str]:
    found = []
    file_paths = {f.path for f in repo_data.files}
    for config in LINTER_CONFIGS:
        if config in file_paths:
            found.append(config)
    return found


def _analyze_commit_quality(repo_data: RepoData) -> str:
    if not repo_data.commits:
        return "poor"

    messages = [c.message.split("\n")[0] for c in repo_data.commits]
    good_count = 0
    for msg in messages:
        msg_lower = msg.lower().strip()
        if len(msg) > 10 and msg_lower not in ("initial commit", "first commit", "init", "wip", "update", "fix"):
            good_count += 1

    ratio = good_count / len(messages) if messages else 0
    if ratio >= 0.7:
        return "good"
    elif ratio >= 0.4:
        return "fair"
    return "poor"


def _detect_dependencies(repo_data: RepoData) -> list[str]:
    deps = []
    for file_info in repo_data.files:
        if file_info.path in DEPENDENCY_FILES:
            deps.append(f"{DEPENDENCY_FILES[file_info.path]}: {file_info.path}")
    return deps


def _detect_error_handling(repo_data: RepoData) -> str:
    blanket_count = 0
    strategic_count = 0

    for file_info in repo_data.files:
        if file_info.is_test or file_info.language in ("Markdown", "JSON", "YAML", "TOML"):
            continue
        for pattern in BLANKET_TRY_CATCH:
            blanket_count += len(pattern.findall(file_info.content))
        for pattern in STRATEGIC_ERROR_PATTERNS:
            strategic_count += len(pattern.findall(file_info.content))

    total = blanket_count + strategic_count
    if total == 0:
        return "minimal"
    if strategic_count > blanket_count:
        return "strategic"
    return "blanket"


def _scan_security(repo_data: RepoData) -> list[str]:
    flags = []
    for file_info in repo_data.files:
        if file_info.is_test:
            continue
        for pattern in SECRET_PATTERNS:
            matches = pattern.findall(file_info.content)
            if matches:
                flags.append(f"{file_info.path}: potential hardcoded secret detected")
                break
    return flags


def scan_repo(repo_data: RepoData) -> ScanResult:
    linter_configs = _detect_linter_configs(repo_data)

    return ScanResult(
        test_ratio=_calc_test_ratio(repo_data),
        has_linter_config=len(linter_configs) > 0,
        linter_configs_found=linter_configs,
        commit_count=len(repo_data.commits),
        commit_quality=_analyze_commit_quality(repo_data),
        commit_messages=[c.message.split("\n")[0] for c in repo_data.commits],
        dependencies=_detect_dependencies(repo_data),
        error_handling=_detect_error_handling(repo_data),
        security_flags=_scan_security(repo_data),
        languages=repo_data.languages,
    )
