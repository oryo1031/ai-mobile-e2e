"""生成物の機械検証。

Python には TypeScript の tsc に相当する層が無いため、AI 生成コードの嘘を
ここで弾く。スキーマ検証・AST 検証・ruff・pytest のコレクションを重ねて、
「存在しない画面やメソッドを呼ぶテスト」が実行前に落ちるようにする。
"""

from __future__ import annotations

import ast
import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import jsonschema
import yaml


@dataclass
class ValidationResult:
    ok: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def merge(self, other: ValidationResult) -> ValidationResult:
        return ValidationResult(
            ok=self.ok and other.ok,
            errors=[*self.errors, *other.errors],
            warnings=[*self.warnings, *other.warnings],
        )


def _load_structured(path: Path) -> Any:
    text = path.read_text(encoding="utf-8")
    if path.suffix in (".yaml", ".yml"):
        return yaml.safe_load(text)
    return json.loads(text)


def validate_schema(document: Path, schema_path: Path) -> ValidationResult:
    """YAML/JSON の成果物を JSON Schema で検証する。"""
    if not document.is_file():
        return ValidationResult(ok=False, errors=[f"成果物がありません: {document}"])
    try:
        data = _load_structured(document)
    except (yaml.YAMLError, json.JSONDecodeError) as exc:
        return ValidationResult(ok=False, errors=[f"{document.name} が壊れています: {exc}"])

    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    validator = jsonschema.Draft202012Validator(schema)
    errors = []
    for error in sorted(validator.iter_errors(data), key=lambda e: list(e.path)):
        location = "/".join(str(p) for p in error.path) or "(root)"
        errors.append(f"{document.name}: {location}: {error.message}")
    return ValidationResult(ok=not errors, errors=errors)


def validate_no_open_questions(spec_path: Path) -> ValidationResult:
    """仕様に未解決の疑問が残っていないか。

    spec-analyst は設計書から読み取れない点を推測で埋めず open_questions に
    列挙する。ここが空でない限り、人が設計書を補うまで先に進ませない。
    """
    data = _load_structured(spec_path)
    questions = (data or {}).get("open_questions") or []
    if not questions:
        return ValidationResult(ok=True)
    return ValidationResult(
        ok=False,
        errors=[
            "設計書に曖昧な点が残っています。設計書を補ってから再実行してください:",
            *[f"  - {q}" for q in questions],
        ],
    )


class _PageUsageVisitor(ast.NodeVisitor):
    """テストコード中の Page Object の使われ方を集める。"""

    def __init__(self) -> None:
        self.imported: set[str] = set()
        # 変数名 -> Page クラス名
        self.instances: dict[str, str] = {}
        self.calls: list[tuple[str, str, int]] = []

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:  # noqa: N802
        if node.module and "pages" in node.module:
            for alias in node.names:
                self.imported.add(alias.asname or alias.name)
        self.generic_visit(node)

    def visit_Assign(self, node: ast.Assign) -> None:  # noqa: N802
        if isinstance(node.value, ast.Call) and isinstance(node.value.func, ast.Name):
            class_name = node.value.func.id
            if class_name.endswith("Page"):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        self.instances[target.id] = class_name
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:  # noqa: N802
        func = node.func
        if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
            variable = func.value.id
            if variable in self.instances:
                self.calls.append((self.instances[variable], func.attr, node.lineno))
        self.generic_visit(node)


def validate_test_code(
    test_dir: Path, api: dict[str, set[str]]
) -> ValidationResult:
    """生成テストが実在する Page Object とメソッドだけを使っているか検証する。"""
    if not test_dir.is_dir():
        return ValidationResult(ok=False, errors=[f"テストディレクトリがありません: {test_dir}"])

    errors: list[str] = []
    warnings: list[str] = []
    files = sorted(test_dir.rglob("test_*.py"))
    if not files:
        return ValidationResult(ok=False, errors=[f"テストファイルがありません: {test_dir}"])

    for file in files:
        try:
            tree = ast.parse(file.read_text(encoding="utf-8"), filename=str(file))
        except SyntaxError as exc:
            errors.append(f"{file.name}:{exc.lineno}: 構文エラー: {exc.msg}")
            continue

        visitor = _PageUsageVisitor()
        visitor.visit(tree)

        for name in visitor.imported:
            if name.endswith("Page") and name != "BasePage" and name not in api:
                errors.append(
                    f"{file.name}: 存在しない Page Object を import しています: {name}"
                )

        for class_name, method, lineno in visitor.calls:
            known = api.get(class_name)
            if known is None:
                errors.append(
                    f"{file.name}:{lineno}: 存在しない Page Object: {class_name}"
                )
            elif method not in known:
                errors.append(
                    f"{file.name}:{lineno}: {class_name} に "
                    f"{method}() はありません"
                )

        if not visitor.instances:
            warnings.append(
                f"{file.name}: Page Object を経由していません。"
                "ロケータを直接扱っていないか確認してください。"
            )

    return ValidationResult(ok=not errors, errors=errors, warnings=warnings)


def run_ruff(root: Path, target: Path) -> ValidationResult:
    proc = subprocess.run(  # noqa: S603
        ["uv", "run", "ruff", "check", str(target)],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode == 0:
        return ValidationResult(ok=True)
    return ValidationResult(ok=False, errors=[proc.stdout.strip() or proc.stderr.strip()])


def run_pytest_collect(root: Path, target: Path) -> ValidationResult:
    """import エラーや収集エラーを実行前に検出する。"""
    proc = subprocess.run(  # noqa: S603
        ["uv", "run", "pytest", "--collect-only", "-q", str(target)],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode == 0:
        return ValidationResult(ok=True)
    return ValidationResult(
        ok=False, errors=[proc.stdout.strip() or proc.stderr.strip()]
    )
