"""Page Object の共通基盤。

Phase 0 の実測結果をここに閉じ込めてある。テストコード側は identifier 名だけを
扱い、下記のプラットフォーム差やスクロールの都合を一切意識しなくてよい。

1. Android では `AppiumBy.ID` が使えない。Appium が bare な id にパッケージ名を
   補完してしまい、Flutter が出す接頭辞なしの resource-id と一致しないため。
   XPath か UiSelector を使う必要がある。iOS は ACCESSIBILITY_ID で引ける。
2. a11y ツリーには画面内に描画されているノードしか出ない。画面外の要素は
   find_element が必ず失敗するため、既定で scroll-into-view を挟む。
3. 値の読み取り先の属性がプラットフォームで異なる(Android は content-desc、
   iOS は value/label)。フォールバック順をここで吸収する。

このファイルは生成物ではない。手で保守する。
"""

from __future__ import annotations

from typing import Any

from appium.webdriver.common.appiumby import AppiumBy
from selenium.common.exceptions import (
    NoSuchElementException,
    TimeoutException,
    WebDriverException,
)
from selenium.webdriver.support import expected_conditions as ec
from selenium.webdriver.support.ui import WebDriverWait

ANDROID = "android"
IOS = "ios"

DEFAULT_TIMEOUT = 15


class ElementNotFoundError(AssertionError):
    """identifier に対応する要素が見つからない。

    ロケータ不備なのかプロダクト側の問題なのかを run-analyst が
    切り分けられるよう、専用の例外にしてある。
    """


class DeeplinkError(AssertionError):
    """ディープリンクを開けなかった。

    「開いていないのに次のステップへ進む」と、失敗の原因が
    まったく別の場所に見えてしまうため専用の例外にしてある。
    """


class BasePage:
    #: 生成される各 Page Object が上書きする。
    SCREEN_ID: str = ""

    def __init__(self, driver: Any, platform: str) -> None:
        self.driver = driver
        self.platform = platform.lower()

    # ------------------------------------------------------------------
    # ロケータ解決
    # ------------------------------------------------------------------
    def _locator(self, identifier: str) -> tuple[str, str]:
        if self.platform == ANDROID:
            return (AppiumBy.XPATH, f"//*[@resource-id='{identifier}']")
        return (AppiumBy.ACCESSIBILITY_ID, identifier)

    def _scroll_locator(self, identifier: str) -> tuple[str, str]:
        if self.platform == ANDROID:
            return (
                AppiumBy.ANDROID_UIAUTOMATOR,
                "new UiScrollable(new UiSelector().scrollable(true))"
                f'.scrollIntoView(new UiSelector().resourceId("{identifier}"))',
            )
        # iOS はスクロール可能な祖先に対する検索で自動的に可視化される。
        return (
            AppiumBy.IOS_PREDICATE,
            f"name == '{identifier}'",
        )

    def find(
        self,
        identifier: str,
        *,
        scrollable: bool = True,
        timeout: int = DEFAULT_TIMEOUT,
    ) -> Any:
        """要素を取得する。画面外なら既定でスクロールして探す。"""
        by, value = self._locator(identifier)
        try:
            return WebDriverWait(self.driver, timeout).until(
                ec.presence_of_element_located((by, value))
            )
        except (TimeoutException, NoSuchElementException):
            pass

        if scrollable:
            scroll_by, scroll_value = self._scroll_locator(identifier)
            try:
                return self.driver.find_element(scroll_by, scroll_value)
            except (TimeoutException, NoSuchElementException):
                pass

        raise ElementNotFoundError(
            f"要素が見つかりません: identifier={identifier!r} "
            f"platform={self.platform} screen={self.SCREEN_ID!r}\n"
            "アプリ側に Semantics(container: true, identifier: ...) が"
            "付与されているか確認してください。"
        )

    # ------------------------------------------------------------------
    # 操作
    # ------------------------------------------------------------------
    def tap(self, identifier: str, *, scrollable: bool = True) -> None:
        self.find(identifier, scrollable=scrollable).click()

    def input(self, identifier: str, value: str, *, scrollable: bool = True) -> None:
        element = self.find(identifier, scrollable=scrollable)
        element.click()
        element.clear()
        element.send_keys(value)

    def toggle(self, identifier: str, *, scrollable: bool = True) -> None:
        self.find(identifier, scrollable=scrollable).click()

    # ------------------------------------------------------------------
    # 検証
    # ------------------------------------------------------------------
    def text_of(self, identifier: str, *, scrollable: bool = True) -> str:
        """表示文言を取得する。属性の在り処がプラットフォームで違う。"""
        element = self.find(identifier, scrollable=scrollable)
        attributes = (
            ["content-desc", "text"]
            if self.platform == ANDROID
            else ["value", "label", "name"]
        )
        for attribute in attributes:
            value = element.get_attribute(attribute)
            if value:
                return str(value)
        return ""

    def is_checked(self, identifier: str, *, scrollable: bool = True) -> bool:
        element = self.find(identifier, scrollable=scrollable)
        if self.platform == ANDROID:
            return element.get_attribute("checked") == "true"
        # iOS の Switch は value が "0" / "1"。
        return element.get_attribute("value") in ("1", "true")

    def is_displayed(self, identifier: str, *, scrollable: bool = False) -> bool:
        try:
            return bool(self.find(identifier, scrollable=scrollable, timeout=3))
        except ElementNotFoundError:
            return False

    def wait_for(self, identifier: str, *, timeout: int = DEFAULT_TIMEOUT) -> Any:
        return self.find(identifier, timeout=timeout)

    # ------------------------------------------------------------------
    # 端末の機能
    # ------------------------------------------------------------------
    def open_deeplink(self, url: str) -> None:
        """ディープリンクでアプリを開く。

        画面操作ではなく端末の機能なので、Page Object の生成対象ではなく
        ここに置く。

        **`driver.get(url)` は使わない。** エミュレータとシミュレータでは
        動くが、実機では動かないことが知られている。代わりに
        `mobile: deepLink` を使う(Android では内部で `am start` が走る)。

        QR コードから起動する試験項目などは、QR の読み取りそのものを
        自動化するのではなく、QR が指す URL をここへ渡して代替する。
        """
        capabilities = getattr(self.driver, "capabilities", {}) or {}
        if self.platform == ANDROID:
            target = capabilities.get("appPackage") or capabilities.get(
                "appium:appPackage"
            )
            args = {"url": url, "package": target}
        else:
            target = capabilities.get("bundleId") or capabilities.get(
                "appium:bundleId"
            )
            args = {"url": url, "bundleId": target}

        try:
            self.driver.execute_script("mobile: deepLink", args)
        except WebDriverException as exc:
            # 端末やドライバのバージョンによっては使えないことがある。
            # 失敗を握りつぶすと「開いていないのに次へ進む」ので、
            # 代替を試したうえで、それも駄目なら理由を添えて落とす。
            try:
                self.driver.get(url)
            except WebDriverException as fallback_exc:
                raise DeeplinkError(
                    f"ディープリンクを開けません: {url}\n"
                    f"  mobile: deepLink -> {exc.msg}\n"
                    f"  driver.get()     -> {fallback_exc.msg}\n"
                    "実機では driver.get() が使えないため、"
                    "mobile: deepLink が失敗する原因を確認してください。"
                ) from exc
