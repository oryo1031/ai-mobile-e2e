"""Appium セッションの設定を組み立てる。

テスト実行 (conftest.py) と実機確認 (e2e inspect) の両方から使う。
capability の組み立てを 2 か所に書くと、片方だけ直して実機で動かない、
という事故が起きるため 1 か所にまとめてある。
"""

from __future__ import annotations

from typing import Any

from appium.options.android import UiAutomator2Options
from appium.options.ios import XCUITestOptions

from .config import Config

ANDROID = "android"
IOS = "ios"


def build_options(
    config: Config, platform: str
) -> UiAutomator2Options | XCUITestOptions:
    """実機向けのセッション設定を組み立てる。

    端末の選択には udid を使う。deviceName は選択に使われないため、
    複数台つないでいるときに意図しない端末へ流れないよう udid を入れる。
    """
    if platform == ANDROID:
        return _android_options(config)
    return _ios_options(config)


def _android_options(config: Config) -> UiAutomator2Options:
    cfg: dict[str, Any] = config.appium.get(ANDROID, {})
    options = UiAutomator2Options()
    options.platform_name = str(cfg.get("platform_name", "Android"))
    options.automation_name = str(cfg.get("automation_name", "UiAutomator2"))
    options.device_name = str(cfg.get("device_name", "Android"))
    if cfg.get("udid"):
        options.udid = str(cfg["udid"])

    # アプリは事前に投入しておき、ここでは起動するだけにする。
    # 同一バージョンだと再インストールがスキップされ、古いビルドを
    # 検証してしまう事故を避けるため。
    options.app_package = config.app.android_package
    options.app_activity = config.app.android_activity
    options.new_command_timeout = 300
    return options


def _ios_options(config: Config) -> XCUITestOptions:
    cfg: dict[str, Any] = config.appium.get(IOS, {})
    options = XCUITestOptions()
    options.platform_name = str(cfg.get("platform_name", "iOS"))
    options.automation_name = str(cfg.get("automation_name", "XCUITest"))
    options.device_name = str(cfg.get("device_name", "iPhone"))
    if cfg.get("udid"):
        options.udid = str(cfg["udid"])
    if cfg.get("platform_version"):
        options.platform_version = str(cfg["platform_version"])

    # 実機では WebDriverAgent を端末にインストールするため署名が要る。
    # 設定が誤っていると xcodebuild が exit code 65 で落ちる。
    if cfg.get("xcode_org_id"):
        options.xcode_org_id = str(cfg["xcode_org_id"])
        options.xcode_signing_id = str(cfg.get("xcode_signing_id", "iPhone Developer"))
    if cfg.get("updated_wda_bundle_id"):
        options.updated_wda_bundle_id = str(cfg["updated_wda_bundle_id"])

    options.bundle_id = config.app.ios_bundle_id
    # 実機用ビルドがあれば Appium に投入させる。
    # 無ければ導入済みの前提で bundleId から起動する。
    if config.ios_app_path.exists():
        options.app = str(config.ios_app_path)

    options.new_command_timeout = 300
    return options
