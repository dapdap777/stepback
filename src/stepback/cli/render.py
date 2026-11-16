"""Terminal rendering for ``stepback ls`` / ``stepback show`` (rich).

User content is always added through :class:`rich.text.Text` (never markup strings) so that
brackets in prompts can't be interpreted as rich markup.
"""

from __future__ import annotations

import json
import time
from datetime import datetime
from typing import Any

from rich import box
from rich.console import Console, Group, RenderableType
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich.tree import Tree

from ..storage.digest import error_message
from ..storage.models import Run, RunSummary, Step

STATUS_STYLE = {"ok": "green", "error": "red", "running": "yellow"}
STATUS_ICON = {"ok": "●", "error": "✗", "running": "◌"}
STOP_STYLE = {
    "end_turn": "green",
    "tool_use": "yellow",
    "max_tokens": "red",
    "stop_sequence": "cyan",
    "pause_turn": "magenta",
    "refusal": "red",
}
BAR_WIDTH = 12


# ---------------------------------------------------------------------- formatting
def fmt_ms(ms: float | None) -> str:
    if ms is None:
        return "—"
    if ms < 1000:
        return f"{ms:.0f}ms"
    seconds = ms / 1000
    if seconds < 60:
        return f"{seconds:.2f}s"
    minutes, secs = divmod(int(seconds), 60)
    return f"{minutes}m {secs:02d}s"


def fmt_tokens(n: int | None) -> str:
    return "—" if n is None else f"{n:,}"


def fmt_cost(cost: float | None) -> str:
    if cost is None:
        return "—"
    if cost == 0:
        return "$0"
    if cost < 0.0001:
        return "<$0.0001"
    if cost < 1:
        return f"${cost:.4f}"
    return f"${cost:,.2f}"


def fmt_ago(ts: float, now: float | None = None) -> str:
    delta = max(0.0, (now or time.time()) - ts)
    if delta < 60:
        return f"{int(delta)}s ago"
    if delta < 3600:
        return f"{int(delta // 60)}m ago"
    if delta < 86400:
        return f"{int(delta // 3600)}h ago"
    return f"{int(delta // 86400)}d ago"


