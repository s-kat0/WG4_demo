"""Fail when known runtime/secret paths or key-shaped values are tracked."""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

FORBIDDEN_PATHS = [
    re.compile(r"(^|/)\.env($|\.)"),
    re.compile(r"(^|/)secrets(?:\.[^.]+)?\.toml$"),
    re.compile(r"(^|/)runtime/"),
    re.compile(r"\.(?:sqlite3?|db)(?:-(?:wal|shm))?$"),
    re.compile(r"(^|/)(?:logs|exports|recordings)/"),
    re.compile(r"\.(?:pem|key)$"),
]
ALLOWED_PATHS = {".env.example", ".streamlit/secrets.example.toml"}
SECRET_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9_-]{20,}"),
    re.compile(r"\$argon2id\$v=19\$m=\d+,t=\d+,p=\d+\$[A-Za-z0-9+/]{16,}"),
]


def tracked_files(root: Path) -> list[str]:
    git_executable = shutil.which("git")
    if git_executable is None:
        raise RuntimeError("git executable not found")
    result = subprocess.run(  # noqa: S603
        [git_executable, "ls-files", "-z"],
        cwd=root,
        check=True,
        capture_output=True,
    )
    return [entry.decode() for entry in result.stdout.split(b"\0") if entry]


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    violations: list[str] = []
    for relative in tracked_files(root):
        if relative not in ALLOWED_PATHS and any(
            pattern.search(relative) for pattern in FORBIDDEN_PATHS
        ):
            violations.append(f"forbidden tracked path: {relative}")
            continue
        path = root / relative
        if not path.is_file() or path.stat().st_size > 2_000_000:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        if relative in ALLOWED_PATHS:
            continue
        for pattern in SECRET_PATTERNS:
            if pattern.search(text):
                violations.append(f"secret-shaped value in: {relative}")
                break
    if violations:
        print("Repository safety check failed:")
        for violation in violations:
            print(f"- {violation}")
        return 1
    print(f"Repository safety check passed ({len(tracked_files(root))} tracked files).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
