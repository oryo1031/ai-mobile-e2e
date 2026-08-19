"""ハーネス設定の読み込み。

このツールキットは開発端末上のアプリリポジトリ配下に置いて使うため、
アプリの場所は e2e.config.yaml でのみ指定し、コード中に埋め込まない。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

CONFIG_FILENAME = "e2e.config.yaml"


class ConfigError(RuntimeError):
    """設定が見つからない、または内容が不正。"""


@dataclass(frozen=True)
class AppConfig:
    root: Path
    lib_dir: Path
    android_package: str
    android_activity: str
    ios_bundle_id: str


@dataclass(frozen=True)
class BuildConfig:
    android_apk: Path
    ios_app: Path


@dataclass(frozen=True)
class CopilotConfig:
    command: str
    model: str
    default_allow_tools: str
    timeout_seconds: int


@dataclass(frozen=True)
class Config:
    root: Path
    app: AppConfig
    build: BuildConfig
    appium: dict[str, Any]
    copilot: CopilotConfig
    paths: dict[str, str]

    @property
    def specs_dir(self) -> Path:
        return self.root / self.paths["specs"]

    @property
    def artifacts_dir(self) -> Path:
        return self.root / self.paths["artifacts"]

    @property
    def locators_path(self) -> Path:
        return self.root / self.paths["locators"]

    @property
    def generated_tests_dir(self) -> Path:
        return self.root / self.paths["generated_tests"]

    @property
    def generated_pages_dir(self) -> Path:
        return self.root / self.paths["generated_pages"]

    @property
    def setup_dir(self) -> Path:
        """前提条件のセットアップ置き場。"""
        return self.root / self.paths.get("setup", "tests/setup")

    @property
    def app_lib_dir(self) -> Path:
        return self.app.root / self.app.lib_dir

    @property
    def android_apk_path(self) -> Path:
        return self.app.root / self.build.android_apk

    @property
    def ios_app_path(self) -> Path:
        return self.app.root / self.build.ios_app


CONFIG_TEMPLATE = """\
# E2E ハーネス設定
#
# このツールキットは開発端末上のアプリリポジトリ配下に置いて使う。
# アプリ側のソースをこのリポジトリに取り込む必要はなく、下の app.root から
# 相対的に参照する。開発端末ごとに変わる値はここだけを書き換える。

app:
  # アプリリポジトリのルート。このファイルからの相対パス、または絶対パス。
  # 例: アプリの配下に tools/ai-mobile-e2e として置いたなら "../.."
  root: "{app_root}"

  # Flutter のソースディレクトリ(app.root からの相対)。
  # Semantics(identifier:) の静的走査対象。
  lib_dir: "{lib_dir}"

  # アプリ識別子。
  android_package: "{android_package}"
  android_activity: "{android_activity}"
  ios_bundle_id: "{ios_bundle_id}"

build:
  # ビルド成果物のパス(app.root からの相対)。
  #
  # これは Appium にインストールまでさせたい場合にだけ使う。IDE の実行ボタンで
  # 端末に入れる運用では参照されない(Xcode の出力先は DerivedData のため、
  # そもそもこのパスには出ない)。ハーネスは端末に入っているアプリを
  # appPackage / bundleId から起動するだけ。
  #
  # iOS 実機は release ビルドであること。iOS 14 以降、debug ビルドは
  # Appium から起動できない(JIT 制約のため)。Xcode の実行ボタンを使う場合は
  # スキームの Build Configuration を Release にしておく。
  android_apk: "{android_apk}"
  ios_app: "{ios_app}"

appium:
  server_url: "{server_url}"

  android:
    platform_name: "Android"
    automation_name: "UiAutomator2"
    # 実機のシリアル番号。`adb devices -l` で確認する。
    # 端末の選択に実際に使われるのはこちらで、device_name は表示用。
    udid: "{android_udid}"
    device_name: "{android_device}"

  ios:
    platform_name: "iOS"
    automation_name: "XCUITest"
    # 実機の UDID。`xcrun devicectl list devices` で確認する。
    udid: "{ios_udid}"
    device_name: "{ios_device}"
    # 端末の iOS バージョンと一致させる。
    platform_version: "{ios_version}"

    # --- 実機で必須の署名設定 ---
    # WebDriverAgent を端末にインストールするために署名が要る。
    # xcode_org_id は Apple Developer の Team ID(10 桁)。
    # 設定が誤っていると xcodebuild が exit code 65 で落ちる。
    xcode_org_id: "{xcode_org_id}"
    xcode_signing_id: "iPhone Developer"
    # 無料の Apple ID を使う場合や、Bundle ID を固定したい場合に指定する。
    updated_wda_bundle_id: "{updated_wda_bundle_id}"

