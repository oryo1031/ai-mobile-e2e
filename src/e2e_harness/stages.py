"""ワークフローの工程定義。

各工程は「AI に投げるタスクプロンプト」と「出口の検証ゲート」の組で表される。
AI を使わない工程(テスト実行)もあり、その場合は execute が入る。

工程の順序と依存関係をここ 1 か所に集約しているため、工程の追加や
入れ替えはこのファイルだけを変えればよい。
"""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import yaml

from .config import Config
from .registry import diff as registry_diff
from .registry import load as load_registry
from .semantics import scan_directory
from .validation import (
    ValidationResult,
    collect_open_questions,
    run_pytest_collect,
    run_ruff,
    validate_deeplink_urls,
    validate_schema,
    validate_setup_coverage,
    validate_test_code,
)


@dataclass
class StageContext:
    config: Config
    run_id: str
    platform: str
    #: 人が書いた設計書のパス(リポジトリルートからの相対)。
    spec_document: str = ""

    @property
    def run_dir(self) -> Path:
        return self.config.artifacts_dir / self.run_id

    @property
    def evidence_dir(self) -> Path:
        return self.run_dir / "evidence"

    @property
    def spec_path(self) -> Path:
        return self.run_dir / "spec.yaml"

    @property
    def testcases_path(self) -> Path:
        return self.run_dir / "testcases.yaml"

    @property
    def analysis_path(self) -> Path:
        return self.run_dir / "analysis.yaml"

    @property
    def scan_report_path(self) -> Path:
        return self.run_dir / "app_scan.md"

    @property
    def locator_proposal_path(self) -> Path:
        return self.run_dir / "locator_proposal.md"

    @property
    def junit_path(self) -> Path:
        return self.run_dir / "junit.xml"

    @property
    def analyze_baseline_path(self) -> Path:
        """ロケータ整備の前に取っておく flutter analyze の基準。"""
        return self.run_dir / "flutter_analyze_baseline.txt"

    def relative(self, path: Path) -> str:
        try:
            return str(path.relative_to(self.config.root))
        except ValueError:
            return str(path)


@dataclass
class Stage:
    name: str
    title: str
    #: Copilot のエージェント名。None なら AI を使わない工程。
    agent: str | None = None
    #: prompts/ 配下のファイル名。
    prompt_name: str | None = None
    #: 人の判断で止まる工程。
    human_review: bool = False
    #: 人に見せる案内を実行時に組み立てる。notes より優先する。
    build_notes: Callable[[StageContext], str] | None = None
    #: AI に渡すタスクプロンプトを組み立てる。
    build_prompt: Callable[[StageContext], str] | None = None
    #: 出口の検証ゲート。
    gate: Callable[[StageContext], ValidationResult] | None = None
    #: AI を使わない工程の実処理。
    execute: Callable[[StageContext], ValidationResult] | None = None
    #: この工程で AI に書かせてよいツール。
    allow_tools: str = "read,write"
    notes: str = ""


# ----------------------------------------------------------------------
# タスクプロンプト
# ----------------------------------------------------------------------
def _spec_prompt(ctx: StageContext) -> str:
    return (
        "設計書を読み込み、正規化仕様 YAML を作成してください。\n\n"
        f"- 設計書: {ctx.spec_document}\n"
        f"- 出力先: {ctx.relative(ctx.spec_path)}\n"
        f"- スキーマ: schemas/spec.schema.json\n"
        f"- アプリのソース(参照のみ): {ctx.config.app_lib_dir}\n\n"
        "設計書から読み取れない点は推測で埋めず、open_questions に"
        "「設計書に何を追記してほしいか」の形で列挙してください。"
    )


def _testcases_prompt(ctx: StageContext) -> str:
    # 参照できる id の一覧を渡さないと、定義済みでも使われない。
    return (
        "正規化仕様から試験項目 YAML を設計してください。\n\n"
        f"- 入力: {ctx.relative(ctx.spec_path)}\n"
        f"- 出力先: {ctx.relative(ctx.testcases_path)}\n"
        f"- スキーマ: schemas/testcases.schema.json\n"
        f"- 使えるテストアカウント: {ctx.relative(ctx.config.accounts_path)}\n"
        f"- 使えるディープリンク: {ctx.relative(ctx.config.deeplinks_path)}\n\n"
        "正常系・異常系・境界値の 3 観点を意識的に埋めてください。"
        "アカウントとディープリンクは、上記のファイルに定義されている id"
        "だけを参照してください。必要なものが未定義なら、その試験項目は"
        "作らずに assumptions へ理由を残してください。"
        "この出力は人がレビューします。"
    )


