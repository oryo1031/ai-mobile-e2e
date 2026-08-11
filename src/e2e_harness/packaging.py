"""配布用アーカイブの作成。

通常の配布はアプリリポジトリに同梱して行うため、この経路は主役ではない。
退避や、リポジトリを介さず一時的に渡したいときの補助として用意してある。

git に依存せず、展開すればそのまま使える形にする。
"""

from __future__ import annotations

import zipfile
from dataclasses import dataclass
from pathlib import Path

#: 配布物に含めないもの。実行時に生成されるものと、端末固有のもの。
EXCLUDED_DIRS = {
    ".git",
    ".venv",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "allure-results",
    "artifacts",
    "node_modules",
}

EXCLUDED_SUFFIXES = {".pyc", ".pyo", ".zip"}


@dataclass
class PackageResult:
    archive: Path
    file_count: int
    size_bytes: int

    @property
    def size_mb(self) -> float:
        return self.size_bytes / (1024 * 1024)


def _should_include(path: Path, root: Path, include_node_modules: bool) -> bool:
    relative = path.relative_to(root)
    for part in relative.parts:
        if part in EXCLUDED_DIRS:
            if part == "node_modules" and include_node_modules:
                continue
            return False
    return path.suffix not in EXCLUDED_SUFFIXES


def create(
    root: Path,
    destination: Path,
    *,
    prefix: str = "ai-mobile-e2e",
    include_node_modules: bool = False,
) -> PackageResult:
    """配布用の zip を作る。

    展開したときに 1 階層のディレクトリになるよう prefix を付ける。
    アプリフォルダ配下へそのまま移動できる形。
    """
    destination.parent.mkdir(parents=True, exist_ok=True)

    files = [
        path
        for path in sorted(root.rglob("*"))
        if path.is_file() and _should_include(path, root, include_node_modules)
    ]

    with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in files:
            archive.write(path, arcname=str(Path(prefix) / path.relative_to(root)))

    return PackageResult(
        archive=destination,
        file_count=len(files),
        size_bytes=destination.stat().st_size,
    )
