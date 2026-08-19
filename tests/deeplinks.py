"""ディープリンクの参照。

URL をテストコードに直接書かないための入口。`testdata/deeplinks.yaml`
を単一ソースとし、テストは id で参照する。

URL が変わったとき、直す場所が 1 か所で済む。試験項目に埋め込む形にすると、
試験項目を作り直すたびに URL が消える。

このファイルは生成物ではない。手で保守する。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

DEEPLINKS_FILE = "testdata/deeplinks.yaml"


class DeeplinkNotFoundError(KeyError):
    """参照されたディープリンクが定義されていない。"""


@lru_cache(maxsize=1)
def _load() -> dict[str, str]:
    import yaml

    root = Path(__file__).resolve().parent.parent
    path = root / DEEPLINKS_FILE
    if not path.is_file():
        return {}
    data: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return {
        str(entry["id"]): str(entry["url"])
        for entry in data.get("deeplinks") or []
        if entry.get("id") and entry.get("url")
    }


def deeplink(deeplink_id: str) -> str:
    """ディープリンクの URL を返す。

        home.open_deeplink(deeplink("campaign_detail"))
    """
    links = _load()
    if deeplink_id not in links:
        known = ", ".join(sorted(links)) or "(未定義)"
        raise DeeplinkNotFoundError(
            f"ディープリンク '{deeplink_id}' が {DEEPLINKS_FILE} にありません。"
            f" 定義済み: {known}"
        )
    return links[deeplink_id]


def known_deeplinks() -> set[str]:
    return set(_load())