def _locators_prompt(ctx: StageContext) -> str:
    return (
        "試験項目が参照する要素をロケータレジストリに整備してください。\n\n"
        f"- 試験項目: {ctx.relative(ctx.testcases_path)}\n"
        f"- アプリ走査結果: {ctx.relative(ctx.scan_report_path)}\n"
        f"- 更新対象: {ctx.relative(ctx.config.locators_path)}\n"
        f"- スキーマ: schemas/locators.schema.json\n"
        f"- アプリのソース(編集可): {ctx.config.app_lib_dir}\n"
        f"- 変更記録の出力先: {ctx.relative(ctx.locator_proposal_path)}\n\n"
        "アプリに identifier が足りない場合は、"
        "Semantics(container: true, identifier: ...) でラップして追加してください。"
        "追加してよいのはこのラップだけで、既存のロジックや整形には触れないこと。"
        "レジストリには、走査で実在が確認できた identifier だけを登録してください。"
    )


def _codegen_prompt(ctx: StageContext) -> str:
    return (
        "試験項目と Page Object から pytest のテストコードを生成してください。\n\n"
        f"- 試験項目: {ctx.relative(ctx.testcases_path)}\n"
        f"- 利用できる Page Object: {ctx.relative(ctx.config.generated_pages_dir)}\n"
        f"- 出力先ディレクトリ: {ctx.relative(ctx.config.generated_tests_dir)}\n"
        f"- fixture の定義: tests/conftest.py\n\n"
        "Page Object に実在するメソッドだけを使ってください。"
        "ロケータ文字列と証跡取得の処理をテストコードに書いてはいけません。"
    )


def _analysis_prompt(ctx: StageContext) -> str:
    return (
        "実行結果と証跡から失敗を分類し、分析レポートを作成してください。\n\n"
        f"- 実行結果(JUnit XML): {ctx.relative(ctx.junit_path)}\n"
        f"- 証跡ディレクトリ: {ctx.relative(ctx.evidence_dir)}\n"
        f"- 試験項目: {ctx.relative(ctx.testcases_path)}\n"
        f"- 出力先: {ctx.relative(ctx.analysis_path)}\n"
        f"- スキーマ: schemas/analysis.schema.json\n\n"
        "product_bug / flaky / test_defect / locator_defect の 4 分類に"
        "振り分け、判断の根拠にした証跡ファイルを evidence に挙げてください。"
    )


# ----------------------------------------------------------------------
# 検証ゲート
# ----------------------------------------------------------------------
def _gate_spec(ctx: StageContext) -> ValidationResult:
    """仕様の正規化の出口。

    open_questions は**止めない**。設計書に情報が無いことは AI の再実行では
    解消せず、ここで止めると人が設計書を直すまで自動化が進まなくなるため。
    警告として出したうえで先へ進め、工程 3 の人のレビューで拾う。
    """
    result = validate_schema(ctx.spec_path, ctx.config.root / "schemas/spec.schema.json")
    if not result.ok:
        return result
    questions = collect_open_questions(ctx.spec_path)
    if questions:
        result.warnings.append(
            f"設計書から読み取れなかった点が {len(questions)} 件あります。"
            "以降の工程は仮定を置いて進むため、試験項目のレビューで確認してください。"
        )
        result.warnings.extend(f"  - {q}" for q in questions)
    return result


def _review_notes(ctx: StageContext) -> str:
    """試験項目レビューの案内。

    設計書から読み取れなかった点と、それに対して AI が置いた仮定を並べる。
    どこを重点的に見ればよいかが分からないとレビューが形骸化するため、
    ここで「疑わしい箇所」を名指しする。
    """
    lines = [
        "試験項目を読み、観点の抜けや期待結果の誤りが無いか確認してください。",
    ]

    questions = collect_open_questions(ctx.spec_path)
    if questions:
        lines.append("")
        lines.append(
            f"設計書から読み取れなかった点が {len(questions)} 件あります。"
            "これらは仮定を置いて進めています:"
        )
        lines.extend(f"  - {q}" for q in questions)

    assumptions = _collect_assumptions(ctx.testcases_path)
    if assumptions:
        lines.append("")
        lines.append("AI が仮定を置いた試験項目(重点的に確認してください):")
        lines.extend(f"  - {a}" for a in assumptions)

    lines.append("")
    lines.append(f"試験項目: {ctx.relative(ctx.testcases_path)}")
    lines.append("問題なければ `e2e approve review` で次へ進みます。")
    return "\n    ".join(lines)


def _collect_assumptions(testcases_path: Path) -> list[str]:
    """試験項目に記録された仮定を集める。"""
    if not testcases_path.is_file():
        return []
    try:
        data = yaml.safe_load(testcases_path.read_text(encoding="utf-8"))
    except yaml.YAMLError:
        return []
    found: list[str] = []
    for case in (data or {}).get("testcases", []):
        for assumption in case.get("assumptions") or []:
            found.append(f"{case.get('id', '?')}: {assumption}")
    return found


