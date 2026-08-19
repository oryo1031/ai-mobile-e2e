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


def collect_open_questions(spec_path: Path) -> list[str]:
    """仕様に残った未解決の疑問を集める。

    spec-analyst は設計書から読み取れない点を推測で埋めず open_questions に
    列挙する。ただしこれは**工程を止めない**。設計書に情報が無いことは
    AI の再実行では解消せず、止めると自動化の意味が薄れるため。

    代わりに、この一覧は工程 3(試験項目の人によるレビュー)で提示される。
    人はそこで、疑わしい試験項目を重点的に見ればよい。
    """
    data = _load_structured(spec_path)
    questions = (data or {}).get("open_questions") or []
    return [str(q) for q in questions]


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


def validate_setup_coverage(
    testcases_path: Path, setup_dir: Path
) -> ValidationResult:
    """前提条件のセットアップが実装されているか確認する。

    試験項目の `preconditions[].id` それぞれに対し、`tests/setup/` に
    `setup_<id>` が実装されている必要がある。

    これが無いと、テストは「その画面にたどり着けない」まま実行され、
    **一番遅い実行時に初めて破綻する**。identifier があっても画面に
    到達する手段は別に要るため、ここで実行前に落とす。
    """
    if not testcases_path.is_file():
        return ValidationResult(ok=False, errors=[f"試験項目がありません: {testcases_path}"])

    try:
        data = yaml.safe_load(testcases_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        return ValidationResult(ok=False, errors=[f"試験項目が壊れています: {exc}"])

    # 参照されている前提条件 ID を集める。
    required: dict[str, str] = {}
    for case in (data or {}).get("testcases", []):
        for pre in case.get("preconditions") or []:
            if isinstance(pre, dict) and pre.get("id"):
                required[str(pre["id"])] = str(pre.get("description", ""))

    if not required:
        return ValidationResult(ok=True)

    # 実装されている setup_<id> を集める。
    implemented: set[str] = set()
    if setup_dir.is_dir():
        for file in setup_dir.rglob("*.py"):
            try:
                tree = ast.parse(file.read_text(encoding="utf-8"), filename=str(file))
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.FunctionDef) and node.name.startswith("setup_"):
                    implemented.add(node.name[len("setup_") :])

    missing = sorted(set(required) - implemented)
    if not missing:
        return ValidationResult(ok=True)

    errors = [
        f"前提条件のセットアップが {len(missing)} 件ありません。"
        " テストがその画面にたどり着けず、実行時に失敗します。"
    ]
    for identifier in missing:
        errors.append(
            f"  - '{identifier}' ({required[identifier]}):"
            f" {setup_dir.name}/ に setup_{identifier} が必要です"
        )
    return ValidationResult(ok=False, errors=errors)


def validate_deeplink_urls(
    testcases_path: Path, deeplinks_path: Path
) -> ValidationResult:
    """ディープリンクの指定が解決できるか確認する。

    `value` は次のどちらかとして扱う。

    - `://` を含む … URL の直書き。形式だけ見る(その場限りの確認用)
    - 含まない     … id とみなし、testdata/deeplinks.yaml に存在するか見る

    実行時に「開かない」形で失敗すると、URL が違うのかアプリの不具合なのかを
    切り分けられなくなる。生成の段階で落とす。
    """
    if not testcases_path.is_file():
        return ValidationResult(ok=True)
    try:
        data = yaml.safe_load(testcases_path.read_text(encoding="utf-8"))
    except yaml.YAMLError:
        return ValidationResult(ok=True)

    known: dict[str, str] = {}
    if deeplinks_path.is_file():
        try:
            raw = yaml.safe_load(deeplinks_path.read_text(encoding="utf-8")) or {}
            known = {
                str(e["id"]): str(e["url"])
                for e in raw.get("deeplinks") or []
                if e.get("id") and e.get("url")
            }
        except yaml.YAMLError:
            known = {}

    placeholders = ("<", ">", "xxx", "TODO", "...")
    errors: list[str] = []
    for case in (data or {}).get("testcases", []):
        for index, step in enumerate(case.get("steps") or [], start=1):
            if step.get("action") != "open_deeplink":
                continue
            where = f"{case.get('id', '?')} の {index} 番目"
            value = str(step.get("value") or "").strip()

            if not value:
                errors.append(f"{where}: ディープリンクの指定が空です")
            elif "://" in value:
                if any(mark in value for mark in placeholders):
                    errors.append(f"{where}: URL が埋まっていません: {value!r}")
            elif value not in known:
                names = ", ".join(sorted(known)) or "(未定義)"
                errors.append(
                    f"{where}: ディープリンク '{value}' が"
                    f" {deeplinks_path.name} にありません。定義済み: {names}"
                )

    if not errors:
        return ValidationResult(ok=True)
    return ValidationResult(
        ok=False,
        errors=[
            "ディープリンクの指定に問題があります。"
            f" URL は {deeplinks_path} に書き、試験項目は id で参照します。",
            *errors,
        ],
    )
