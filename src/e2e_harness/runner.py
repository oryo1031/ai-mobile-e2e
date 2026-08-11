"""オーケストレータ本体。

工程を順に進め、各工程の出口で検証ゲートを通し、落ちたらその内容を添えて
担当エージェントに差し戻す。人のレビュー地点では必ず停止する。

Copilot には複数エージェントを制御する API が無いため、進行管理・状態保存・
検証は決定論的にこちら側で持ち、AI 呼び出しだけを Copilot に委ねている。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from . import copilot as copilot_mod
from . import semantics
from . import state as state_mod
from .agents import agent_body
from .config import Config
from .copilot import ExecutionMode
from .registry import diff as registry_diff
from .registry import load as load_registry
from .stages import STAGE_BY_NAME, STAGE_NAMES, Stage, StageContext, stage_index
from .state import RunState, StageStatus
from .validation import ValidationResult

MAX_ATTEMPTS = 3


@dataclass
class StageOutcome:
    stage: str
    status: StageStatus
    message: str = ""
    result: ValidationResult | None = None


class Reporter:
    """進捗の表示。CLI から差し替えられるように分離してある。"""

    def stage_start(self, index: int, total: int, stage: Stage) -> None:
        print(f"\n[{index}/{total}] {stage.title} ({stage.name})")

    def info(self, message: str) -> None:
        print(f"    {message}")

    def success(self, message: str) -> None:
        print(f"  ✓ {message}")

    def failure(self, message: str) -> None:
        print(f"  ✗ {message}")

    def warning(self, message: str) -> None:
        print(f"  ! {message}")


class Runner:
    def __init__(
        self,
        config: Config,
        run_state: RunState,
        *,
        mode: ExecutionMode,
        reporter: Reporter | None = None,
    ) -> None:
        self.config = config
        self.state = run_state
        self.mode = mode
        self.reporter = reporter or Reporter()
        self.context = StageContext(
            config=config,
            run_id=run_state.run_id,
            platform=run_state.platform,
            spec_document=run_state.spec_document,
        )

    # ------------------------------------------------------------------
    def save(self) -> None:
        state_mod.save(self.config.artifacts_dir, self.state)

    def next_stage(self) -> Stage | None:
        for name in STAGE_NAMES:
            status = self.state.stage(name).status
            if status is not StageStatus.COMPLETED:
                return STAGE_BY_NAME[name]
        return None

    # ------------------------------------------------------------------
    def prepare_stage(self, stage: Stage) -> None:
        """工程の実行前に必要な下ごしらえ。

        ロケータ整備の前にはアプリを走査して結果を渡し、テストコード生成の
        前には Page Object を再生成する。AI にこの手順を任せると忘れるため、
        決定論的にこちらで行う。
        """
        if stage.name == "locators":
            self._write_scan_report()
        elif stage.name == "codegen":
            self._regenerate_pages()

    def _write_scan_report(self) -> None:
        usages = semantics.scan_directory(self.config.app_lib_dir)
        registry = load_registry(self.config.locators_path)
        delta = registry_diff(registry, usages)

        lines = [
            "# アプリ走査結果",
            "",
            f"対象: `{self.config.app_lib_dir}`",
            "",
            "## 実在する Semantics(identifier:)",
            "",
            "| identifier | ファイル | 行 | container: true | 動的 |",
            "|---|---|---|---|---|",
        ]
        for usage in sorted(usages, key=lambda u: u.identifier):
            rel = usage.file.name
            safe = "あり" if usage.has_container_true else "**なし**"
            dynamic = "はい" if usage.is_dynamic else ""
            lines.append(
                f"| `{usage.identifier}` | {rel} | {usage.line} | {safe} | {dynamic} |"
            )

        if delta.unsafe:
            lines += [
                "",
                "## 危険な書き方(container: true がない)",
                "",
                "マージにより identifier が失われます。修正提案に含めてください。",
                "",
            ]
            for usage in delta.unsafe:
                lines.append(f"- `{usage.identifier}` — {usage.file}:{usage.line}")

        if delta.unregistered:
            lines += ["", "## レジストリ未登録", ""]
            for identifier in delta.unregistered:
                lines.append(f"- `{identifier}`")

        if delta.missing_in_app:
            lines += ["", "## レジストリにあるがアプリに存在しない", ""]
            for identifier in delta.missing_in_app:
                lines.append(f"- `{identifier}`")

        self.context.run_dir.mkdir(parents=True, exist_ok=True)
        self.context.scan_report_path.write_text(
            "\n".join(lines) + "\n", encoding="utf-8"
        )
        self.reporter.info(
            f"アプリ走査: identifier {len(usages)} 件 / "
            f"危険な書き方 {len(delta.unsafe)} 件"
        )

    def _regenerate_pages(self) -> None:
        from .pages import generate

        registry = load_registry(self.config.locators_path)
        written = generate(
            registry,
            self.config.generated_pages_dir,
            source=str(self.config.locators_path.name),
        )
        self.reporter.info(f"Page Object を再生成: {len(written)} ファイル")

    # ------------------------------------------------------------------
    def invoke_agent(self, stage: Stage, extra: str = "") -> tuple[bool, str]:
        """担当エージェントを呼ぶ。戻り値は (成功したか, メッセージ)。"""
        assert stage.build_prompt is not None
        assert stage.agent is not None
        assert stage.prompt_name is not None

        task = stage.build_prompt(self.context)
        if extra:
            task = f"{task}\n\n## 前回の検証で落ちた内容\n\n{extra}\n\n上記を直してください。"

        if self.mode is ExecutionMode.MANUAL:
            # CLI が使えない環境向け。人がチャットに貼って実行する。
            instructions = agent_body(self.config.root, stage.prompt_name)
            path = copilot_mod.write_manual_prompt(
                run_directory=self.context.run_dir,
                stage_name=stage.name,
                agent=stage.agent,
                prompt=f"{task}\n\n---\n\n{instructions}",
            )
            return False, f"手動実行用のプロンプトを書き出しました: {path}"

        result = copilot_mod.run_cli(
            command=self.config.copilot.command,
            prompt=task,
            agent=stage.agent,
            allow_tools=stage.allow_tools or self.config.copilot.default_allow_tools,
            model=self.config.copilot.model,
            cwd=self.config.root,
            timeout_seconds=self.config.copilot.timeout_seconds,
            add_dirs=[self.config.app.root],
        )
        transcript = self.context.run_dir / f"{stage.name}.copilot.log"
        transcript.write_text(
            (result.stdout or "") + "\n" + (result.stderr or ""), encoding="utf-8"
        )
        if not result.ok:
            return False, f"Copilot の実行が失敗しました (exit={result.returncode})"
        return True, ""

    # ------------------------------------------------------------------
    def run_stage(self, stage: Stage) -> StageOutcome:
        stage_state = self.state.stage(stage.name)

        if stage.human_review:
            stage_state.status = StageStatus.AWAITING_REVIEW
            stage_state.started_at = stage_state.started_at or state_mod.now()
            self.save()
            return StageOutcome(
                stage=stage.name,
                status=StageStatus.AWAITING_REVIEW,
                message=stage.notes,
            )

        stage_state.status = StageStatus.RUNNING
        stage_state.started_at = state_mod.now()
        self.save()

        self.prepare_stage(stage)

        # AI を使わない工程。
        if stage.execute is not None:
            result = stage.execute(self.context)
            for warning in result.warnings:
                self.reporter.warning(warning)
            if result.ok:
                stage_state.status = StageStatus.COMPLETED
                stage_state.finished_at = state_mod.now()
                self.save()
                return StageOutcome(stage.name, StageStatus.COMPLETED, result=result)
            stage_state.status = StageStatus.FAILED
            stage_state.error = "\n".join(result.errors)
            self.save()
            return StageOutcome(stage.name, StageStatus.FAILED, result=result)

        # 手動モードでは、人が Copilot のチャットで工程を実行し終えている
        # ことがある。その場合は成果物が既に揃っているので、プロンプトを
        # 再発行せずゲートだけ通して先へ進める。これが無いと手動モードで
        # ワークフローが永久に進まない。
        if self.mode is ExecutionMode.MANUAL and stage.gate is not None:
            existing = stage.gate(self.context)
            if existing.ok:
                self.reporter.info("成果物が既に揃っているため、この工程は完了とみなします。")
                stage_state.status = StageStatus.COMPLETED
                stage_state.finished_at = state_mod.now()
                stage_state.error = None
                self.save()
                return StageOutcome(stage.name, StageStatus.COMPLETED, result=existing)

        # AI 工程。ゲートが落ちたら内容を添えて差し戻す。
        feedback = ""
        for attempt in range(1, MAX_ATTEMPTS + 1):
            stage_state.attempts = attempt
            self.save()
            if attempt > 1:
                self.reporter.info(f"再試行 {attempt}/{MAX_ATTEMPTS}")

            ok, message = self.invoke_agent(stage, feedback)
            if not ok:
                stage_state.status = StageStatus.FAILED
                stage_state.error = message
                self.save()
                return StageOutcome(stage.name, StageStatus.FAILED, message=message)

            if stage.gate is None:
                break

            result = stage.gate(self.context)
            for warning in result.warnings:
                self.reporter.warning(warning)
            if result.ok:
                stage_state.status = StageStatus.COMPLETED
                stage_state.finished_at = state_mod.now()
                stage_state.error = None
                self.save()
                return StageOutcome(stage.name, StageStatus.COMPLETED, result=result)

            feedback = "\n".join(f"- {e}" for e in result.errors)
            self.reporter.failure(f"検証ゲートが落ちました (試行 {attempt})")
            for error in result.errors[:10]:
                self.reporter.info(error)

        stage_state.status = StageStatus.FAILED
        stage_state.error = feedback
        self.save()
        return StageOutcome(
            stage.name,
            StageStatus.FAILED,
            message=f"{MAX_ATTEMPTS} 回試行しましたが検証ゲートを通りませんでした。",
        )

    # ------------------------------------------------------------------
    def run(self, *, until: str | None = None) -> StageOutcome | None:
        total = len(STAGE_NAMES)
        last: StageOutcome | None = None
        while True:
            stage = self.next_stage()
            if stage is None:
                self.reporter.success("すべての工程が完了しました。")
                return last
            if until is not None and stage_index(stage.name) > stage_index(until):
                return last

            self.reporter.stage_start(stage_index(stage.name) + 1, total, stage)
            outcome = self.run_stage(stage)
            last = outcome

            if outcome.status is StageStatus.COMPLETED:
                self.reporter.success(f"{stage.title} が完了しました。")
                continue
            if outcome.status is StageStatus.AWAITING_REVIEW:
                self.reporter.warning("人の確認待ちで停止します。")
                self.reporter.info(outcome.message)
                self.reporter.info(f"承認: e2e approve {stage.name}")
                return outcome
            self.reporter.failure(outcome.message or f"{stage.title} が失敗しました。")
            return outcome


def build_run(
    config: Config, spec_document: Path, platform: str
) -> RunState:
    feature = spec_document.stem
    run_id = state_mod.new_run_id(feature)
    try:
        relative = str(spec_document.relative_to(config.root))
    except ValueError:
        relative = str(spec_document)
    run_state = RunState(
        run_id=run_id, spec_document=relative, platform=platform
    )
    state_mod.save(config.artifacts_dir, run_state)
    return run_state