def _gate_testcases(ctx: StageContext) -> ValidationResult:
    return validate_schema(
        ctx.testcases_path, ctx.config.root / "schemas/testcases.schema.json"
    )


def _gate_locators(ctx: StageContext) -> ValidationResult:
    """レジストリがアプリの実体とずれていないかを突き合わせる。

    レジストリの嘘はそのままテストコードに流れ込むため、ここが
    ハルシネーション対策の最重要ゲートになる。
    """
    result = validate_schema(
        ctx.config.locators_path, ctx.config.root / "schemas/locators.schema.json"
    )
    if not result.ok:
        return result

    registry = load_registry(ctx.config.locators_path)
    if not registry.screens:
        return ValidationResult(
            ok=False,
            errors=[
                "ロケータレジストリが空です。試験項目が参照する要素を"
                " アプリに追加して登録する必要があります。",
                f"  アプリ走査結果: {ctx.relative(ctx.scan_report_path)}",
                f"  変更記録:       {ctx.relative(ctx.locator_proposal_path)}",
                "  アプリに Semantics(container: true, identifier: ...) が"
                " 1 つも無い場合、この工程で追加します。",
            ],
        )

    usages = scan_directory(ctx.config.app_lib_dir)
    delta = registry_diff(registry, usages)

    errors: list[str] = []
    warnings: list[str] = []
    for identifier in delta.missing_in_app:
        errors.append(
            f"レジストリの identifier '{identifier}' がアプリのソースに存在しません。"
        )
    for usage in delta.unsafe:
        errors.append(
            f"{usage.file.name}:{usage.line}: identifier '{usage.identifier}' に "
            "container: true がありません。マージで identifier が消えます。"
        )
    for identifier in delta.unregistered:
        warnings.append(
            f"アプリの identifier '{identifier}' はレジストリに未登録です。"
        )
    if errors:
        return ValidationResult(ok=False, errors=errors, warnings=warnings)

    # この工程はアプリのソースを書き換える。壊していないことを確認する。
    return ValidationResult(ok=True, warnings=warnings).merge(
        _run_flutter_analyze(ctx)
    )


