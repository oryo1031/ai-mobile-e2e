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
from appium import webdriver

from e2e_harness.config import Config, load_config
from e2e_harness.session import build_options

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
@pytest.fixture(scope="session")
def harness_config() -> Config:
    """ハーネス設定。capability の組み立ても含めてパッケージ側と共有する。"""
    from e2e_harness.config import find_config_file

    return load_config(find_config_file(Path(__file__).resolve().parent))


@pytest.fixture(scope="session")
def platform(request: pytest.FixtureRequest) -> str:
    return str(request.config.getoption("--platform"))


@pytest.fixture(scope="session")
def evidence_root(request: pytest.FixtureRequest, harness_config: Config) -> Path:
    option = request.config.getoption("--evidence-dir")
    path = (
        Path(str(option))
        if option
        else harness_config.artifacts_dir / "local" / "evidence"
    )
    path.mkdir(parents=True, exist_ok=True)
    return path


# ----------------------------------------------------------------------
# ドライバ
# ----------------------------------------------------------------------
@pytest.fixture
def driver(
    request: pytest.FixtureRequest,
    harness_config: Config,
    platform: str,
    evidence_root: Path,
) -> Any:
    """テストごとに Appium セッションを張る。

    テスト間の状態汚染を避けるため、セッションはテスト単位で使い捨てる。
    """
    options = build_options(harness_config, platform)
    server_url = str(harness_config.appium.get("server_url", "http://127.0.0.1:4723"))
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
