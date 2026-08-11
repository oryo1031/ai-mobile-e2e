"""実機の画面を覗いて、identifier が本当に露出しているか確かめる。

`e2e scan-app` はアプリのソースを静的に走査するだけで、
「その identifier が実機のアクセシビリティツリーに実際に出るか」までは分からない。
両者が食い違うのが、この方式で最も厄介な失敗の形になる。

Phase 0 で確認したとおり、`Semantics(container: true, identifier:)` の書き方を
外すと identifier は静かに消える。実機ではさらに OS 側の事情が加わりうるため、
本格運用の前にここで実測する。
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from appium import webdriver

from .config import Config
from .registry import Registry
from .session import ANDROID, build_options


@dataclass
class InspectResult:
    platform: str
    #: 画面に出ていた identifier(resource-id / name)。
    found: list[str] = field(default_factory=list)
    #: レジストリに載っているのに画面に出ていなかったもの。
    missing: list[str] = field(default_factory=list)
    #: 画面に出ているがレジストリに無いもの。
    unregistered: list[str] = field(default_factory=list)
    page_source: str = ""
    screenshot: Path | None = None
    source_path: Path | None = None


def _identifiers_in_source(source: str, platform: str) -> set[str]:
    """アクセシビリティツリーから識別子を抜き出す。

    Android は resource-id、iOS は name に identifier が出る。
    """
    root = ET.fromstring(source)
    attribute = "resource-id" if platform == ANDROID else "name"
    values: set[str] = set()
    for element in root.iter():
        value = element.attrib.get(attribute, "")
        if value:
            # Android は "pkg:id/xxx" 形式になることがある。
            values.add(value.split("/")[-1])
    return values


def inspect_screen(
    config: Config,
    registry: Registry,
    platform: str,
    output_dir: Path,
) -> InspectResult:
    """いま端末に出ている画面を取得して、レジストリと突き合わせる。"""
    output_dir.mkdir(parents=True, exist_ok=True)
    options = build_options(config, platform)
    server_url = str(config.appium.get("server_url", "http://127.0.0.1:4723"))

    driver: Any = webdriver.Remote(server_url, options=options)
    try:
        driver.implicitly_wait(5)
        source = str(driver.page_source)
        source_path = output_dir / f"{platform}_page_source.xml"
        source_path.write_text(source, encoding="utf-8")

        screenshot = output_dir / f"{platform}_screen.png"
        driver.get_screenshot_as_file(str(screenshot))
    finally:
        driver.quit()

    on_screen = _identifiers_in_source(source, platform)
    registered = registry.identifiers()

    # 連番付き identifier は接頭辞で照合する。
    def matches_registered(value: str) -> bool:
        return any(
            value == r or (r.rstrip("0123456789") and value.startswith(r.rstrip("0123456789")))
            for r in registered
        )

    return InspectResult(
        platform=platform,
        found=sorted(v for v in on_screen if matches_registered(v)),
        missing=sorted(r for r in registered if r not in on_screen),
        unregistered=sorted(v for v in on_screen if not matches_registered(v)),
        page_source=source,
        screenshot=screenshot,
        source_path=source_path,
    )
