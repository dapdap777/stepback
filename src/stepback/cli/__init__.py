"""The ``stepback`` command-line interface."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import click
from rich import box
from rich.console import Console
from rich.table import Table
from rich.text import Text

from .. import __version__
from ..config import resolve_db_path
from ..storage import AmbiguousRunId, RunHasForks, RunNotFound, TraceStore
from . import render


class Ctx:
    def __init__(self, db: Path | None) -> None:
        self.db_path = resolve_db_path(db)
        self.console = Console(highlight=False, soft_wrap=False)

    def open_store(self) -> TraceStore:
        if not self.db_path.exists():
            self.console.print(
                Text.assemble(
                    ("No traces yet ", "bold"),
                    (f"({self.db_path})\n", "dim"),
                    "Record some by wrapping your client:\n\n",
                    ("    import stepback\n", "cyan"),
                    ("    client = stepback.wrap(Anthropic())\n", "cyan"),
                )
            )
            raise SystemExit(1)
        return TraceStore(self.db_path)

    def resolve(self, store: TraceStore, run_id: str | None) -> str:
        if run_id is None:
            latest = store.list_runs(limit=1)
            if not latest:
                raise click.ClickException("no runs recorded yet")
            return latest[0].run.id
        try:
            return store.resolve_run_id(run_id)
        except RunNotFound:
            by_name = store.find_run_by_name(run_id)
            if by_name:
                return by_name
            raise click.ClickException(
                f"no run id or name matches {run_id!r} (see `stepback ls`)"
            ) from None
        except AmbiguousRunId as exc:
            raise click.ClickException(str(exc)) from None


pass_ctx = click.make_pass_decorator(Ctx)


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.option(
    "--db",
    type=click.Path(dir_okay=False, path_type=Path),
    envvar="STEPBACK_DB",
    help="Trace database (default: ./.stepback/traces.db).",
)
@click.version_option(__version__, "-V", "--version", prog_name="stepback")
@click.pass_context
def cli(ctx: click.Context, db: Path | None) -> None:
    """Stepback: a time-travel debugger for AI agents."""
    ctx.obj = Ctx(db)


@cli.command("ls")
@click.option("-n", "--limit", default=20, show_default=True, help="Number of runs to show.")
@click.option("--no-forks", is_flag=True, help="Hide forked (replayed) runs.")
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output.")
@pass_ctx
def ls_command(obj: Ctx, limit: int, no_forks: bool, as_json: bool) -> None:
    """List recorded runs, newest first."""
    with obj.open_store() as store:
        summaries = store.list_runs(limit=limit, include_forks=not no_forks)
        total = store.count_runs()
    if as_json:
        click.echo(json.dumps([s.to_dict() for s in summaries], indent=2))
        return
    if not summaries:
        obj.console.print("No runs recorded yet.", style="dim")
        return
    obj.console.print(render.runs_table(summaries, width=obj.console.width))
    if total > len(summaries):
        obj.console.print(
            f"showing {len(summaries)} of {total} runs · use -n to see more", style="dim"
        )


@cli.command("show")
@click.argument("run_id", required=False)
@click.option("-s", "--step", "step_idx", type=int, help="Show one step in full (by index).")
@click.option("--full", is_flag=True, help="Don't truncate message previews.")
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output.")
@pass_ctx
def show_command(
    obj: Ctx, run_id: str | None, step_idx: int | None, full: bool, as_json: bool
) -> None:
    """Print a run as a step tree.

    RUN_ID is a run id, a unique id prefix, or a run name (latest run with that name).
    Defaults to the most recent run.
    """
    console = obj.console
    with obj.open_store() as store:
        resolved = obj.resolve(store, run_id)
        summary = store.summarize(resolved)
        assert summary is not None
        steps = store.get_lineage(resolved)
        forks = [s for s in (store.summarize(f.id) for f in store.list_forks(resolved)) if s]
        forked_from = None
        if summary.run.parent_run_id:
            parent = store.get_run(summary.run.parent_run_id)
            fork_step = (
                store.get_step(summary.run.forked_from_step_id)
                if summary.run.forked_from_step_id
                else None
            )
            if parent is not None:
                forked_from = (parent, fork_step)

    if as_json:
        payload: dict[str, Any] = summary.to_dict()
        payload["steps"] = [s.to_dict() for s in steps]
        payload["forks"] = [f.to_dict() for f in forks]
        click.echo(json.dumps(payload, indent=2, ensure_ascii=False))
        return

    if step_idx is not None:
        matches = [s for s in steps if s.idx == step_idx]
        if not matches:
            raise click.ClickException(f"run {resolved} has no step #{step_idx}")
        render.step_detail(matches[-1], console)
        return

    console.print(render.run_header(summary, forked_from))
    console.print(render.run_tree(summary, steps, forks, width=console.width, full=full))
    if steps:
        console.print(
            Text.assemble(
                ("  stepback show ", "dim"),
                (resolved, "dim cyan"),
                (" --step N", "dim"),
                ("  for full messages · ", "dim"),
                ("stepback ui", "dim cyan"),
                ("  to browse and replay", "dim"),
            )
        )


@cli.command("rm")
@click.argument("run_ids", nargs=-1, required=True)
@click.option("-y", "--yes", is_flag=True, help="Don't ask for confirmation.")
@click.option("--cascade", is_flag=True, help="Also delete runs forked from these runs.")
@pass_ctx
def rm_command(obj: Ctx, run_ids: tuple[str, ...], yes: bool, cascade: bool) -> None:
    """Delete runs (and their steps)."""
    with obj.open_store() as store:
        resolved = [obj.resolve(store, r) for r in run_ids]
        if not yes:
            click.confirm(f"Delete {len(resolved)} run(s): {', '.join(resolved)}?", abort=True)
        for run_id in resolved:
            try:
                store.delete_run(run_id, cascade_forks=cascade)
            except RunHasForks as exc:
                raise click.ClickException(f"{exc}. Use --cascade to delete them too.") from None
    obj.console.print(f"Deleted {len(resolved)} run(s).", style="green")


@cli.command("prices")
@click.option("-m", "--model", help="Show the price that applies to this model id.")
@pass_ctx
def prices_command(obj: Ctx, model: str | None) -> None:
    """Show the price table used for cost estimates (USD per million tokens)."""
    from ..recorder.pricing import effective_prices, lookup, prices_path

    console = obj.console
    path = prices_path()
    if model:
        price = lookup(model)
        if price is None:
            raise click.ClickException(
                f"no price for {model!r}; add it to {path} (see `stepback prices`)"
            )
        console.print(Text.assemble((model, "bold cyan"), f"  {price}"))
        return
    table = Table(header_style="bold", box=box.SIMPLE_HEAD)
    table.add_column("Model (prefix)", style="cyan")
    for col in ("Input", "Output", "Cache read"):
        table.add_column(col, justify="right")
    table.add_column("Source", style="dim")
    for key, (price, source) in sorted(effective_prices().items()):
        cache = price.cache_read if price.cache_read is not None else price.input * 0.1
        table.add_row(
            key,
            f"${price.input:g}",
            f"${price.output:g}",
            f"${cache:g}",
            source if source == "built-in" else Text(source, style="yellow"),
        )
    console.print(table)
    state = "found" if path.exists() else "not found"
    console.print(
        Text.assemble(
            ("Overrides: ", "dim"),
            (str(path), "cyan"),
            (f" ({state}) · cache writes cost 1.25x input (5m) or 2x (1h)", "dim"),
        )
    )


@cli.command("ui")
@click.option("--host", default="127.0.0.1", show_default=True, help="Interface to bind.")
@click.option(
    "-p", "--port", type=int, default=None, help="Port (default: 7777, or the next free one)."
)
@click.option("--open/--no-open", "open_browser", default=True, help="Open a browser tab.")
@click.option(
    "--allow-origin",
    "allow_origins",
    multiple=True,
    envvar="STEPBACK_ALLOW_ORIGINS",
    help="Extra exact origin allowed to call the API, e.g. a Vite dev server "
    "(http://localhost:5173). Repeatable. No wildcards.",
)
@pass_ctx
def ui_command(
    obj: Ctx, host: str, port: int | None, open_browser: bool, allow_origins: tuple[str, ...]
) -> None:
    """Start the local web UI (http://127.0.0.1:7777)."""
    import threading
    import webbrowser

    import uvicorn

    from ..server import create_app
    from ..server.security import LOOPBACK_HOSTS, is_loopback

    console = obj.console
    origins = [o for value in allow_origins for o in value.replace(",", " ").split()]
    if any("*" in o for o in origins):
        raise click.ClickException("--allow-origin takes exact origins; wildcards are not allowed")
    allowed_hosts = set(LOOPBACK_HOSTS)
    if not is_loopback(host):
        console.print(
            Text.assemble(
                ("Warning: ", "bold red"),
                f"binding to {host} exposes your recorded prompts and responses (and replay, "
                "which spends API credits) to anyone who can reach this address.",
            )
        )
        allowed_hosts.add(host)
    chosen = _pick_port(host, port)
    url_host = "127.0.0.1" if host in ("0.0.0.0", "::") else host
    if ":" in url_host:
        url_host = f"[{url_host}]"
    url = f"http://{url_host}:{chosen}"

    try:
        app = create_app(obj.db_path, allowed_hosts=allowed_hosts, allowed_origins=origins)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from None
    console.print(
        Text.assemble(
            ("Stepback UI ", "bold"),
            (url, "bold cyan underline"),
            (f"  ·  {obj.db_path}", "dim"),
            ("\nPress Ctrl+C to stop.", "dim"),
        )
    )
    if open_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    uvicorn.run(app, host=host, port=chosen, log_level="warning", server_header=False)


def _pick_port(host: str, requested: int | None) -> int:
    """The requested port (must be free), else 7777 or the next free port after it."""
    import socket

    def free(port: int) -> bool:
        family = socket.AF_INET6 if ":" in host else socket.AF_INET
        with socket.socket(family, socket.SOCK_STREAM) as sock:
            # Same option uvicorn uses, so sockets lingering in TIME_WAIT after a restart
            # don't make a free port look busy. A port with a live listener still fails.
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind((host, port))
                sock.listen(1)
            except OSError:
                return False
            return True

    if requested is not None:
        if requested and not free(requested):
            raise click.ClickException(f"port {requested} is already in use")
        return requested
    for port in range(7777, 7797):
        if free(port):
            return port
    raise click.ClickException("no free port between 7777 and 7796; pass --port")


@cli.command("openapi")
def openapi_command() -> None:
    """Print the API's OpenAPI schema (used to generate the frontend's types)."""
    from ..server import openapi_schema

    click.echo(json.dumps(openapi_schema(), indent=2, sort_keys=True))


def main(argv: list[str] | None = None) -> None:
    try:
        cli.main(args=argv, prog_name="stepback")
    except BrokenPipeError:  # e.g. `stepback ls | head`
        sys.exit(0)