def analyze_errors(app_root: Path) -> list[str] | None:
    """flutter analyze のエラー行を集める。

    実行できなかった場合は None を返す(flutter が無い環境など)。
    """
    if shutil.which("flutter") is None:
        return None
    try:
        proc = subprocess.run(  # noqa: S603
            ["flutter", "analyze", "--no-pub"],
            cwd=app_root,
            capture_output=True,
            text=True,
            timeout=600,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    # 「error • ...」の行だけを見る。info や warning は元から出ていることが多い。
    return sorted(
        line.strip()
        for line in (proc.stdout or "").splitlines()
        if line.strip().startswith("error •")
    )


def _run_flutter_analyze(ctx: StageContext) -> ValidationResult:
    """アプリのソースを壊していないか確認する。

    ロケータ整備は AI がアプリのソースを編集する唯一の工程なので、
    構文や型を壊していないことをここで機械的に確かめる。

    ただし**実アプリは元から analyze を通っていないことがある**ため、
    絶対の合否では見ない。工程の開始前に取っておいた基準と突き合わせ、
    **新たに増えたエラーだけ**を落とす。そうしないと、無関係な既存の
    エラーでこの工程が永久に進めなくなる。
    """
    current = analyze_errors(ctx.config.app.root)
    if current is None:
        return ValidationResult(
            ok=True,
            warnings=[
                "flutter が見つからないため、アプリの静的解析を省略しました。"
                " アプリ側の差分を目視で確認してください。"
            ],
        )

    baseline: list[str] = []
    if ctx.analyze_baseline_path.is_file():
        baseline = [
            line
            for line in ctx.analyze_baseline_path.read_text(
                encoding="utf-8"
            ).splitlines()
            if line.strip()
        ]

    introduced = [line for line in current if line not in baseline]
    if not introduced:
        warnings = []
        if baseline:
            warnings.append(
                f"アプリには元から {len(baseline)} 件の解析エラーがあります"
                "(この工程が増やしたものではありません)。"
            )
        return ValidationResult(ok=True, warnings=warnings)

    return ValidationResult(
        ok=False,
        errors=[
            f"アプリのソースに解析エラーが {len(introduced)} 件増えました。"
            " 追加した Semantics が構文を壊していないか確認してください。",
            *introduced[:20],
        ],
    )


def _gate_codegen(ctx: StageContext) -> ValidationResult:
    from .pages import public_api

    registry = load_registry(ctx.config.locators_path)
    result = validate_test_code(
        ctx.config.generated_tests_dir,
        public_api(registry, ctx.config.generated_pages_dir),
    )
    if not result.ok:
        return result
    # 前提条件のセットアップが無いと、テストはその画面へ到達できないまま
    # 実行され、一番遅い段階で失敗する。ここで落とす。
    result = result.merge(
        validate_setup_coverage(ctx.testcases_path, ctx.config.setup_dir)
    )
    result = result.merge(
        validate_deeplink_urls(ctx.testcases_path, ctx.config.deeplinks_path)
    )
    if not result.ok:
        return result
    result = result.merge(run_ruff(ctx.config.root, ctx.config.generated_tests_dir))
    if not result.ok:
        return result
    return result.merge(
        run_pytest_collect(ctx.config.root, ctx.config.generated_tests_dir)
    )


def _gate_analysis(ctx: StageContext) -> ValidationResult:
    return validate_schema(
        ctx.analysis_path, ctx.config.root / "schemas/analysis.schema.json"
    )


# ----------------------------------------------------------------------
# AI を使わない工程
# ----------------------------------------------------------------------
def _execute_tests(ctx: StageContext) -> ValidationResult:
    """pytest を実行する。証跡取得は conftest.py が担う。

    テストの失敗自体はこの工程の失敗ではない。失敗の分析は次工程の
    仕事なので、ここでは「実行が成立したか」だけを見る。
    """
    ctx.evidence_dir.mkdir(parents=True, exist_ok=True)
    argv = [
        "uv",
        "run",
        "pytest",
        str(ctx.config.generated_tests_dir),
        f"--platform={ctx.platform}",
        f"--evidence-dir={ctx.evidence_dir}",
        f"--junitxml={ctx.junit_path}",
        "-v",
    ]
    proc = subprocess.run(  # noqa: S603
        argv, cwd=ctx.config.root, capture_output=True, text=True, check=False
    )
    (ctx.run_dir / "pytest_output.txt").write_text(
        proc.stdout + "\n" + proc.stderr, encoding="utf-8"
    )
    # pytest の終了コード: 0=全通過, 1=テスト失敗, それ以外=実行自体の異常
    if proc.returncode in (0, 1):
        warnings = (
            [] if proc.returncode == 0 else ["失敗したテストがあります。次工程で分析します。"]
        )
        return ValidationResult(ok=True, warnings=warnings)
    return ValidationResult(
        ok=False,
        errors=[
            f"pytest の実行に失敗しました (exit={proc.returncode})",
            proc.stdout.strip()[-2000:],
            proc.stderr.strip()[-2000:],
        ],
    )


# ----------------------------------------------------------------------
# 工程一覧
# ----------------------------------------------------------------------
STAGES: list[Stage] = [
    Stage(
        name="spec",
        title="仕様の正規化",
        agent="e2e-spec-analyst",
        prompt_name="spec-analyst",
        build_prompt=_spec_prompt,
        gate=_gate_spec,
    ),
    Stage(
        name="testcases",
        title="試験項目の設計",
        agent="e2e-testcase-designer",
        prompt_name="testcase-designer",
        build_prompt=_testcases_prompt,
        gate=_gate_testcases,
    ),
    Stage(
        name="review",
        title="試験項目の人によるレビュー",
        human_review=True,
        build_notes=_review_notes,
    ),
    Stage(
        name="locators",
        title="ロケータレジストリの整備",
        agent="e2e-locator-curator",
        prompt_name="locator-curator",
        build_prompt=_locators_prompt,
        gate=_gate_locators,
        allow_tools="read,write,shell",
    ),
    Stage(
        name="codegen",
        title="テストコードの生成",
        agent="e2e-test-codegen",
        prompt_name="test-codegen",
        build_prompt=_codegen_prompt,
        gate=_gate_codegen,
    ),
    Stage(
        name="execute",
        title="テストの実行と証跡取得",
        execute=_execute_tests,
    ),
    Stage(
        name="analyze",
        title="実行結果の分析",
        agent="e2e-run-analyst",
        prompt_name="run-analyst",
        build_prompt=_analysis_prompt,
        gate=_gate_analysis,
    ),
    Stage(
        name="confirm",
        title="人による結果確認",
        human_review=True,
        notes=(
            "分析レポートと証跡を確認してください。"
            "確認が済んだら `e2e approve confirm` で完了です。"
        ),
    ),
]

STAGE_BY_NAME: dict[str, Stage] = {s.name: s for s in STAGES}
STAGE_NAMES: list[str] = [s.name for s in STAGES]


def stage_index(name: str) -> int:
    return STAGE_NAMES.index(name)
