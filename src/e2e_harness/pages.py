"""ロケータレジストリから Page Object を生成する。

AI にロケータ文字列を書かせないことがハルシネーション対策の要になる。
テストコードは生成された Page Object のメソッドしか呼べず、そのメソッド名は
registry.yaml に実在する要素からしか生まれない。
"""

from __future__ import annotations

import ast
from pathlib import Path

from .registry import Element, Registry, Screen

HEADER = '''"""自動生成ファイル。手で編集しないこと。

生成元: {source}
再生成: e2e gen-pages
"""

from __future__ import annotations

from .base import BasePage
'''


def _class_name(screen_id: str) -> str:
    return "".join(part.capitalize() for part in screen_id.split("_")) + "Page"


def _const_name(element_id: str) -> str:
    return element_id.upper()


def _method_lines(element: Element) -> list[str]:
    """要素の役割に応じた操作メソッドを組み立てる。"""
    const = _const_name(element.id)
    scroll = "" if element.scrollable else ", scrollable=False"
    lines: list[str] = []

    if element.dynamic_index:
        # 連番付き identifier。接頭辞に index を足して解決する。
        prefix = element.identifier.rstrip("0123456789")
        lines += [
            f"    def {element.id}_identifier(self, index: int) -> str:",
            f'        return f"{prefix}{{index}}"',
            "",
        ]
        if element.role in ("button", "list_item"):
            lines += [
                f"    def tap_{element.id}(self, index: int) -> None:",
                f"        self.tap(self.{element.id}_identifier(index){scroll})",
                "",
            ]
        lines += [
            f"    def {element.id}_text(self, index: int) -> str:",
            f"        return self.text_of(self.{element.id}_identifier(index){scroll})",
            "",
            f"    def is_{element.id}_displayed(self, index: int) -> bool:",
            f"        return self.is_displayed(self.{element.id}_identifier(index))",
            "",
        ]
        return lines

    if element.role in ("button", "list_item", "image"):
        lines += [
            f"    def tap_{element.id}(self) -> None:",
            f"        self.tap(self.{const}{scroll})",
            "",
        ]
    if element.role == "text_field":
        lines += [
            f"    def input_{element.id}(self, value: str) -> None:",
            f"        self.input(self.{const}, value{scroll})",
            "",
        ]
    if element.role in ("checkbox", "switch"):
        lines += [
            f"    def toggle_{element.id}(self) -> None:",
            f"        self.toggle(self.{const}{scroll})",
            "",
            f"    def is_{element.id}_checked(self) -> bool:",
            f"        return self.is_checked(self.{const}{scroll})",
            "",
        ]

    # 文言の取得と表示確認はすべての要素に用意する。
    lines += [
        f"    def {element.id}_text(self) -> str:",
        f"        return self.text_of(self.{const}{scroll})",
        "",
        f"    def is_{element.id}_displayed(self) -> bool:",
        f"        return self.is_displayed(self.{const})",
        "",
        f"    def wait_for_{element.id}(self) -> None:",
        f"        self.wait_for(self.{const})",
        "",
    ]
    return lines


def render_screen(screen: Screen, source: str) -> str:
    # クラス定義の前は空行 2 行。生成物もそのまま lint に通す。
    lines: list[str] = [HEADER.format(source=source).rstrip(), "", ""]
    lines.append(f"class {_class_name(screen.id)}(BasePage):")
    lines.append(f'    """{screen.name}"""')
    lines.append("")
    lines.append(f'    SCREEN_ID = "{screen.id}"')
    lines.append("")
    for element in screen.elements:
        description = element.description or element.role
        lines.append(f"    # {description}")
        lines.append(f'    {_const_name(element.id)} = "{element.identifier}"')
    lines.append("")
    for element in screen.elements:
        lines.extend(_method_lines(element))
    # 末尾の余分な空行を落とす。
    while lines and lines[-1] == "":
        lines.pop()
    return "\n".join(lines) + "\n"


def render_init(registry: Registry) -> str:
    lines = [
        '"""自動生成ファイル。手で編集しないこと。"""',
        "",
        "from .base import BasePage",
    ]
    # import は isort の順序に合わせて画面 ID の昇順で並べる。
    for screen in sorted(registry.screens, key=lambda s: s.id):
        lines.append(f"from .{screen.id} import {_class_name(screen.id)}")
    lines.append("")
    names = ['"BasePage"'] + [
        f'"{_class_name(s.id)}"' for s in sorted(registry.screens, key=lambda s: s.id)
    ]
    lines.append("__all__ = [")
    for name in names:
        lines.append(f"    {name},")
    lines.append("]")
    return "\n".join(lines) + "\n"


def generate(registry: Registry, output_dir: Path, source: str) -> list[Path]:
    """Page Object 一式を書き出し、生成したファイルの一覧を返す。"""
    output_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for screen in registry.screens:
        path = output_dir / f"{screen.id}.py"
        path.write_text(render_screen(screen, source), encoding="utf-8")
        written.append(path)
    init_path = output_dir / "__init__.py"
    init_path.write_text(render_init(registry), encoding="utf-8")
    written.append(init_path)
    return written


#: BasePage を読めなかったときに使う最低限の一覧。
#: 実際は base.py から読み取るので、通常こちらは使われない。
_FALLBACK_BASE_METHODS = frozenset(
    {"tap", "input", "toggle", "find", "text_of", "is_checked", "is_displayed"}
)


def base_page_methods(pages_dir: Path) -> set[str]:
    """BasePage が実際に持っている公開メソッドを読み取る。

    以前はここを手書きの一覧で持っていたため、BasePage にメソッドを
    足しても検証側が知らないままになり、生成テストが「存在しない
    メソッドを呼んでいる」と誤判定されて工程が止まった。
    実物から読むことで二重管理をなくす。
    """
    base_file = pages_dir / "base.py"
    if not base_file.is_file():
        return set(_FALLBACK_BASE_METHODS)
    try:
        tree = ast.parse(base_file.read_text(encoding="utf-8"), filename=str(base_file))
    except SyntaxError:
        return set(_FALLBACK_BASE_METHODS)

    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "BasePage":
            return {
                item.name
                for item in node.body
                if isinstance(item, ast.FunctionDef)
                and not item.name.startswith("_")
            }
    return set(_FALLBACK_BASE_METHODS)


def public_api(registry: Registry, pages_dir: Path | None = None) -> dict[str, set[str]]:
    """テストコードから呼んでよいクラスとメソッドの一覧。

    テストコードの静的検証で「存在しないメソッドを呼んでいないか」を
    判定するために使う。
    """
    base_methods = (
        base_page_methods(pages_dir) if pages_dir is not None
        else set(_FALLBACK_BASE_METHODS)
    )

    # 画面に属さない操作(ディープリンクなど)は BasePage を直接使う。
    api: dict[str, set[str]] = {"BasePage": set(base_methods)}

    for screen in registry.screens:
        methods: set[str] = set(base_methods)
        for element in screen.elements:
            rendered = "\n".join(_method_lines(element))
            for line in rendered.splitlines():
                stripped = line.strip()
                if stripped.startswith("def "):
                    methods.add(stripped[4:].split("(", 1)[0])
        api[_class_name(screen.id)] = methods
    return api
