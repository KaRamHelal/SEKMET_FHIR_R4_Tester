"""`sekmet` command line interface."""
from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path
from typing import Optional

import httpx
import typer

from .config import load_settings

app = typer.Typer(help="SEKMET FHIR R4 Tester - micro HIS for testing real HIS FHIR interfaces", no_args_is_help=True)
scenario_app = typer.Typer(help="Run and list test scenarios", no_args_is_help=True)
peers_app = typer.Typer(help="Inspect and test configured peers", no_args_is_help=True)
wf_app = typer.Typer(help="Run individual hospital workflows", no_args_is_help=True)
keys_app = typer.Typer(help="SMART Backend Services keys", no_args_is_help=True)
validator_app = typer.Typer(help="HL7 FHIR validator integration", no_args_is_help=True)
subs_app = typer.Typer(help="Subscriptions on peers", no_args_is_help=True)
for sub, name in ((scenario_app, "scenario"), (peers_app, "peers"), (wf_app, "workflow"), (keys_app, "keys"),
                  (validator_app, "validator"), (subs_app, "subscriptions")):
    app.add_typer(sub, name=name)

ConfigOpt = typer.Option(None, "--config", "-c", help="settings.yaml path (default: $SEKMET_CONFIG or ./settings.yaml)")


def _ctx(config: str | None):
    from .context import AppContext
    return AppContext(load_settings(config))


def _server_up(base_url: str) -> bool:
    try:
        return httpx.get(f"{base_url.rstrip('/')}/metadata", timeout=2).status_code == 200
    except httpx.HTTPError:
        return False


