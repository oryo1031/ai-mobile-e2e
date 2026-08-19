"""テストアカウントの参照。

認証情報をテストコードに直接書かないための入口。`testdata/accounts.yaml`
を単一ソースとし、テストとセットアップは id で参照する。

パスワードが変わったとき、直す場所が 1 か所で済む。試験項目ごとに値を
埋め込むと、変更のたびに全件を追うことになる。

このファイルは生成物ではない。手で保守する。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

ACCOUNTS_FILE = "testdata/accounts.yaml"


class AccountNotFoundError(KeyError):
    """参照されたアカウントが定義されていない。"""


@lru_cache(maxsize=1)
def _load() -> dict[str, dict[str, str]]:
    root = Path(__file__).resolve().parent.parent
    path = root / ACCOUNTS_FILE
    if not path.is_file():
        return {}
    data: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return {
        str(entry["id"]): {
            str(k): str(v) for k, v in (entry.get("attributes") or {}).items()
        }
        for entry in data.get("accounts") or []
    }


def account(account_id: str) -> dict[str, str]:
    """アカウントの属性を返す。

    ログインのセットアップへはそのまま展開して渡せる。

        setup_logged_in(driver, platform, **account("card_member"))
    """
    accounts = _load()
    if account_id not in accounts:
        known = ", ".join(sorted(accounts)) or "(未定義)"
        raise AccountNotFoundError(
            f"アカウント '{account_id}' が {ACCOUNTS_FILE} にありません。"
            f" 定義済み: {known}"
        )
    return dict(accounts[account_id])


def known_accounts() -> set[str]:
    return set(_load())
