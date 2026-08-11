"""プロンプト資産から Copilot のカスタムエージェント定義を生成する。

開発端末では GitHub Copilot を使うため、各工程のエージェントは
`.github/agents/<name>.agent.md` として置く必要がある。プロンプト本体を
prompts/ に単一ソースで持ち、frontmatter だけをここで合成することで、
VS Code のチャットから使う場合と CLI から自動実行する場合で内容がずれないようにする。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

AGENTS_DIR = Path(".github/agents")
COMMON_PROMPT = "_common"


@dataclass
class AgentDefinition:
    name: str
    prompt: str
    description: str
    tools: list[str] = field(default_factory=list)
    model: str = ""
    agents: list[str] = field(default_factory=list)
    handoffs: list[dict[str, Any]] = field(default_factory=list)


def load_definitions(path: Path) -> tuple[list[AgentDefinition], AgentDefinition]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    stage_agents = [
        AgentDefinition(
            name=str(a["name"]),
            prompt=str(a["prompt"]),
            description=str(a["description"]),
            tools=list(a.get("tools", [])),
            model=str(a.get("model", "")),
        )
        for a in raw["agents"]
    ]
    orch_raw = raw["orchestrator"]
    orchestrator = AgentDefinition(
        name=str(orch_raw["name"]),
        prompt=str(orch_raw["prompt"]),
        description=str(orch_raw["description"]),
        tools=list(orch_raw.get("tools", [])),
        model=str(orch_raw.get("model", "")),
        # オーケストレーターからはすべての工程エージェントを呼べるようにする。
        agents=[a.name for a in stage_agents],
        handoffs=list(orch_raw.get("handoffs", [])),
    )
    return stage_agents, orchestrator


def _yaml_list(values: list[str]) -> str:
    return "[" + ", ".join(f"'{v}'" for v in values) + "]"


def render_agent_file(
    definition: AgentDefinition, body: str, common: str
) -> str:
    lines = ["---", f"name: {definition.name}", f"description: {definition.description}"]
    if definition.tools:
        lines.append(f"tools: {_yaml_list(definition.tools)}")
    if definition.agents:
        lines.append(f"agents: {_yaml_list(definition.agents)}")
    if definition.model:
        lines.append(f"model: {definition.model}")
    if definition.handoffs:
        lines.append("handoffs:")
        for handoff in definition.handoffs:
            lines.append(f"  - label: {handoff['label']}")
            lines.append(f"    agent: {handoff['agent']}")
            lines.append(f"    prompt: {handoff['prompt']}")
            lines.append("    send: false")
    lines.append("---")
    lines.append("")
    lines.append(
        "<!-- このファイルは自動生成されています。編集は prompts/"
        f"{definition.prompt}.md に対して行い、`e2e sync-agents` を実行してください。 -->"
    )
    lines.append("")
    lines.append(body.strip())
    lines.append("")
    lines.append(common.strip())
    lines.append("")
    return "\n".join(lines)


def sync(root: Path, output_dir: Path | None = None) -> list[Path]:
    """agents.yaml と prompts/ からエージェント定義を再生成する。

    既定ではハーネス直下の .github/agents/ に出力する。ハーネスをアプリ
    リポジトリに同梱する場合は、アプリリポジトリ直下の .github/agents/ を
    output_dir に指定する。そこに置けば VS Code が既定で探索するため、
    chat.agentFilesLocations の設定が不要になる。
    """
    definitions_path = root / "agents.yaml"
    prompts_dir = root / "prompts"
    output_dir = output_dir or (root / AGENTS_DIR)
    output_dir.mkdir(parents=True, exist_ok=True)

    common = (prompts_dir / f"{COMMON_PROMPT}.md").read_text(encoding="utf-8")
    stage_agents, orchestrator = load_definitions(definitions_path)

    written: list[Path] = []
    for definition in [*stage_agents, orchestrator]:
        body_path = prompts_dir / f"{definition.prompt}.md"
        if not body_path.is_file():
            raise FileNotFoundError(f"プロンプトがありません: {body_path}")
        body = body_path.read_text(encoding="utf-8")
        # オーケストレーターは工程を担当しないため共通規約は付けない。
        common_text = "" if definition is orchestrator else common
        content = render_agent_file(definition, body, common_text)
        path = output_dir / f"{definition.name}.agent.md"
        path.write_text(content, encoding="utf-8")
        written.append(path)
    return written


def agent_body(root: Path, prompt_name: str) -> str:
    """CLI から Copilot を呼ぶときに使うエージェント本文。"""
    prompts_dir = root / "prompts"
    body = (prompts_dir / f"{prompt_name}.md").read_text(encoding="utf-8")
    common = (prompts_dir / f"{COMMON_PROMPT}.md").read_text(encoding="utf-8")
    return f"{body}\n\n{common}"
