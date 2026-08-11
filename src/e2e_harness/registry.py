"""ロケータレジストリの読み書きと、アプリ実体との突合。

registry.yaml がテスト側の単一ソースになる。AI はここに載っていない要素を
テストコードから参照できない。逆に registry 自体が嘘をついていないかは
アプリのソース走査と突き合わせて検出する。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from .semantics import SemanticsUsage


@dataclass(frozen=True)
class Element:
    id: str
    identifier: str
    role: str
    description: str = ""
    scrollable: bool = True
    dynamic_index: bool = False


@dataclass(frozen=True)
class Screen:
    id: str
    name: str
    elements: tuple[Element, ...]


@dataclass(frozen=True)
class Registry:
    screens: tuple[Screen, ...]

    def all_elements(self) -> list[tuple[Screen, Element]]:
        return [(s, e) for s in self.screens for e in s.elements]

    def identifiers(self) -> set[str]:
        return {e.identifier for _, e in self.all_elements()}

    def screen(self, screen_id: str) -> Screen | None:
        return next((s for s in self.screens if s.id == screen_id), None)


@dataclass
class RegistryDiff:
    """registry とアプリ実体のズレ。"""

    #: registry にあるがアプリのソースに存在しない identifier。
    missing_in_app: list[str]
    #: アプリにあるが registry に載っていない identifier。
    unregistered: list[str]
    #: container: true が無く、マージで消えうる書き方。
    unsafe: list[SemanticsUsage]

    @property
    def has_blocking_problem(self) -> bool:
        """次工程に進ませてはいけないレベルの問題があるか。

        未登録(unregistered)はテストが触らない要素かもしれないので警告に留め、
        registry の嘘と危険な書き方だけをブロッキング扱いにする。
        """
        return bool(self.missing_in_app) or bool(self.unsafe)


def empty_registry() -> Registry:
    return Registry(screens=())


def load(path: Path) -> Registry:
    if not path.is_file():
        return empty_registry()
    raw: dict[str, Any] | None = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not raw or "screens" not in raw or raw["screens"] is None:
        return empty_registry()
    screens: list[Screen] = []
    for screen_raw in raw["screens"]:
        elements = tuple(
            Element(
                id=str(e["id"]),
                identifier=str(e["identifier"]),
                role=str(e["role"]),
                description=str(e.get("description", "")),
                scrollable=bool(e.get("scrollable", True)),
                dynamic_index=bool(e.get("dynamic_index", False)),
            )
            for e in screen_raw.get("elements", [])
        )
        screens.append(
            Screen(
                id=str(screen_raw["id"]),
                name=str(screen_raw.get("name", screen_raw["id"])),
                elements=elements,
            )
        )
    return Registry(screens=tuple(screens))


def dump(registry: Registry, path: Path) -> None:
    data = {
        "screens": [
            {
                "id": s.id,
                "name": s.name,
                "elements": [
                    {
                        "id": e.id,
                        "identifier": e.identifier,
                        "role": e.role,
                        "description": e.description,
                        "scrollable": e.scrollable,
                        "dynamic_index": e.dynamic_index,
                    }
                    for e in s.elements
                ],
            }
            for s in registry.screens
        ]
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )


def _identifier_matches(registered: str, actual: str) -> bool:
    """動的 identifier(連番付き)を考慮した一致判定。

    registry 側が `item_0` のような具体値でも、アプリ側は
    `'item_$index'` と補間で書かれている。接頭辞で照合する。
    """
    if registered == actual:
        return True
    if "$" in actual:
        prefix = actual.split("$", 1)[0]
        return bool(prefix) and registered.startswith(prefix)
    return False


def diff(registry: Registry, usages: list[SemanticsUsage]) -> RegistryDiff:
    actual = [u.identifier for u in usages]

    missing: list[str] = []
    for _, element in registry.all_elements():
        if not any(_identifier_matches(element.identifier, a) for a in actual):
            missing.append(element.identifier)

    registered = registry.identifiers()
    unregistered: list[str] = []
    for usage in usages:
        if not any(_identifier_matches(r, usage.identifier) for r in registered):
            unregistered.append(usage.identifier)

    unsafe = [u for u in usages if not u.is_safe]

    return RegistryDiff(
        missing_in_app=sorted(set(missing)),
        unregistered=sorted(set(unregistered)),
        unsafe=unsafe,
    )