copilot:
  # Copilot CLI の実行ファイル。
  command: "copilot"
  # 再現性のためモデルを固定する。空にすると Copilot の既定に従う。
  model: ""
  # 各工程で許可するツール。工程ごとに上書きできる。
  default_allow_tools: "read,write"
  # 1 工程あたりのタイムアウト(秒)。
  timeout_seconds: 900

paths:
  specs: "specs"
  artifacts: "artifacts"
  locators: "locators/registry.yaml"
  generated_tests: "tests/e2e"
  generated_pages: "tests/pages"
  setup: "tests/setup"
"""


def render_config(**values: str) -> str:
    """設定ファイルの内容を組み立てる。

    コメントを保ったまま再生成できるよう、既存ファイルを書き換えるのではなく
    テンプレートから作り直す方式にしている。
    """
    defaults = {
        "app_root": "..",
        "lib_dir": "lib",
        "android_package": "com.example.app",
        "android_activity": ".MainActivity",
        "ios_bundle_id": "com.example.app",
        # 実機は arm64。split-per-abi でビルドすると容量も小さくなる。
        "android_apk": "build/app/outputs/flutter-apk/app-arm64-v8a-debug.apk",
        # 実機用ビルド。iphonesimulator のものは実機で動かない。
        "ios_app": "build/ios/iphoneos/Runner.app",
        "server_url": "http://127.0.0.1:4723",
        "android_udid": "",
        "android_device": "Android 実機",
        "ios_udid": "",
        "ios_device": "iPhone 実機",
        "ios_version": "",
        "xcode_org_id": "",
        "updated_wda_bundle_id": "",
    }
    defaults.update({k: v for k, v in values.items() if v})
    return CONFIG_TEMPLATE.format(**defaults)


def find_config_file(start: Path | None = None) -> Path:
    """カレントディレクトリから上に向かって e2e.config.yaml を探す。"""
    current = (start or Path.cwd()).resolve()
    for candidate in [current, *current.parents]:
        path = candidate / CONFIG_FILENAME
        if path.is_file():
            return path
    raise ConfigError(
        f"{CONFIG_FILENAME} が見つかりません。ハーネスのルートで実行してください。"
    )


def load_config(path: Path | None = None) -> Config:
    config_path = path or find_config_file()
    root = config_path.parent
    raw: dict[str, Any] = yaml.safe_load(config_path.read_text(encoding="utf-8"))

    for key in ("app", "build", "appium", "copilot", "paths"):
        if key not in raw:
            raise ConfigError(f"{CONFIG_FILENAME} に '{key}' セクションがありません。")

    app_raw = raw["app"]
    # app.root は設定ファイルからの相対で解決する。開発端末ごとに
    # 絶対パスが変わるため、相対指定できることが重要。
    app_root = (root / str(app_raw["root"])).resolve()

    app = AppConfig(
        root=app_root,
        lib_dir=Path(str(app_raw.get("lib_dir", "lib"))),
        android_package=str(app_raw["android_package"]),
        android_activity=str(app_raw.get("android_activity", ".MainActivity")),
        ios_bundle_id=str(app_raw.get("ios_bundle_id", "")),
    )

    build_raw = raw["build"]
    build = BuildConfig(
        android_apk=Path(str(build_raw["android_apk"])),
        ios_app=Path(str(build_raw["ios_app"])),
    )

    copilot_raw = raw["copilot"]
    copilot = CopilotConfig(
        command=str(copilot_raw.get("command", "copilot")),
        model=str(copilot_raw.get("model", "")),
        default_allow_tools=str(copilot_raw.get("default_allow_tools", "read,write")),
        timeout_seconds=int(copilot_raw.get("timeout_seconds", 900)),
    )

    return Config(
        root=root,
        app=app,
        build=build,
        appium=raw["appium"],
        copilot=copilot,
        paths={str(k): str(v) for k, v in raw["paths"].items()},
    )
