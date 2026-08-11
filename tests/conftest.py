"""テスト実行基盤と証跡の自動取得。

証跡取得をここに集約しているのは、テストコード側に書かせると AI が必ず
書き漏らすため。テストは「何を確かめるか」だけを書き、スクリーンショット・
動画・ログ・画面階層の取得はすべてこのフックが面倒を見る。

証跡のファイル名は試験項目 ID に対応させる。結果確認をする人と
run-analyst エージェントが、証跡と試験項目を突き合わせられるようにするため。
"""

from __future__ import annotations

import base64
import contextlib
import re
from pathlib import Path
from typing import Any

import pytest
import yaml
from appium import webdriver
from appium.options.android import UiAutomator2Options
from appium.options.ios import XCUITestOptions

CONFIG_FILENAME = "e2e.config.yaml"
_TESTCASE_ID_RE = re.compile(r"^test_(tc_[a-z0-9_]+)$")


# ----------------------------------------------------------------------
# オプション
# ----------------------------------------------------------------------
def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--platform",
        action="store",
        default="android",
        choices=("android", "ios"),
        help="テスト対象のプラットフォーム",
    )
    parser.addoption(
        "--evidence-dir",
        action="store",
        default=None,
        help="証跡の出力先ディレクトリ",
    )
    parser.addoption(
        "--no-video",
        action="store_true",
        default=False,
        help="動画の録画を無効にする",
    )


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "e2e: Appium を使う E2E テスト")


# ----------------------------------------------------------------------
# 設定
# ----------------------------------------------------------------------
def _find_config(start: Path) -> Path:
    for candidate in [start, *start.parents]:
        path = candidate / CONFIG_FILENAME
        if path.is_file():
            return path
    raise FileNotFoundError(f"{CONFIG_FILENAME} が見つかりません")


@pytest.fixture(scope="session")
def harness_config() -> dict[str, Any]:
    path = _find_config(Path(__file__).resolve().parent)
    data: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8"))
    data["_root"] = path.parent
    return data


@pytest.fixture(scope="session")
def platform(request: pytest.FixtureRequest) -> str:
    return str(request.config.getoption("--platform"))


@pytest.fixture(scope="session")
def evidence_root(request: pytest.FixtureRequest, harness_config: dict[str, Any]) -> Path:
    option = request.config.getoption("--evidence-dir")
    if option:
        path = Path(str(option))
    else:
        path = Path(harness_config["_root"]) / "artifacts" / "local" / "evidence"
    path.mkdir(parents=True, exist_ok=True)
    return path


# ----------------------------------------------------------------------
# ドライバ
# ----------------------------------------------------------------------
def _build_options(
    platform_name: str, config: dict[str, Any]
) -> UiAutomator2Options | XCUITestOptions:
    root = Path(config["_root"])
    app_root = (root / config["app"]["root"]).resolve()

    if platform_name == "android":
        options = UiAutomator2Options()
        appium_cfg = config["appium"]["android"]
        options.platform_name = appium_cfg["platform_name"]
        options.device_name = appium_cfg["device_name"]
        options.automation_name = appium_cfg["automation_name"]
        # アプリは事前に投入しておき、ここでは起動するだけにする。
        # 同一バージョンだと再インストールがスキップされ、古いビルドを
        # 検証してしまう事故を避けるため。
        options.app_package = config["app"]["android_package"]
        options.app_activity = config["app"]["android_activity"]
        options.new_command_timeout = 300
        return options

    options_ios = XCUITestOptions()
    appium_cfg = config["appium"]["ios"]
    options_ios.platform_name = appium_cfg["platform_name"]
    options_ios.device_name = appium_cfg["device_name"]
    options_ios.platform_version = str(appium_cfg["platform_version"])
    options_ios.automation_name = appium_cfg["automation_name"]
    options_ios.app = str(app_root / config["build"]["ios_app"])
    options_ios.new_command_timeout = 300
    return options_ios


@pytest.fixture
def driver(
    request: pytest.FixtureRequest,
    harness_config: dict[str, Any],
    platform: str,
    evidence_root: Path,
) -> Any:
    """テストごとに Appium セッションを張る。

    テスト間の状態汚染を避けるため、セッションはテスト単位で使い捨てる。
    """
    options = _build_options(platform, harness_config)
    server_url = harness_config["appium"]["server_url"]
    session = webdriver.Remote(server_url, options=options)
    session.implicitly_wait(5)

    record_video = not request.config.getoption("--no-video")
    if record_video:
        try:
            session.start_recording_screen()
        except Exception:  # noqa: BLE001 - 録画非対応環境でもテストは続行する
            record_video = False

    request.node.stash[_DRIVER_KEY] = session
    request.node.stash[_EVIDENCE_KEY] = _evidence_dir(evidence_root, request.node.name)
    request.node.stash[_VIDEO_KEY] = record_video

    yield session

    try:
        _capture_final_evidence(request.node, session, record_video)
    finally:
        session.quit()


# ----------------------------------------------------------------------
# 証跡
# ----------------------------------------------------------------------
_DRIVER_KEY = pytest.StashKey[Any]()
_EVIDENCE_KEY = pytest.StashKey[Path]()
_VIDEO_KEY = pytest.StashKey[bool]()
_FAILED_KEY = pytest.StashKey[bool]()


def testcase_id(test_name: str) -> str:
    """テスト関数名から試験項目 ID を復元する。

    test-codegen が `test_tc_login_001` の形式で関数名を作る規約になっている。
    """
    match = _TESTCASE_ID_RE.match(test_name.split("[")[0])
    if match:
        return match.group(1).upper()
    return test_name


def _evidence_dir(root: Path, test_name: str) -> Path:
    path = root / testcase_id(test_name)
    path.mkdir(parents=True, exist_ok=True)
    return path


def _safe_write(path: Path, content: str) -> None:
    with contextlib.suppress(OSError):
        path.write_text(content, encoding="utf-8")


def _capture_final_evidence(node: Any, session: Any, record_video: bool) -> None:
    directory: Path = node.stash[_EVIDENCE_KEY]
    failed = node.stash.get(_FAILED_KEY, False)

    # 成否によらず最終画面は残す。合格の証跡としても使う。
    with contextlib.suppress(Exception):
        session.get_screenshot_as_file(str(directory / "final.png"))

    if failed:
        # 失敗時は原因究明に必要なものを厚めに取る。
        # run-analyst はこれらを根拠に失敗を分類する。
        with contextlib.suppress(Exception):
            _safe_write(directory / "page_source.xml", session.page_source)
        for log_type, filename in (("logcat", "logcat.txt"), ("syslog", "syslog.txt")):
            try:
                entries = session.get_log(log_type)
                _safe_write(
                    directory / filename,
                    "\n".join(str(e.get("message", e)) for e in entries),
                )
            except Exception:  # noqa: BLE001
                continue

    if record_video:
        with contextlib.suppress(Exception):
            payload = session.stop_recording_screen()
            if failed and payload:
                (directory / "recording.mp4").write_bytes(base64.b64decode(payload))


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo[None]):  # type: ignore[no-untyped-def]
    """テストの成否を記録し、証跡の取り方を切り替える。"""
    outcome = yield
    report = outcome.get_result()
    if report.when == "call" and report.failed:
        item.stash[_FAILED_KEY] = True
        # 失敗直後、セッションが生きているうちにスクリーンショットを取る。
        session = item.stash.get(_DRIVER_KEY, None)
        directory = item.stash.get(_EVIDENCE_KEY, None)
        if session is not None and directory is not None:
            with contextlib.suppress(Exception):
                session.get_screenshot_as_file(str(directory / "failure.png"))
