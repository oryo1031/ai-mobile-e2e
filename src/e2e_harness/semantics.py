"""アプリの Dart ソースから Semantics(identifier:) を抽出する。

Phase 0 の実測で、`Semantics(identifier:)` は `container: true` を明示しないと
兄弟ノードとマージされ identifier が消えることが分かっている。ここでは
identifier の一覧を取るだけでなく、その落とし穴に該当する書き方も検出する。

アプリのソースはこのリポジトリには取り込めないため、設定ファイルの app.root
から相対的に走査する。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

WIDGET = "Semantics"
_IDENTIFIER_RE = re.compile(r"\bidentifier\s*:\s*(['\"])(?P<value>[^'\"]*)\1")
_CONTAINER_RE = re.compile(r"\bcontainer\s*:\s*true\b")
_MERGE_SEMANTICS_RE = re.compile(r"\bMergeSemantics\b")


@dataclass(frozen=True)
class SemanticsUsage:
    """ソース中の 1 個の Semantics(identifier:) 呼び出し。"""

    identifier: str
    file: Path
    line: int
    has_container_true: bool
    #: identifier に文字列補間が含まれる(例: 'item_$index')。
    is_dynamic: bool

    @property
    def is_safe(self) -> bool:
        """マージで identifier が失われない書き方かどうか。"""
        return self.has_container_true


def _find_call_spans(source: str, widget: str) -> list[tuple[int, int]]:
    """`widget(` の開き括弧に対応する閉じ括弧までの範囲を返す。

    文字列リテラル内の括弧を数えないよう、簡易的に引用符を追跡する。
    """
    spans: list[tuple[int, int]] = []
    pattern = re.compile(rf"\b{re.escape(widget)}\s*\(")
    for match in pattern.finditer(source):
        start = match.end() - 1  # 開き括弧の位置
        depth = 0
        index = start
        quote: str | None = None
        while index < len(source):
            char = source[index]
            if quote is not None:
                if char == "\\":
                    index += 2
                    continue
                if char == quote:
                    quote = None
            elif char in "'\"":
                quote = char
            elif char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
                if depth == 0:
                    spans.append((start, index))
                    break
            index += 1
    return spans


def _top_level_args(body: str) -> str:
    """ネストした呼び出しを除いた、直下の引数部分だけを取り出す。

    `child:` に入れ子になった別の Semantics の container 指定を
    自分のものと誤認しないために必要。
    """
    out: list[str] = []
    depth = 0
    quote: str | None = None
    index = 0
    while index < len(body):
        char = body[index]
        if quote is not None:
            if char == "\\":
                index += 2
                continue
            if char == quote:
                quote = None
            if depth == 0:
                out.append(char)
            index += 1
            continue
        if char in "'\"":
            quote = char
            if depth == 0:
                out.append(char)
        elif char in "([{":
            depth += 1
        elif char in ")]}":
            depth -= 1
        elif depth == 0:
            out.append(char)
        index += 1
    return "".join(out)


def scan_source(source: str, path: Path) -> list[SemanticsUsage]:
    usages: list[SemanticsUsage] = []
    for start, end in _find_call_spans(source, WIDGET):
        body = source[start + 1 : end]
        top = _top_level_args(body)
        ident_match = _IDENTIFIER_RE.search(top)
        if not ident_match:
            continue
        # 文字列補間があると識別子に $ が残る。
        raw = source[start + 1 : end]
        interpolated = re.search(
            r"\bidentifier\s*:\s*(['\"])[^'\"]*\$[^'\"]*\1", raw
        )
        line = source.count("\n", 0, start) + 1
        usages.append(
            SemanticsUsage(
                identifier=ident_match.group("value"),
                file=path,
                line=line,
                has_container_true=bool(_CONTAINER_RE.search(top)),
                is_dynamic=bool(interpolated),
            )
        )
    return usages


def scan_directory(lib_dir: Path) -> list[SemanticsUsage]:
    """アプリの lib 配下を走査して Semantics(identifier:) を集める。"""
    if not lib_dir.is_dir():
        raise FileNotFoundError(
            f"アプリのソースディレクトリが見つかりません: {lib_dir}\n"
            "e2e.config.yaml の app.root / app.lib_dir を確認してください。"
        )
    usages: list[SemanticsUsage] = []
    for dart_file in sorted(lib_dir.rglob("*.dart")):
        source = dart_file.read_text(encoding="utf-8", errors="replace")
        usages.extend(scan_source(source, dart_file))
    return usages


def unsafe_usages(usages: list[SemanticsUsage]) -> list[SemanticsUsage]:
    """マージで identifier が失われうる書き方を抜き出す。"""
    return [u for u in usages if not u.is_safe]