def _embedded_server(config: str | None):
    """Start the app in-process (background thread) and return its context."""
    import uvicorn
    from .main import create_app

    settings = load_settings(config)
    fastapi_app = create_app(settings)
    server = uvicorn.Server(uvicorn.Config(fastapi_app, host=settings.host, port=settings.port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.1)
    if not server.started:
        typer.secho("Embedded server failed to start", fg="red")
        raise typer.Exit(2)
    typer.secho(f"Embedded SEKMET server listening on {settings.base_url}", fg="cyan")
    return fastapi_app.state.ctx, server


@app.command()
def serve(config: Optional[str] = ConfigOpt, host: Optional[str] = None, port: Optional[int] = None,
          reload: bool = False):
    """Run the micro HIS (FHIR server + web console)."""
    import os
    import uvicorn

    if config:
        os.environ["SEKMET_CONFIG"] = config
    s = load_settings(config)
    typer.secho(f"SEKMET FHIR base: {s.base_url}   console: {s.root_url}/ui", fg="green")
    uvicorn.run("sekmet.main:app_factory", factory=True, host=host or s.host, port=port or s.port, reload=reload,
                log_level="info")


@scenario_app.command("list")
def scenario_list():
    """List scenarios (./scenarios and the built-in library)."""
    from .scenarios.runner import list_scenarios
    for s in list_scenarios():
        tags = ",".join(s["tags"])
        typer.echo(f"{s['id']:<34} {s['steps']:>3} steps  [{tags}]  {s['name']}")


@scenario_app.command("run")
def scenario_run(names: list[str] = typer.Argument(..., help="scenario ids/paths, or 'all'"),
                 peer: str = typer.Option("self", "--peer", "-p"),
                 tag: Optional[str] = typer.Option(None, help="with 'all': only scenarios with this tag"),
                 report_dir: str = typer.Option("reports", help="directory for JSON/JUnit/HTML reports"),
                 serve: Optional[bool] = typer.Option(None, "--serve/--no-serve",
                                                      help="start an embedded server (default: when not running)"),
                 config: Optional[str] = ConfigOpt):
    """Run scenarios against a peer and write reports. Exit code 1 if anything failed."""
    from .scenarios.report import write_reports
    from .scenarios.runner import ScenarioRunner, list_scenarios

    settings = load_settings(config)
    server = None
    if serve or (serve is None and not _server_up(settings.base_url)):
        ctx, server = _embedded_server(config)
    else:
        ctx = _ctx(config)
    if names == ["all"]:
        names = [s["id"] for s in list_scenarios() if not tag or tag in s["tags"]]
    runner = ScenarioRunner(ctx, peer)
    results = []
    for n in names:
        typer.secho(f"\n▶ {n}  (peer: {peer})", bold=True)
        try:
            r = runner.run(n)
        except FileNotFoundError as e:
            typer.secho(str(e), fg="red")
            continue
        for s in r.steps:
            color = {"passed": "green", "warning": "yellow", "failed": "red", "error": "red", "skipped": "blue"}[s.status]
            typer.secho(f"  {s.status.upper():<7}", fg=color, nl=False)
            typer.echo(f" {s.index:>2}. {s.name}  ({s.duration_ms:.0f} ms)")
            if s.status in ("failed", "error", "warning", "skipped") and s.message:
                typer.secho(f"           {s.message[:1500]}", fg="red" if s.status in ("failed", "error") else "yellow")
        typer.secho(f"  => {r.status.upper()} in {r.duration_ms / 1000:.1f}s  run={r.run_id}",
                    fg="green" if r.status == "passed" else "yellow" if r.status in ("warning", "skipped") else "red")
        results.append(r.to_dict())
    stamp = time.strftime("%Y%m%d-%H%M%S")
    paths = write_reports(results, Path(report_dir) / f"{stamp}-{peer}")
    passed = sum(r["status"] in ("passed", "warning", "skipped") for r in results)
    typer.secho(f"\n{passed}/{len(results)} scenarios passed. Reports: {paths['html']}", bold=True)
    if server:
        server.should_exit = True
    raise typer.Exit(0 if passed == len(results) else 1)


@peers_app.command("list")
def peers_list(config: Optional[str] = ConfigOpt):
    s = load_settings(config)
    for name in s.peer_names():
        p = s.peer(name)
        typer.echo(f"{name:<16} {p.base_url:<50} auth={p.auth.type:<7} mode={p.mode}")


@peers_app.command("test")
def peers_test(name: str, config: Optional[str] = ConfigOpt):
    """Fetch the peer's CapabilityStatement (and a token when SMART) and summarise what it supports."""
    from .client.peer import PeerError
    from .ui.routes import peer_summary
    ctx = _ctx(config)
    try:
        info = peer_summary(ctx, name)
    except (PeerError, KeyError) as e:
        typer.secho(f"FAILED: {e}", fg="red")
        raise typer.Exit(1)
    typer.echo(json.dumps(info, indent=2))


@wf_app.command("list")
def wf_list():
    from .workflows import load_all
    from .workflows.registry import REGISTRY
    load_all()
    for k, w in sorted(REGISTRY.items()):
        params = ", ".join(f"{p.name}{'*' if p.required else ''}" for p in w.params)
        typer.echo(f"{k:<28} {w.title}  ({params})")


@wf_app.command("run")
def wf_run(key: str, target: str = typer.Option("local", "--target", "-t",
                                                 help="local | <peer> | rest:<peer> | messaging:<peer>"),
           arg: list[str] = typer.Option([], "--arg", "-a", help="name=value (repeatable)"),
           config: Optional[str] = ConfigOpt):
    """Run one workflow and print the resulting resources as JSON."""
    from .workflows.registry import run_workflow
    ctx = _ctx(config)
    args = dict(a.split("=", 1) for a in arg)
    out = run_workflow(ctx, key, target, args)
    typer.echo(json.dumps(out, indent=2))


@app.command()
def seed(count: int = 5, target: str = typer.Option("local", "--target", "-t"), admit: bool = True,
         config: Optional[str] = ConfigOpt):
    """Create facility master data and N patients (optionally admitted) on a target."""
    from .workflows.registry import run_workflow
    ctx = _ctx(config)
    run_workflow(ctx, "adt.facility", target)
    for i in range(count):
        reg = run_workflow(ctx, "adt.register_patient", target)
        line = f"Patient/{reg['patient']['id']} {reg['mrn']}"
        if admit and i % 2 == 0:
            adm = run_workflow(ctx, "adt.admit", target, {"patient": reg["patient"]["id"]})
            line += f"  admitted Encounter/{adm['encounter']['id']}"
        typer.echo(line)


@keys_app.command("generate")
def keys_generate(out: str = "keys/sekmet_private.pem", alg: str = "RS384", force: bool = False):
    """Generate a private key for SMART Backend Services client assertions and print its public JWKS."""
    from .auth.keys import generate_private_key, public_jwk, save_private_key
    if Path(out).exists() and not force:
        typer.secho(f"{out} exists (use --force to overwrite)", fg="yellow")
        raise typer.Exit(1)
    key = generate_private_key(alg)
    save_private_key(key, out)
    jwks = {"keys": [public_jwk(key, alg)]}
    Path(out).with_suffix(".jwks.json").write_text(json.dumps(jwks, indent=2))
    typer.echo(f"Private key: {out}\nPublic JWKS: {Path(out).with_suffix('.jwks.json')}\n")
    typer.echo(json.dumps(jwks, indent=2))


@app.command()
def validate(file: str, conformance: bool = typer.Option(False, help="also run the HL7 validator jar"),
             profile: Optional[str] = None, config: Optional[str] = ConfigOpt):
    """Validate a FHIR JSON resource file."""
    from .fhir.validation import has_errors, validate as do_validate
    s = load_settings(config)
    oo = do_validate(json.loads(Path(file).read_text()), s, conformance=conformance, profile=profile)
    for i in oo.get("issue", []):
        color = {"error": "red", "fatal": "red", "warning": "yellow"}.get(i.get("severity"), None)
        typer.secho(f"{i.get('severity'):<11} {i.get('code'):<14} {i.get('diagnostics', '')}", fg=color)
    raise typer.Exit(1 if has_errors(oo.get("issue", [])) else 0)


@validator_app.command("download")
def validator_download(out: str = "tools/validator_cli.jar"):
    """Download the official HL7 validator_cli.jar."""
    from .fhir.validation import VALIDATOR_URL
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    typer.echo(f"Downloading {VALIDATOR_URL} ...")
    with httpx.stream("GET", VALIDATOR_URL, follow_redirects=True, timeout=300) as r:
        r.raise_for_status()
        with open(out, "wb") as f:
            for chunk in r.iter_bytes():
                f.write(chunk)
    typer.echo(f"Saved to {out}. Set validation.validator_jar: {out} in settings.yaml")


@subs_app.command("register")
def subs_register(peer: str, config: Optional[str] = ConfigOpt):
    """Create the Subscriptions listed under peers.<peer>.subscribe on the peer."""
    from .subscriptions.engine import register_remote
    ctx = _ctx(config)
    typer.echo(json.dumps(register_remote(ctx, peer), indent=2, default=str))


@app.command()
def reset(yes: bool = typer.Option(False, "--yes", help="confirm"), config: Optional[str] = ConfigOpt):
    """Delete all local resources, traffic and run history."""
    if not yes:
        typer.confirm("Wipe the local SEKMET database?", abort=True)
    ctx = _ctx(config)
    ctx.store.reset()
    typer.echo("Local database cleared.")


@app.command()
def reindex(config: Optional[str] = ConfigOpt):
    """Rebuild the search index (after changing search parameter definitions)."""
    ctx = _ctx(config)
    typer.echo(f"Reindexed {ctx.store.reindex_all()} resources")


def main():  # pragma: no cover
    app()


if __name__ == "__main__":  # pragma: no cover
    sys.exit(app())
