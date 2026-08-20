"""`e2e` コマンド。

ワークフローの進行はすべてこのコマンドが担う。Copilot 側のエージェントは
「決められた入力を読んで決められた出力を書く」ことだけを行い、順序・状態・
検証はここが持つ。
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path

from . import agents as agents_mod
from . import copilot as copilot_mod
from . import ingest as ingest_mod
from . import packaging, semantics
from . import state as state_mod
from .config import Config, ConfigError, load_config
from .pages import generate as generate_pages
from .registry import diff as registry_diff
from .registry import load as load_registry
from .runner import Runner, build_run
from .stages import (
    STAGE_BY_NAME,
    STAGE_NAMES,
    STAGES,
    StageContext,
    stage_index,
)
from .state import StageStatus

PLATFORMS = ("android", "ios")


def _resolve_run_id(config: Config, requested: str | None) -> str:
    if requested:
        return requested
    runs = state_mod.list_runs(config.artifacts_dir)
    if not runs:
        raise SystemExit("実行履歴がありません。`e2e run --spec <設計書>` から始めてください。")
    return runs[0]


def _appium_server_ready(server_url: str, timeout: float = 2.0) -> bool:
    """Appium サーバが応答するか確認する。"""
    if not server_url:
        return False
    try:
        with urllib.request.urlopen(  # noqa: S310
            f"{server_url.rstrip('/')}/status", timeout=timeout
        ) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (OSError, ValueError):
        return False
    return bool(payload.get("value", {}).get("ready"))


def _connected_android_devices() -> list[str]:
    """adb から見えている端末の一覧。"""
    if shutil.which("adb") is None:
        return []
    try:
        proc = subprocess.run(  # noqa: S603
            ["adb", "devices"], capture_output=True, text=True, timeout=10, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    devices: list[str] = []
    for line in proc.stdout.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 2 and parts[1] == "device":
            devices.append(parts[0])
    return devices


def _ios_devices_output() -> str:
    """接続されている iOS 実機の一覧(生の出力)。"""
    if shutil.which("xcrun") is None:
        return ""
    for argv in (
        ["xcrun", "devicectl", "list", "devices"],
        ["xcrun", "xctrace", "list", "devices"],
    ):
        try:
            proc = subprocess.run(  # noqa: S603
                argv, capture_output=True, text=True, timeout=30, check=False
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        if proc.returncode == 0 and proc.stdout.strip():
            return proc.stdout
    return ""


def _is_ios_app_installed(udid: str, bundle_id: str) -> bool:
    """iOS 実機にアプリが入っているか。

    IDE からビルドして端末に入れる運用では、ビルド成果物のパスを
    当てにできない(Xcode の出力先は DerivedData)。端末側を見る。
    """
    if not udid or not bundle_id or shutil.which("xcrun") is None:
        return False
    try:
        proc = subprocess.run(  # noqa: S603
            ["xcrun", "devicectl", "device", "info", "apps", "--device", udid],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return bundle_id in proc.stdout


def _is_app_installed(package: str) -> bool:
    if not package or shutil.which("adb") is None:
        return False
    try:
        proc = subprocess.run(  # noqa: S603
            ["adb", "shell", "pm", "list", "packages", package],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return f"package:{package}" in proc.stdout


def _status_mark(status: StageStatus) -> str:
    return {
        StageStatus.COMPLETED: "✓",
        StageStatus.FAILED: "✗",
        StageStatus.RUNNING: "…",
        StageStatus.AWAITING_REVIEW: "⏸",
        StageStatus.AWAITING_MANUAL: "⏸",
        StageStatus.PENDING: " ",
    }[status]


# ----------------------------------------------------------------------
def cmd_run(config: Config, args: argparse.Namespace) -> int:
    spec = Path(args.spec)
    if not spec.is_absolute():
        spec = (config.root / spec).resolve()
    if not spec.is_file():
        raise SystemExit(f"設計書が見つかりません: {spec}")

    run_state = build_run(config, spec, args.platform)
    mode = copilot_mod.resolve_mode(args.mode, config.copilot.command)
    print(f"実行 ID: {run_state.run_id}")
    print(f"設計書  : {run_state.spec_document}")
    print(f"対象    : {run_state.platform}")
    print(f"実行方式: {mode.label}")
    if mode is copilot_mod.ExecutionMode.MANUAL:
        print(
            "  Copilot CLI が見つからないため手動になりました。"
            "各工程のプロンプトをファイルに書き出します。"
        )

    runner = Runner(config, run_state, mode=mode)
    outcome = runner.run(until=args.until)
    return 0 if outcome is None or outcome.status is not StageStatus.FAILED else 1


def cmd_resume(config: Config, args: argparse.Namespace) -> int:
    run_id = _resolve_run_id(config, args.run)
    run_state = state_mod.load(config.artifacts_dir, run_id)

    force: set[str] = set()
    if args.redo:
        # 指定した工程とそれ以降を未実行に戻す。前の工程の成果物は残す。
        # 状態を戻すだけでは、成果物が揃っている工程が「検証を通るので
        # 飛ばす」に拾われて再実行されない。force で明示的に上書きする。
        start = stage_index(args.redo)
        force = set(STAGE_NAMES[start:])
        reset = [name for name in STAGE_NAMES[start:] if name in run_state.stages]
        for name in reset:
            run_state.stages[name] = state_mod.StageState()
        state_mod.save(config.artifacts_dir, run_state)
        titles = ", ".join(STAGE_BY_NAME[n].title for n in reset)
        print(f"やり直します: {titles or STAGE_BY_NAME[args.redo].title}")

    mode = copilot_mod.resolve_mode(args.mode, config.copilot.command)
    print(f"実行 ID: {run_id} を再開します")
    print(f"実行方式: {mode.label}")
    runner = Runner(config, run_state, mode=mode, force=force)
    outcome = runner.run(until=args.until)
    return 0 if outcome is None or outcome.status is not StageStatus.FAILED else 1


def cmd_status(config: Config, args: argparse.Namespace) -> int:
    runs = state_mod.list_runs(config.artifacts_dir)
    if not runs:
        print("実行履歴がありません。")
        return 0
    run_id = _resolve_run_id(config, args.run)
    run_state = state_mod.load(config.artifacts_dir, run_id)

    print(f"実行 ID : {run_state.run_id}")
    print(f"設計書  : {run_state.spec_document}")
    print(f"対象    : {run_state.platform}")
    print(f"成果物  : {config.artifacts_dir / run_id}")
    print()
    for name in STAGE_NAMES:
        stage = STAGE_BY_NAME[name]
        stage_state = run_state.stage(name)
        mark = _status_mark(stage_state.status)
        attempts = (
            f" (試行 {stage_state.attempts})" if stage_state.attempts > 1 else ""
        )
        print(f"  [{mark}] {stage.title}{attempts}")
        if stage_state.error:
            first = stage_state.error.strip().splitlines()[0]
            print(f"       └ {first}")
    if len(runs) > 1:
        print()
        print(f"他に {len(runs) - 1} 件の実行履歴があります。--run で指定できます。")
    return 0


def cmd_approve(config: Config, args: argparse.Namespace) -> int:
    run_id = _resolve_run_id(config, args.run)
    run_state = state_mod.load(config.artifacts_dir, run_id)
    stage = STAGE_BY_NAME.get(args.stage)
    if stage is None:
        raise SystemExit(f"不明な工程です: {args.stage}")
    if not stage.human_review:
        raise SystemExit(f"'{args.stage}' は人の承認を必要とする工程ではありません。")

    stage_state = run_state.stage(args.stage)
    stage_state.status = StageStatus.COMPLETED
    stage_state.finished_at = state_mod.now()
    state_mod.save(config.artifacts_dir, run_state)
    print(f"✓ {stage.title} を承認しました。")
    print("  続行: e2e resume")
    return 0


def cmd_stages(config: Config, args: argparse.Namespace) -> int:
    del config, args
    print("ワークフローの工程:\n")
    for index, stage in enumerate(STAGES, start=1):
        who = "人" if stage.human_review else (stage.agent or "コマンド")
        print(f"  {index}. {stage.name:10s} {stage.title}  [{who}]")
    return 0


def cmd_scan_app(config: Config, args: argparse.Namespace) -> int:
    del args
    usages = semantics.scan_directory(config.app_lib_dir)
    registry = load_registry(config.locators_path)
    delta = registry_diff(registry, usages)

    print(f"走査対象: {config.app_lib_dir}")
    print(f"identifier: {len(usages)} 件\n")
    for usage in sorted(usages, key=lambda u: u.identifier):
        safe = "  " if usage.has_container_true else "!!"
        dynamic = " (動的)" if usage.is_dynamic else ""
        print(f"  {safe} {usage.identifier}{dynamic}  {usage.file.name}:{usage.line}")

    if delta.unsafe:
        print(f"\n!! container: true が無い箇所が {len(delta.unsafe)} 件あります。")
        print("   マージにより identifier がネイティブ側に露出しません。")
    if delta.unregistered:
        print(f"\n未登録の identifier: {len(delta.unregistered)} 件")
    if delta.missing_in_app:
        print(f"\nレジストリにあるがアプリに存在しない: {len(delta.missing_in_app)} 件")
        for identifier in delta.missing_in_app:
            print(f"  - {identifier}")
    return 1 if delta.has_blocking_problem else 0


def cmd_inspect(config: Config, args: argparse.Namespace) -> int:
    """実機に今出ている画面を取得し、identifier の露出を確認する。"""
    from . import inspect as inspect_mod

    registry = load_registry(config.locators_path)
    output_dir = config.artifacts_dir / "inspect"

    print(f"{args.platform} の端末に接続しています...")
    try:
        result = inspect_mod.inspect_screen(
            config, registry, args.platform, output_dir
        )
    except Exception as exc:  # noqa: BLE001 - 原因は多岐にわたるのでそのまま見せる
        print(f"\n接続に失敗しました: {type(exc).__name__}", file=sys.stderr)
        print(f"{exc}", file=sys.stderr)
        if args.platform == "ios":
            print(
                "\niOS 実機では WebDriverAgent の署名が必要です。"
                "xcodebuild が exit code 65 で落ちている場合は"
                " appium.ios.xcode_org_id を確認してください。",
                file=sys.stderr,
            )
        return 1

    print(f"  画面           : {result.screenshot}")
    print(f"  ツリー         : {result.source_path}")
    print()

    attribute = "resource-id" if args.platform == "android" else "name"
    print(f"画面に出ている identifier ({attribute}):")
    if result.found:
        for identifier in result.found:
            print(f"  ✓ {identifier}")
    else:
        print("  (レジストリに載っている identifier は 1 つも出ていません)")

    if result.missing:
        print("\nレジストリにあるが、この画面には出ていない:")
        for identifier in result.missing:
            print(f"  - {identifier}")
        print(
            "  ※ 別画面の要素なら正常。今の画面にあるはずのものが出ていない場合は、"
            "\n    Semantics(container: true, identifier: ...) の書き方を確認する。"
        )

    if args.all and result.unregistered:
        print("\nレジストリに無い識別子(アプリ以外の要素も含む):")
        for identifier in result.unregistered[:40]:
            print(f"  ? {identifier}")

    return 0 if result.found or not registry.screens else 1


def cmd_gen_pages(config: Config, args: argparse.Namespace) -> int:
    del args
    registry = load_registry(config.locators_path)
    if not registry.screens:
        raise SystemExit(f"ロケータレジストリが空です: {config.locators_path}")
    written = generate_pages(
        registry, config.generated_pages_dir, source=config.locators_path.name
    )
    for path in written:
        print(f"  生成: {path.relative_to(config.root)}")
    print(f"\n{len(written)} ファイルを生成しました。")
    return 0


def cmd_gate(config: Config, args: argparse.Namespace) -> int:
    run_id = _resolve_run_id(config, args.run)
    run_state = state_mod.load(config.artifacts_dir, run_id)
    stage = STAGE_BY_NAME.get(args.stage)
    if stage is None or stage.gate is None:
        raise SystemExit(f"'{args.stage}' に検証ゲートはありません。")

    ctx = StageContext(
        config=config,
        run_id=run_id,
        platform=run_state.platform,
        spec_document=run_state.spec_document,
    )
    result = stage.gate(ctx)
    for warning in result.warnings:
        print(f"  ! {warning}")
    if result.ok:
        print(f"✓ {stage.title} の検証ゲートを通過しました。")
        return 0
    print(f"✗ {stage.title} の検証ゲートが落ちました:")
    for error in result.errors:
        print(f"  - {error}")
    return 1


def cmd_sync_agents(config: Config, args: argparse.Namespace) -> int:
    """エージェント定義を再生成する。

    ハーネス配下には常に出力する。Copilot CLI は作業ディレクトリから
    git ルートまでを探すため、ハーネスが単独の git リポジトリの場合は
    ここに無いと 1 つも見つからない。

    --output を指定すると、そこにも複製する(VS Code 用にアプリのルートへ)。
    """
    written = agents_mod.sync(config.root)
    print(f"  生成: .github/agents/ ({len(written)} ファイル / Copilot CLI 用)")

    if args.output:
        output_dir = Path(args.output)
        if not output_dir.is_absolute():
            output_dir = (config.root / output_dir).resolve()
        agents_mod.sync(config.root, output_dir)
        print(f"  生成: {output_dir} ({len(written)} ファイル / VS Code 用)")
    else:
        print(
            "\nVS Code の Copilot はワークスペース直下の .github/agents/ しか"
            "探しません。\nアプリのルートを開いて使う場合は、そちらにも出力してください:\n"
            "  e2e sync-agents --output ../.github/agents"
        )
    return 0


def cmd_ingest(config: Config, args: argparse.Namespace) -> int:
    """Excel や Confluence の設計書を取り込む。"""
    source = Path(args.source)
    if not source.is_absolute():
        source = (Path.cwd() / source).resolve()

    if args.output:
        output = Path(args.output)
        if not output.is_absolute():
            output = (config.root / output).resolve()
    else:
        output = config.specs_dir / f"{source.stem}.md"

    try:
        result = ingest_mod.ingest(source, output, sheets=args.sheet or None)
    except ingest_mod.IngestError as exc:
        print(f"取り込みエラー: {exc}", file=sys.stderr)
        return 1

    print(f"取り込み: {result.source.name}")
    print(f"  出力: {result.output.relative_to(config.root)}")
    if result.sections:
        print(f"  セクション数: {result.sections}")
    for warning in result.warnings:
        print(f"  ! {warning}")

    print("\n変換結果に目を通してから次へ進んでください。")
    print(f"  uv run e2e run --spec {result.output.relative_to(config.root)}")
    return 0


def cmd_package(config: Config, args: argparse.Namespace) -> int:
    """スナップショットの zip を作る。

    通常の配布はアプリリポジトリ経由で行う。これは退避や、
    リポジトリを介さず一時的に渡したいときのための補助。
    """
    destination = Path(args.output)
    if not destination.is_absolute():
        destination = (Path.cwd() / destination).resolve()

    result = packaging.create(
        config.root,
        destination,
        prefix=args.prefix,
        include_node_modules=args.include_node_modules,
    )
    print(f"作成: {result.archive}")
    print(f"  ファイル数: {result.file_count}")
    print(f"  サイズ: {result.size_mb:.1f} MB")
    print(f"  展開すると {args.prefix}/ になります。")
    if not args.include_node_modules:
        print(
            "\nAppium 本体 (node_modules) は含めていません。展開先で\n"
            "`npm install` と `appium driver install` が必要です。\n"
            "展開先の端末でネットワークが使えない場合は"
            " --include-node-modules を付けてください。"
        )
    return 0


def cmd_init(config: Config, args: argparse.Namespace) -> int:
    """配置直後に、自分のアプリ向けの状態へ初期化する。

    このリポジトリには検証用アプリ向けの成果物(ロケータ・Page Object・
    テスト・設計書)が例として入っている。それらを残したまま実アプリに
    向けると、無関係なテストが走ってしまうため、ここで片付ける。
    """
    from .config import CONFIG_FILENAME, render_config

    removed: list[str] = []
    kept: list[str] = []

    # 1. 設定ファイルを対象アプリ向けに作り直す。
    config_path = config.root / CONFIG_FILENAME
    config_path.write_text(
        render_config(
            app_root=args.app_root,
            android_package=args.android_package,
            android_activity=args.android_activity,
            # 省略時は Android と同じ識別子とみなす。異なるアプリでは
            # 明示が要るため、下で実際に使う値を表示する。
            ios_bundle_id=args.ios_bundle_id or args.android_package,
        ),
        encoding="utf-8",
    )
    ios_bundle_id = args.ios_bundle_id or args.android_package
    print(f"  更新: {CONFIG_FILENAME}")
    print(f"       app.root          = {args.app_root}")
    print(f"       android_package   = {args.android_package}")
    print(f"       android_activity  = {args.android_activity}")
    print(f"       ios_bundle_id     = {ios_bundle_id}")
    if not args.ios_bundle_id:
        print(
            "       ! iOS の Bundle ID は Android と同じとみなしました。"
            "異なる場合は --ios-bundle-id で指定してください。"
        )

    # 2. ロケータレジストリを空にする。
    config.locators_path.parent.mkdir(parents=True, exist_ok=True)
    config.locators_path.write_text("screens: []\n", encoding="utf-8")
    removed.append(str(config.locators_path.relative_to(config.root)))

    # 3. 生成済みの Page Object を消す。base.py は手書きなので残す。
    if config.generated_pages_dir.is_dir():
        for path in config.generated_pages_dir.glob("*.py"):
            if path.name in ("base.py", "__init__.py"):
                kept.append(str(path.relative_to(config.root)))
                continue
            path.unlink()
            removed.append(str(path.relative_to(config.root)))
        (config.generated_pages_dir / "__init__.py").write_text(
            '"""Page Object。screen 単位のモジュールは e2e gen-pages が生成する。"""\n'
            "\n"
            "from .base import BasePage\n"
            "\n"
            '__all__ = ["BasePage"]\n',
            encoding="utf-8",
        )

    # 4. 生成済みのテストを消す。
    if config.generated_tests_dir.is_dir():
        for path in config.generated_tests_dir.glob("test_*.py"):
            path.unlink()
            removed.append(str(path.relative_to(config.root)))

    # 5. 例として入っている設計書を消す。雛形は残す。
    if not args.keep_specs:
        for path in config.specs_dir.glob("*.md"):
            if path.name == "SPEC_TEMPLATE.md":
                continue
            path.unlink()
            removed.append(str(path.relative_to(config.root)))

    for path_str in removed:
        print(f"  削除: {path_str}")

    # 6. エージェント定義を生成する。
    #
    # VS Code と Copilot CLI で探索の仕方が違うため、両方に置く。
    #
    # - VS Code: ワークスペース直下の .github/agents/ のみ。アプリのルートを
    #   開いて使うので、そちらに要る
    # - Copilot CLI: 作業ディレクトリから **git ルート** までの .github/agents/。
    #   ハーネスを単独の git リポジトリとして配置した場合、git ルートは
    #   ハーネス自身になるため、ハーネス配下にも要る
    #
    # どちらか一方だけにすると、片方の経路でエージェントが 1 つも見つからない。
    app_root = (config.root / args.app_root).resolve()

    agents_mod.sync(config.root)
    print("  生成: .github/agents/ (6 エージェント / Copilot CLI 用)")

    if app_root.is_dir() and app_root != config.root.resolve():
        target = app_root / ".github" / "agents"
        agents_mod.sync(config.root, target)
        print(f"  生成: {target} (6 エージェント / VS Code 用)")
    else:
        print(
            f"  ! アプリのルート ({app_root}) が見つからないため、"
            "ハーネス配下にのみ出力しました。"
        )
        print(
            "    アプリ配下に置いたあと "
            "`e2e sync-agents --output ../.github/agents` を実行してください。"
        )

    print("\n初期化しました。次の手順:")
    print("  1. uv run e2e doctor        # 前提条件を確認")
    print("  2. specs/ に設計書を書く (specs/SPEC_TEMPLATE.md を雛形にする)")
    print("  3. uv run e2e run --spec specs/<機能>.md")
    return 0


def cmd_doctor(config: Config, args: argparse.Namespace) -> int:
    """前提条件を確認する。

    これは助言ツールであり、進行をブロックしない。すべてが ✓ になることを
    目指す作りにもしていない。対象外のプラットフォームの項目や、
    手動モードで代替できる項目まで失敗として数えると、正常な状態でも
    永久に「問題あり」と出てしまうため。

    終了コードは「必須」の失敗数だけで決める。
    """
    target = args.platform  # android / ios / both
    required_failures = 0
    optional_failures = 0

    def check(
        label: str, ok: bool, hint: str = "", *, required: bool = True
    ) -> None:
        nonlocal required_failures, optional_failures
        if ok:
            print(f"  ✓ {label}")
            return
        if required:
            required_failures += 1
            print(f"  ✗ {label}")
        else:
            optional_failures += 1
            print(f"  △ {label}  (任意)")
        if hint:
            print(f"      {hint}")

    def skip(label: str) -> None:
        print(f"  - {label}  (対象外)")

    check_android = target in ("android", "both")
    check_ios = target in ("ios", "both")

    print(f"前提条件の確認 (対象: {target})\n")

    # --- 共通 ---
    check(
        f"アプリのソース: {config.app_lib_dir}",
        config.app_lib_dir.is_dir(),
        "e2e.config.yaml の app.root を確認してください。",
    )
    check(
        "Appium CLI",
        shutil.which("appium") is not None
        or (config.root / "node_modules/.bin/appium").exists(),
        "npm install で Appium を導入してください。",
    )
    server_url = str(config.appium.get("server_url", ""))
    check(
        f"Appium サーバの応答 ({server_url})",
        _appium_server_ready(server_url),
        "別のターミナルで `npx appium --port "
        f"{server_url.rsplit(':', 1)[-1]}` を起動してください。",
    )

    # --- Android ---
    if check_android:
        # IDE からビルドして端末へ入れる運用ではファイルが無くて当然なので、
        # 必須にしない。Appium に投入させたい場合にだけ要る。
        check(
            f"Android ビルド成果物: {config.android_apk_path.name}",
            config.android_apk_path.is_file(),
            "IDE からビルドして端末へ入れる運用なら不要です。"
            " コマンドでビルドする場合は flutter build apk --debug"
            " --target-platform android-arm64 --split-per-abi を実行してください。",
            required=False,
        )
        check(
            "adb",
            shutil.which("adb") is not None,
            "Android SDK の platform-tools を PATH に追加してください。",
        )
        devices = _connected_android_devices()
        android_udid = str(config.appium.get("android", {}).get("udid", ""))
        check(
            f"Android 実機の接続 ({android_udid or 'udid 未設定'})",
            bool(devices) and (not android_udid or android_udid in devices),
            (
                "USB で接続し、端末側で USB デバッグを有効にしてください。"
                " 初回は端末に出る認証ダイアログを許可する必要があります。"
                if not devices
                else f"接続中: {', '.join(devices)}。"
                " e2e.config.yaml の appium.android.udid と一致しません。"
            ),
        )
        if devices and not android_udid:
            print(
                f"      複数台つなぐ場合は udid を設定してください:"
                f" {', '.join(devices)}"
            )
        # 実機に入っているかが本命。ファイルの有無より確実。
        if devices:
            check(
                f"アプリが端末に入っているか ({config.app.android_package})",
                _is_app_installed(config.app.android_package),
                "IDE からビルドして端末へ入れてください。"
                " コマンドの場合は adb install -r <APK>。"
                " アプリを直したら入れ直すこと。",
            )
    else:
        skip("Android 関連")

    # --- iOS ---
    if check_ios:
        # Xcode の出力先は DerivedData なので、IDE ビルドではここに出ない。
        # Appium に投入させたい場合にだけ要るため、必須にしない。
        check(
            f"iOS ビルド成果物: {config.ios_app_path.name}",
            config.ios_app_path.is_dir()
            and "iphonesimulator" not in str(config.build.ios_app),
            "IDE からビルドして端末へ入れる運用なら不要です。"
            " コマンドでビルドする場合は flutter build ios --release。",
            required=False,
        )
        ios_cfg = config.appium.get("ios", {})
        ios_udid = str(ios_cfg.get("udid", ""))
        check(
            f"iOS 実機の接続 ({ios_udid or 'udid 未設定'})",
            bool(ios_udid) and ios_udid in _ios_devices_output(),
            (
                "`xcrun devicectl list devices` で UDID を確認し、"
                " e2e.config.yaml の appium.ios.udid に設定してください。"
                if not ios_udid
                else "端末が見つかりません。USB 接続、ペアリング"
                "(このコンピュータを信頼)、端末側の「デベロッパモード」が"
                "有効か確認してください。"
            ),
        )
        check(
            "iOS の署名設定 (xcode_org_id)",
            bool(ios_cfg.get("xcode_org_id")),
            "実機では WebDriverAgent の署名が必須です。Apple Developer の"
            " Team ID を appium.ios.xcode_org_id に設定してください。"
            " 未設定だと xcodebuild が exit code 65 で落ちます。",
        )
        if ios_udid:
            check(
                f"アプリが端末に入っているか ({config.app.ios_bundle_id})",
                _is_ios_app_installed(ios_udid, config.app.ios_bundle_id),
                "Xcode からビルドして端末へ入れてください。"
                " **スキームの Build Configuration を Release にすること。**"
                " debug のまま入れると Appium から起動できません。",
            )
    else:
        skip("iOS 関連")

    # --- ハーネスの資産 ---
    check(
        "ロケータレジストリ",
        config.locators_path.is_file(),
        "最初の実行時に locator-curator が作成します。",
    )
    # VS Code と Copilot CLI で探索の仕方が違うため、置き場所が 2 つある。
    # 使う経路に必要な方が無いと、エージェントが 1 つも見つからない。
    harness_agents = config.root / ".github" / "agents"
    app_agents = config.app.root / ".github" / "agents"
    check(
        "Copilot エージェント定義 (VS Code 用: アプリのルート)",
        app_agents.is_dir(),
        f"{app_agents} が必要です。"
        " e2e sync-agents --output ../.github/agents を実行してください。",
    )
    if copilot_mod.cli_available(config.copilot.command):
        # CLI は作業ディレクトリから git ルートまでしか探さない。
        # ハーネスが単独の git リポジトリなら、ここに無いと見つからない。
        check(
            "Copilot エージェント定義 (CLI 用: ハーネス配下)",
            harness_agents.is_dir(),
            f"{harness_agents} が必要です。e2e sync-agents を実行してください。",
        )
    # 手動モードが正式な運用のひとつなので、無くても失敗にはしない。
    check(
        f"Copilot CLI ({config.copilot.command})",
        copilot_mod.cli_available(config.copilot.command),
        "無い場合は手動モードで運用します(工程ごとにプロンプトを書き出し、"
        "VS Code のチャットに貼って実行する)。",
        required=False,
    )

    print()
    if required_failures:
        print(f"必須の不足: {required_failures} 件 — 解消しないとテストを実行できません。")
    else:
        print("必須の項目はすべて満たしています。")
    if optional_failures:
        print(f"任意の不足: {optional_failures} 件 — 運用は可能です。")
    return 1 if required_failures else 0


# ----------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="e2e",
        description="Flutter アプリの E2E テストを AI エージェントで進めるハーネス",
    )
    parser.add_argument("--config", type=Path, help="e2e.config.yaml のパス")
    sub = parser.add_subparsers(dest="command", required=True)

    run_p = sub.add_parser("run", help="設計書からワークフローを開始する")
    run_p.add_argument("--spec", required=True, help="設計書 (Markdown) のパス")
    run_p.add_argument("--platform", choices=PLATFORMS, default="android")
    run_p.add_argument("--mode", choices=("auto", "cli", "manual"), default="auto")
    run_p.add_argument("--until", choices=STAGE_NAMES, help="この工程まで進めて止める")
    run_p.set_defaults(func=cmd_run)

    resume_p = sub.add_parser("resume", help="中断地点から再開する")
    resume_p.add_argument("--run", help="実行 ID (既定: 最新)")
    resume_p.add_argument("--mode", choices=("auto", "cli", "manual"), default="auto")
    resume_p.add_argument("--until", choices=STAGE_NAMES)
    resume_p.add_argument(
        "--redo",
        choices=STAGE_NAMES,
        help="この工程以降をやり直す。既定では、検証を通る工程は飛ばされる。",
    )
    resume_p.set_defaults(func=cmd_resume)

    status_p = sub.add_parser("status", help="進捗を表示する")
    status_p.add_argument("--run", help="実行 ID (既定: 最新)")
    status_p.set_defaults(func=cmd_status)

    approve_p = sub.add_parser("approve", help="人のレビュー工程を承認する")
    approve_p.add_argument("stage", choices=[s.name for s in STAGES if s.human_review])
    approve_p.add_argument("--run", help="実行 ID (既定: 最新)")
    approve_p.set_defaults(func=cmd_approve)

    stages_p = sub.add_parser("stages", help="工程の一覧を表示する")
    stages_p.set_defaults(func=cmd_stages)

    scan_p = sub.add_parser("scan-app", help="アプリの Semantics(identifier:) を走査する")
    scan_p.set_defaults(func=cmd_scan_app)

    inspect_p = sub.add_parser(
        "inspect", help="実機の画面を取得し identifier の露出を確認する"
    )
    inspect_p.add_argument("--platform", choices=PLATFORMS, default="android")
    inspect_p.add_argument(
        "--all", action="store_true", help="レジストリに無い識別子も表示する"
    )
    inspect_p.set_defaults(func=cmd_inspect)

    gen_p = sub.add_parser("gen-pages", help="レジストリから Page Object を生成する")
    gen_p.set_defaults(func=cmd_gen_pages)

    gate_p = sub.add_parser("gate", help="特定工程の検証ゲートだけ実行する")
    gate_p.add_argument("stage", choices=[s.name for s in STAGES if s.gate])
    gate_p.add_argument("--run", help="実行 ID (既定: 最新)")
    gate_p.set_defaults(func=cmd_gate)

    sync_p = sub.add_parser(
        "sync-agents", help="prompts から Copilot エージェント定義を生成する"
    )
    sync_p.add_argument(
        "--output",
        help="出力先ディレクトリ (既定: .github/agents)。"
        "アプリリポジトリ直下の .github/agents を指定できる。",
    )
    sync_p.set_defaults(func=cmd_sync_agents)

    ingest_p = sub.add_parser(
        "ingest", help="Excel / Confluence の設計書を取り込んで specs/ に変換する"
    )
    ingest_p.add_argument(
        "source", help="設計書のパス (.xlsx / .html / .csv / .md / .txt)"
    )
    ingest_p.add_argument("--output", help="出力先 (既定: specs/<元ファイル名>.md)")
    ingest_p.add_argument(
        "--sheet",
        action="append",
        help="取り込む Excel シート名。複数指定可。既定は全シート。",
    )
    ingest_p.set_defaults(func=cmd_ingest)

    package_p = sub.add_parser(
        "package", help="スナップショットの zip を作る(退避や一時共有用)"
    )
    package_p.add_argument(
        "--output", default="ai-mobile-e2e.zip", help="出力先の zip パス"
    )
    package_p.add_argument(
        "--prefix",
        default="ai-mobile-e2e",
        help="展開時のディレクトリ名",
    )
    package_p.add_argument(
        "--include-node-modules",
        action="store_true",
        help="Appium 本体を同梱する(展開先でネットワークが使えない場合)",
    )
    package_p.set_defaults(func=cmd_package)

    init_p = sub.add_parser(
        "init", help="配置直後に自分のアプリ向けへ初期化する"
    )
    init_p.add_argument(
        "--app-root",
        default="..",
        help="アプリリポジトリのルート(このファイルからの相対パス)",
    )
    # プラットフォームが名前から分かるようにする。
    # 単に --package だと、どちらの識別子か読み取れない。
    init_p.add_argument(
        "--android-package",
        required=True,
        help="Android のパッケージ名 (例: com.example.app)",
    )
    init_p.add_argument(
        "--android-activity",
        default=".MainActivity",
        help="Android の起動 Activity (既定: .MainActivity)",
    )
    init_p.add_argument(
        "--ios-bundle-id",
        default="",
        help="iOS の Bundle ID。省略すると --android-package の値を流用する",
    )
    init_p.add_argument(
        "--keep-specs", action="store_true", help="specs/ の既存ファイルを残す"
    )
    init_p.set_defaults(func=cmd_init)

    doctor_p = sub.add_parser("doctor", help="前提条件を確認する")
    doctor_p.add_argument(
        "--platform",
        choices=(*PLATFORMS, "both"),
        default="both",
        help="確認するプラットフォーム(既定: both)。"
        "片方だけ試す日は絞ると対象外の項目が判定から外れる。",
    )
    doctor_p.set_defaults(func=cmd_doctor)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print(f"設定エラー: {exc}", file=sys.stderr)
        return 2
    return int(args.func(config, args))


if __name__ == "__main__":
    raise SystemExit(main())