def fmt_time(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


def one_line(text: str, limit: int) -> str:
    flat = " ".join(text.split())
    if limit > 1 and len(flat) > limit:
        return flat[: limit - 1].rstrip() + "…"
    return flat


def run_duration(summary: RunSummary) -> float | None:
    run = summary.run
    if run.ended_at is not None:
        return (run.ended_at - run.created_at) * 1000
    return summary.latency_ms or None


def status_text(status: str) -> Text:
    style = STATUS_STYLE.get(status, "white")
    return Text(f"{STATUS_ICON.get(status, '•')} {status}", style=style)


# ---------------------------------------------------------------------- content helpers
def block_text(content: Any) -> str:
    """Plain text of a message ``content`` (string or list of blocks)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "text":
                    parts.append(str(block.get("text", "")))
                elif block.get("type") == "tool_result":
                    parts.append(block_text(block.get("content")))
                elif block.get("type") in ("image", "document"):
                    parts.append(f"[{block.get('type')}]")
            elif isinstance(block, str):
                parts.append(block)
        return " ".join(p for p in parts if p)
    return "" if content is None else str(content)


def new_user_input(step: Step) -> str | None:
    """Text the user added for this step (None if the step only carries tool results)."""
    messages = step.request.get("messages")
    if not isinstance(messages, list) or not messages:
        return None
    last = messages[-1]
    if not isinstance(last, dict) or last.get("role") != "user":
        return None
    content = last.get("content")
    if isinstance(content, list) and any(
        isinstance(b, dict) and b.get("type") == "tool_result" for b in content
    ):
        texts = [
            str(b.get("text", ""))
            for b in content
            if isinstance(b, dict) and b.get("type") == "text"
        ]
        return " ".join(texts) or None
    text = block_text(content)
    return text or None


def format_tool_input(value: Any, limit: int) -> str:
    if isinstance(value, dict):
        args = ", ".join(f"{k}={json.dumps(v, ensure_ascii=False)}" for k, v in value.items())
        return one_line(args, limit)
    return one_line(json.dumps(value, ensure_ascii=False), limit)


# ---------------------------------------------------------------------- ls
def runs_table(summaries: list[RunSummary], now: float | None = None, width: int = 120) -> Table:
    """Run list sized to ``width``: optional columns drop out, then Name shrinks to fit."""
    optional = {"started": width >= 100, "duration": width >= 90, "model": width >= 140}
    name_width = 30
    table = _runs_table(summaries, now, optional, name_width)
    measure = Console(width=10_000)
    while name_width > 10:
        overflow = measure.measure(table).maximum - width
        if overflow <= 0:
            break
        name_width = max(10, name_width - max(1, overflow))
        table = _runs_table(summaries, now, optional, name_width)
    return table


def _runs_table(
    summaries: list[RunSummary], now: float | None, optional: dict[str, bool], name_width: int
) -> Table:
    table = Table(box=box.SIMPLE_HEAD, header_style="bold", pad_edge=False, expand=False)
    table.add_column("Run", style="cyan", no_wrap=True)
    table.add_column("Name", no_wrap=True, overflow="ellipsis", max_width=name_width)
    if optional["started"]:
        table.add_column("Started", style="dim", no_wrap=True)
    table.add_column("Status", no_wrap=True)
    table.add_column("Steps", justify="right", no_wrap=True)
    table.add_column("Tokens in/out", justify="right", no_wrap=True)
    table.add_column("Cost", justify="right", style="green", no_wrap=True)
    if optional["duration"]:
        table.add_column("Duration", justify="right", no_wrap=True)
    if optional["model"]:
        table.add_column("Model", style="dim", no_wrap=True)
    for s in summaries:
        name = Text(s.run.name)
        if s.run.parent_run_id:
            name = Text.assemble(("↳ ", "magenta"), name)
        if s.fork_count:
            name.append(f" +{s.fork_count}⟲", style="magenta")
        steps = Text(str(s.step_count))
        if s.error_count:
            steps.append(f" {s.error_count}✗", style="red")
        row: list[RenderableType] = [s.run.id, name]
        if optional["started"]:
            row.append(fmt_ago(s.run.created_at, now))
        row += [
            status_text(s.run.status),
            steps,
            f"{fmt_tokens(s.input_tokens)} / {fmt_tokens(s.output_tokens)}",
            fmt_cost(s.cost_usd),
        ]
        if optional["duration"]:
            row.append(fmt_ms(run_duration(s)))
        if optional["model"]:
            row.append(", ".join(s.models) or "—")
        table.add_row(*row)
    return table


# ---------------------------------------------------------------------- show
def run_header(summary: RunSummary, forked_from: tuple[Run, Step | None] | None = None) -> Panel:
    run = summary.run
    grid = Table.grid(padding=(0, 3))
    for _ in range(3):
        grid.add_column()

    def pair(label: str, value: Text | str) -> Text:
        text = Text()
        text.append(f"{label} ", style="dim")
        text.append_text(value if isinstance(value, Text) else Text(value))
        return text

    tokens = Text(fmt_tokens(summary.input_tokens), style="bold")
    tokens.append(" in · ", style="dim")
    tokens.append(fmt_tokens(summary.output_tokens), style="bold")
    tokens.append(" out", style="dim")
    grid.add_row(
        pair("status", status_text(run.status)),
        pair("started", f"{fmt_time(run.created_at)} ({fmt_ago(run.created_at)})"),
        pair("duration", Text(fmt_ms(run_duration(summary)), style="bold")),
    )
    grid.add_row(
        pair("steps", Text(str(summary.step_count), style="bold")),
        pair("tokens", tokens),
        pair("cost", Text(fmt_cost(summary.cost_usd), style="bold green")),
    )
    rows: list[RenderableType] = [grid]
    if summary.models:
        rows.append(pair("models", Text(", ".join(summary.models), style="cyan")))
    if forked_from is not None:
        parent, step = forked_from
        line = Text()
        line.append("forked from ", style="dim")
        line.append(parent.id, style="magenta")
        if step is not None:
            line.append(f" at step #{step.idx}", style="magenta")
        rows.append(line)
    title = Text.assemble((f" {run.name}", "bold"), (f"  {run.id} ", "dim cyan"))
    return Panel(Group(*rows), title=title, title_align="left", border_style="cyan", padding=(0, 1))


def latency_bar(ms: float | None, max_ms: float) -> Text:
    if ms is None or max_ms <= 0:
        return Text("·" * BAR_WIDTH, style="dim")
    filled = max(1, round(BAR_WIDTH * ms / max_ms))
    ratio = ms / max_ms
    color = "green" if ratio < 0.4 else "yellow" if ratio < 0.8 else "red"
    bar = Text("█" * filled, style=color)
    bar.append("░" * (BAR_WIDTH - filled), style="bright_black")
    return bar


def step_label(
    step: Step,
    max_ms: float,
    inherited: bool,
    branch_from: int | None,
    show_ttft: bool = False,
    model_width: int = 17,
) -> Text:
    """One aligned line per step: index, model, latency bar, timings, tokens, cost, outcome."""
    label = Text()
    label.append(f"#{step.idx:<2} ", style="bold cyan" if not inherited else "dim cyan")
    label.append(f"{(step.model or step.provider):<{model_width}} ", style="white")
    label.append_text(latency_bar(step.latency_ms, max_ms))
    label.append(f" {fmt_ms(step.latency_ms):>7}", style="bold")
    if show_ttft:
        ttft = f"ttft {fmt_ms(step.ttft_ms)}" if step.ttft_ms is not None else ""
        label.append(f"  {ttft:<12}", style="dim")
    tokens = f"{fmt_tokens(step.input_tokens)} → {fmt_tokens(step.output_tokens)}"
    label.append(f"  {tokens:>15}", style="")
    label.append(" tok", style="dim")
    label.append(f"  {fmt_cost(step.cost_usd):>8}", style="green")
    if step.error:
        err = step.error
        code = f" {err['status_code']}" if err.get("status_code") else ""
        label.append(f"  ✗ {err.get('type', 'error')}{code}", style="bold red")
    elif step.stop_reason:
        label.append(f"  {step.stop_reason}", style=STOP_STYLE.get(step.stop_reason, "white"))
    if step.incomplete:
        label.append("  incomplete", style="red")
    if step.is_replay:
        label.append("  ⟲ replay", style="bold magenta")
    if branch_from is not None:
        label.append(f"  ↳ from #{branch_from}", style="magenta")
    if inherited:
        label.append("  (inherited)", style="dim italic")
        label.stylize("dim")
    return label


def _row(kind: str, style: str, body: Text | str, body_style: str = "") -> Text:
    text = Text()
    text.append(f"{kind:<10}", style=style)
    if isinstance(body, Text):
        text.append_text(body)
    else:
        text.append(body, style=body_style)
    return text


def add_step_details(node: Tree, step: Step, width: int, full: bool) -> None:
    limit = 0 if full else max(30, width - 30)

    def clip(text: str) -> str:
        return " ".join(text.split()) if full else one_line(text, limit)

    user_text = new_user_input(step)
    if user_text:
        node.add(_row("user", "bold blue", clip(user_text)))

    if step.error:
        node.add(_row("error", "bold red", clip(error_message(step.error)), "red"))

    content = (step.response or {}).get("content") or []
    results = {r.get("tool_use_id"): r for r in step.tool_results}
    for block in content:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "thinking":
            thought = str(block.get("thinking") or "")
            body = clip(thought) if thought else "(hidden)"
            node.add(_row("thinking", "magenta", body, "dim italic"))
        elif btype == "redacted_thinking":
            node.add(_row("thinking", "magenta", "(redacted)", "dim italic"))
        elif btype == "text":
            text = str(block.get("text") or "")
            if text.strip():
                node.add(_row("assistant", "bold green", clip(text)))
        elif btype in ("tool_use", "server_tool_use", "mcp_tool_use"):
            call = Text()
            call.append(str(block.get("name")), style="bold yellow")
            call.append("(", style="dim")
            call.append(format_tool_input(block.get("input"), limit or 10_000), style="yellow")
            call.append(")", style="dim")
            tool_node = node.add(_row("tool", "bold yellow", call))
            result = results.get(block.get("id"))
            if result is not None:
                is_error = bool(result.get("is_error"))
                body = clip(block_text(result.get("content"))) or "(empty)"
                tool_node.add(
                    _row(
                        "✗ error" if is_error else "→ result",
                        "red" if is_error else "green",
                        body,
                        "red" if is_error else "",
                    )
                )
            elif btype == "tool_use":
                tool_node.add(Text("no result recorded", style="dim italic"))


def run_tree(
    summary: RunSummary,
    steps: list[Step],
    forks: list[RunSummary],
    width: int = 120,
    full: bool = False,
) -> Tree:
    run = summary.run
    root_label = Text()
    root_label.append("steps", style="bold")
    root_label.append(f"  {len(steps)}", style="dim")
    inherited_count = sum(1 for s in steps if s.run_id != run.id)
    if inherited_count:
        root_label.append(f" ({inherited_count} inherited from the parent run)", style="dim")
    tree = Tree(root_label, guide_style="bright_black")
    max_ms = max((s.latency_ms or 0 for s in steps), default=0)
    show_ttft = any(s.ttft_ms is not None for s in steps)
    model_width = max((len(s.model or s.provider) for s in steps), default=10)
    idx_by_id = {s.id: s.idx for s in steps}
    forks_by_step: dict[str, list[RunSummary]] = {}
    for fork in forks:
        if fork.run.forked_from_step_id:
            forks_by_step.setdefault(fork.run.forked_from_step_id, []).append(fork)

    previous: Step | None = None
    for step in steps:
        branch_from = None
        if (
            previous is not None
            and step.parent_step_id
            and step.parent_step_id != previous.id
            and step.parent_step_id in idx_by_id
        ):
            branch_from = idx_by_id[step.parent_step_id]
        inherited = step.run_id != run.id
        node = tree.add(step_label(step, max_ms, inherited, branch_from, show_ttft, model_width))
        add_step_details(node, step, width, full)
        for fork in forks_by_step.get(step.id, []):
            line = Text()
            line.append("⟲ fork ", style="bold magenta")
            line.append(fork.run.id, style="magenta")
            line.append(f"  {fork.run.name}", style="")
            line.append(
                f"  · {fork.step_count} step{'s' if fork.step_count != 1 else ''}"
                f" · {fmt_cost(fork.cost_usd)}",
                style="dim",
            )
            node.add(line)
        previous = step
    if not steps:
        tree.add(Text("no steps recorded", style="dim italic"))
    return tree


def step_detail(step: Step, console: Console) -> None:
    """Full, untruncated view of one step: params, system prompt, messages, response."""
    req = dict(step.request)
    messages = req.pop("messages", []) or []
    system = req.pop("system", None)
    tools = req.pop("tools", None)

    params = Table.grid(padding=(0, 2))
    params.add_column(style="dim")
    params.add_column()
    for key, value in req.items():
        params.add_row(key, Text(json.dumps(value, ensure_ascii=False)))
    if tools:
        params.add_row("tools", Text(", ".join(str(t.get("name")) for t in tools), style="yellow"))
    for label, value in (
        ("latency", fmt_ms(step.latency_ms)),
        ("ttft", fmt_ms(step.ttft_ms)),
        ("tokens", f"{fmt_tokens(step.input_tokens)} in · {fmt_tokens(step.output_tokens)} out"),
        (
            "cache",
            f"{fmt_tokens(step.cache_read_tokens)} read · "
            f"{fmt_tokens(step.cache_write_tokens)} write",
        ),
        ("cost", fmt_cost(step.cost_usd)),
    ):
        params.add_row(label, Text(value))
    title = Text()
    title.append(f"step #{step.idx}", style="bold")
    title.append(f"  {step.id}  {step.operation or ''}", style="dim")
    console.print(Panel(params, title=title, title_align="left", border_style="cyan"))

    if system:
        text = system if isinstance(system, str) else block_text(system)
        console.print(Panel(Text(text), title="system", title_align="left", border_style="magenta"))
    role_style = {"user": "blue", "assistant": "green", "system": "magenta"}
    for i, message in enumerate(messages):
        role = str(message.get("role", "?")) if isinstance(message, dict) else "?"
        body = _content_renderable(message.get("content") if isinstance(message, dict) else message)
        console.print(
            Panel(
                body,
                title=f"[{i}] {role}",
                title_align="left",
                border_style=role_style.get(role, "white"),
            )
        )
    if step.response is not None:
        console.print(
            Panel(
                _content_renderable(step.response.get("content")),
                title=f"response · {step.stop_reason or ''}",
                title_align="left",
                border_style="bold green",
            )
        )
    if step.error:
        console.print(
            Panel(
                Text(json.dumps(step.error, indent=2, ensure_ascii=False)),
                title="error",
                border_style="red",
            )
        )


def _content_renderable(content: Any) -> RenderableType:
    if isinstance(content, str):
        return Text(content)
    if not isinstance(content, list):
        return Text(json.dumps(content, indent=2, ensure_ascii=False))
    parts: list[RenderableType] = []
    for block in content:
        if not isinstance(block, dict):
            parts.append(Text(str(block)))
            continue
        btype = block.get("type")
        if btype == "text":
            parts.append(Text(str(block.get("text", ""))))
        elif btype == "thinking":
            parts.append(Text(f"💭 {block.get('thinking') or '(hidden)'}", style="dim italic"))
        elif btype in ("tool_use", "server_tool_use", "mcp_tool_use"):
            head = Text()
            head.append(f"⚙ {block.get('name')} ", style="bold yellow")
            head.append(f"{block.get('id')}", style="dim")
            parts.append(head)
            parts.append(Text(json.dumps(block.get("input"), indent=2, ensure_ascii=False)))
        elif btype == "tool_result":
            style = "red" if block.get("is_error") else "green"
            head = Text()
            head.append("→ result ", style=f"bold {style}")
            head.append(str(block.get("tool_use_id")), style="dim")
            parts.append(head)
            parts.append(Text(block_text(block.get("content"))))
        else:
            parts.append(Text(json.dumps(block, indent=2, ensure_ascii=False), style="dim"))
    return Group(*parts)
