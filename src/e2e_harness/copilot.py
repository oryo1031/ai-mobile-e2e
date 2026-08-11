"""GitHub Copilot の呼び出し層。

開発端末では Copilot CLI が使える場合と、VS Code の拡張しか使えない場合の
両方がありうる。前者は自動実行、後者は人がチャットに貼って実行する運用に
なるため、同じプロンプト資産で両方を賄えるようにしてある。
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path


class ExecutionMode(StrEnum):
    #: Copilot CLI を非対話モードで叩く。
    CLI = "cli"
    #: プロンプトをファイルに書き出し、人が VS Code のチャットで実行する。
    MANUAL = "manual"


@dataclass
class CopilotResult:
    ok: bool
    stdout: str
    stderr: str
    returncode: int


class CopilotUnavailableError(RuntimeError):
    """Copilot CLI が見つからない。"""


def cli_available(command: str = "copilot") -> bool:
    return shutil.which(command) is not None


def resolve_mode(requested: str, command: str) -> ExecutionMode:
    """実行モードを決める。auto なら CLI の有無で自動判定する。"""
    if requested == "auto":
        return ExecutionMode.CLI if cli_available(command) else ExecutionMode.MANUAL
    return ExecutionMode(requested)


def build_command(
    *,
    command: str,
    prompt: str,
    agent: str,
    allow_tools: str,
    model: str,
    add_dirs: list[Path] | None = None,
) -> list[str]:
    """Copilot CLI の非対話実行コマンドを組み立てる。

    -p で非対話、-s で装飾を落として出力をそのまま扱えるようにする。
    --no-ask-user は自動実行中に入力待ちで止まるのを防ぐために必須。
    """
    argv = [
        command,
        "-p",
        prompt,
        "--agent",
        agent,
        "--allow-tool",
        allow_tools,
        "--no-ask-user",
        "-s",
    ]
    if model:
        argv += ["--model", model]
    for directory in add_dirs or []:
        argv += ["--add-dir", str(directory)]
    return argv


def run_cli(
    *,
    command: str,
    prompt: str,
    agent: str,
    allow_tools: str,
    model: str,
    cwd: Path,
    timeout_seconds: int,
    add_dirs: list[Path] | None = None,
) -> CopilotResult:
    if not cli_available(command):
        raise CopilotUnavailableError(
            f"Copilot CLI '{command}' が見つかりません。"
            " --mode manual を使うか、CLI をインストールしてください。"
        )
    argv = build_command(
        command=command,
        prompt=prompt,
        agent=agent,
        allow_tools=allow_tools,
        model=model,
        add_dirs=add_dirs,
    )
    try:
        proc = subprocess.run(  # noqa: S603
            argv,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return CopilotResult(
            ok=False,
            stdout="",
            stderr=f"{timeout_seconds} 秒でタイムアウトしました。",
            returncode=-1,
        )
    return CopilotResult(
        ok=proc.returncode == 0,
        stdout=proc.stdout,
        stderr=proc.stderr,
        returncode=proc.returncode,
    )


def write_manual_prompt(
    *, run_directory: Path, stage_name: str, agent: str, prompt: str
) -> Path:
    """手動実行用にプロンプトを書き出す。

    VS Code の Copilot Chat でエージェントを選んで貼り付けるための
    手順を先頭に付ける。
    """
    directory = run_directory / "prompts"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{stage_name}.prompt.md"
    header = (
        f"<!-- 工程: {stage_name} / エージェント: {agent} -->\n"
        f"# 手動実行の手順\n\n"
        f"1. VS Code の Copilot Chat を開く\n"
        f"2. エージェント選択で `{agent}` を選ぶ\n"
        f"3. 下の「プロンプト」以下をすべて貼り付けて実行する\n"
        f"4. 生成物が指定パスに保存されたことを確認し、"
        f"`e2e resume` で次の工程へ進む\n\n"
        f"---\n\n## プロンプト\n\n"
    )
    path.write_text(header + prompt + "\n", encoding="utf-8")
    return path
