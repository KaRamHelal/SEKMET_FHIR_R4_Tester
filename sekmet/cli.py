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
ts_app = typer.Typer(help="Run FHIR TestScripts, export runs as TestScripts", no_args_is_help=True)
load_app = typer.Typer(help="Load / concurrency testing with scenarios as user journeys", no_args_is_help=True)
bulk_app = typer.Typer(help="FHIR Bulk Data ($export) against a peer", no_args_is_help=True)
for sub, name in ((scenario_app, "scenario"), (peers_app, "peers"), (wf_app, "workflow"), (keys_app, "keys"),
                  (validator_app, "validator"), (subs_app, "subscriptions"), (ts_app, "testscript"),
                  (load_app, "load"), (bulk_app, "bulk")):
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
    tls = ({"ssl_certfile": settings.tls_certfile, "ssl_keyfile": settings.tls_keyfile}
           if settings.tls_certfile and settings.tls_keyfile else {})
    server = uvicorn.Server(uvicorn.Config(fastapi_app, host=settings.host, port=settings.port, log_level="warning",
                                           **tls))
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
    tls = {"ssl_certfile": s.tls_certfile, "ssl_keyfile": s.tls_keyfile} if s.tls_certfile and s.tls_keyfile else {}
    if tls and not s.base_url.startswith("https://"):
        typer.secho("TLS is configured but base_url is not https://; links and discovery will be wrong", fg="yellow")
    uvicorn.run("sekmet.main:app_factory", factory=True, host=host or s.host, port=port or s.port, reload=reload,
                log_level="info", **tls)


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
                 var: list[str] = typer.Option([], "--var", help="scenario variable override name=value (repeatable)"),
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
            r = runner.run(n, dict(v.split("=", 1) for v in var))
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


def _print_result(r) -> None:
    for s in r.steps:
        color = {"passed": "green", "warning": "yellow", "failed": "red", "error": "red", "skipped": "blue"}[s.status]
        typer.secho(f"  {s.status.upper():<7}", fg=color, nl=False)
        typer.echo(f" {s.index:>2}. {s.name[:110]}")
        if s.status in ("failed", "error", "warning") and s.message:
            typer.secho(f"           {s.message[:800]}", fg="red" if s.status in ("failed", "error") else "yellow")
    typer.secho(f"  => {r.status.upper()} in {r.duration_ms / 1000:.1f}s  run={r.run_id}",
                fg="green" if r.status == "passed" else "yellow" if r.status == "warning" else "red")


@ts_app.command("run")
def testscript_run(files: list[str] = typer.Argument(..., help="TestScript files (.json/.xml) or directories"),
                   peer: list[str] = typer.Option(["self"], "--peer", "-p",
                                                  help="peer per TestScript destination (repeat for multi-system)"),
                   fixtures: list[str] = typer.Option([], "--fixtures", "-f", help="directories with fixture files"),
                   fixture_base: str = typer.Option("https://hl7.org/fhir/R4",
                                                    help="URL base for Type/id fixtures ('' to disable downloads)"),
                   var: list[str] = typer.Option([], "--var", help="variable override name=value"),
                   report_dir: str = typer.Option("reports"), config: Optional[str] = ConfigOpt):
    """Execute FHIR TestScripts; writes the usual reports plus one TestReport per script."""
    from .scenarios.report import write_reports
    from .scenarios.testscript import TestScriptRunner, build_test_report, load_testscript
    settings = load_settings(config)
    server = None
    if "self" in peer and not _server_up(settings.base_url):
        ctx, server = _embedded_server(config)
    else:
        ctx = _ctx(config)
    paths: list[Path] = []
    for f in files:
        p = Path(f)
        paths += sorted(x for x in p.iterdir() if x.suffix in (".json", ".xml")) if p.is_dir() else [p]
    fx_dirs = [*fixtures, *{str(p.parent) for p in paths}]
    runner = TestScriptRunner(ctx, peer, fx_dirs, fixture_base or None, dict(v.split("=", 1) for v in var))
    results, reports = [], []
    for p in paths:
        try:
            ts = load_testscript(p)
        except (ValueError, json.JSONDecodeError) as e:
            typer.secho(f"skip {p}: {e}", fg="yellow")
            continue
        typer.secho(f"\n▶ {p.name}: {ts.get('title') or ts.get('name')}  (peers: {', '.join(peer)})", bold=True)
        r = runner.run(ts)
        _print_result(r)
        results.append(r.to_dict())
        reports.append((p.stem, build_test_report(r, ts.get("url"))))
    out = Path(report_dir) / f"{time.strftime('%Y%m%d-%H%M%S')}-testscript-{'-'.join(peer)}"
    written = write_reports(results, out)
    for stem, rep in reports:
        (out / f"TestReport-{stem}.json").write_text(json.dumps(rep, indent=2))
    ok = sum(r["status"] in ("passed", "warning") for r in results)
    typer.secho(f"\n{ok}/{len(results)} TestScripts passed. Reports + TestReports: {written['html'].rsplit('/', 1)[0]}",
                bold=True)
    if server:
        server.should_exit = True
    raise typer.Exit(0 if ok == len(results) else 1)


@ts_app.command("export")
def testscript_export(run_id: str, out: Optional[str] = None, config: Optional[str] = ConfigOpt):
    """Export a recorded scenario run as a replayable TestScript (requests + response-code asserts)."""
    from .scenarios.runner import load_run
    from .scenarios.testscript import export_run
    ctx = _ctx(config)
    run = load_run(ctx.store, run_id)
    if not run:
        typer.secho(f"No run {run_id}", fg="red")
        raise typer.Exit(1)
    peer_base = ctx.settings.peer(run["peer"].split(",")[0]).base_url
    ts = export_run(ctx, run, peer_base)
    target = Path(out or f"reports/TestScript-{run_id}.json")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(ts, indent=2))
    n_ops = sum("operation" in a for a in ts["test"][0]["action"])
    typer.echo(f"Wrote {target}: {n_ops} operations, {len(ts['variable']) - 1} id variables, "
               f"{len(ts['contained'])} fixtures")


@load_app.command("run")
def load_run(scenario: str = typer.Argument(..., help="scenario id/path used as the user journey"),
             peer: str = typer.Option("self", "--peer", "-p"),
             users: int = typer.Option(5, "--users", "-u", help="concurrent virtual users"),
             duration: float = typer.Option(30, "--duration", "-d", help="seconds"),
             iterations: Optional[int] = typer.Option(None, help="stop after this many journeys in total"),
             ramp: float = typer.Option(0, help="seconds to start all users"),
             think_ms: int = typer.Option(0, help="pause per user between journeys"),
             var: list[str] = typer.Option([], "--var", help="scenario variable override name=value"),
             log_traffic: bool = typer.Option(False, help="also log every request/response body (slower, big DB)"),
             max_error_rate: Optional[float] = typer.Option(None, help="gate: max % of 5xx/transport errors"),
             max_p95: Optional[float] = typer.Option(None, help="gate: max overall p95 latency (ms)"),
             max_iteration_failure: Optional[float] = typer.Option(None, help="gate: max % failed journeys"),
             i_own_this_system: bool = typer.Option(False, "--i-own-this-system",
                                                    help="required for non-local peers: you are allowed to load it"),
             report_dir: str = typer.Option("reports"), config: Optional[str] = ConfigOpt):
    """Run a scenario as a load test: N users in parallel, per-endpoint latency percentiles, errors,
    and scenario assertions under concurrency. Exit code 1 when a gate fails."""
    from .load.report import write_load_report
    from .load.runner import LoadConfig, LoadRunner, check_gates, is_local
    settings = load_settings(config)
    target = settings.peer(peer).base_url
    if not is_local(target) and not i_own_this_system:
        typer.secho(f"Refusing to load-test {target}: not local. Only load systems you own or are authorised to "
                    f"stress, then pass --i-own-this-system.", fg="red")
        raise typer.Exit(2)
    server = None
    if peer == "self" and not _server_up(settings.base_url):
        ctx, server = _embedded_server(config)
    else:
        ctx = _ctx(config)
    cfg = LoadConfig(scenario, peer, users, duration, iterations, ramp, think_ms, dict(v.split("=", 1) for v in var),
                     log_traffic)
    typer.secho(f"Load: {scenario} on {peer} ({target}), {users} users, {duration:g}s"
                f"{f', max {iterations} journeys' if iterations else ''}", bold=True)
    s = LoadRunner(ctx, cfg).run()
    typer.echo(f"  requests {s['requests']}  throughput {s['throughput_rps']} req/s  "
               f"p50 {s['latency']['p50']} ms  p95 {s['latency']['p95']} ms  p99 {s['latency']['p99']} ms")
    typer.echo(f"  5xx/transport errors {s['server_error_rate_pct']}%   journeys {s['iterations']} "
               f"({s['iterations_per_s']}/s, p95 {s['iteration_p95_ms']} ms)   failed journeys "
               f"{s['iteration_failure_rate_pct']}%")
    for e in s["endpoints"][:12]:
        typer.echo(f"    {e['endpoint']:<44} n={e['count']:<6} p95={e['p95']:<8} err={e['server_errors']}")
    for f in s["failures"][:5]:
        typer.secho(f"  ✗ {f['count']}x {f['detail'][:200]}", fg="red")
    paths = write_load_report(s, Path(report_dir) / f"{time.strftime('%Y%m%d-%H%M%S')}-load-{peer}")
    typer.echo(f"  report: {paths['html']}")
    gates = check_gates(s, max_error_rate, max_p95, max_iteration_failure)
    for g in gates:
        typer.secho(f"  GATE FAILED: {g}", fg="red")
    if server:
        server.should_exit = True
    raise typer.Exit(1 if gates else 0)


@bulk_app.command("export")
def bulk_export_cmd(peer: str = typer.Option("self", "--peer", "-p"),
                    level: str = typer.Option("group", help="system | patient | group"),
                    group: Optional[str] = typer.Option(None, help="Group id (level=group)"),
                    type_: Optional[str] = typer.Option(None, "--type", help="comma-separated _type"),
                    since: Optional[str] = None, timeout: float = 600,
                    poll_max: Optional[float] = typer.Option(None, help="cap Retry-After waits (default: honour)"),
                    config: Optional[str] = ConfigOpt):
    """Kick off $export on a peer, follow it to completion, download and validate every NDJSON file."""
    from .bulk.client import bulk_export
    ctx = _ctx(config)
    if level == "group" and not group:
        typer.secho("--group is required for level=group (system/patient exports can be very large)", fg="red")
        raise typer.Exit(2)
    out = bulk_export(ctx.peer_client(peer), level, group, type_.split(",") if type_ else None, since,
                      None, timeout, poll_max)
    typer.echo(f"kick-off {out['kickoff_status']}, {out['polls']} poll(s), Retry-After requested "
               f"{sorted({x for x in out['retry_after_requested'] if x})}, {out.get('duration_ms', 0) / 1000:.1f}s")
    for f in out["files"]:
        typer.echo(f"  {f['type']:<22} {f['lines']:>7} lines  declared={f['declared']}  invalid={f['invalid']}  "
                   f"{'; '.join(f['issues'][:2])}")
    for e in out["errors"]:
        typer.secho(f"  ✗ {e}", fg="red")
    typer.secho("VALID" if out["all_valid"] else "PROBLEMS FOUND", fg="green" if out["all_valid"] else "red")
    raise typer.Exit(0 if out["all_valid"] else 1)


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


@keys_app.command("tls-cert")
def keys_tls_cert(hostnames: list[str] = typer.Argument(None, help="DNS names / IPs (default: localhost 127.0.0.1)"),
                  out: str = "keys/tls", days: int = 365):
    """Self-signed TLS certificate for serving SEKMET over https (testing only)."""
    import datetime
    import ipaddress
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    names = hostnames or ["localhost", "127.0.0.1"]
    key = ec.generate_private_key(ec.SECP256R1())
    sans = []
    for n in names:
        try:
            sans.append(x509.IPAddress(ipaddress.ip_address(n)))
        except ValueError:
            sans.append(x509.DNSName(n))
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, names[0])])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(minutes=5))
            .not_valid_after(now + datetime.timedelta(days=days))
            .add_extension(x509.SubjectAlternativeName(sans), critical=False)
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
            .sign(key, hashes.SHA256()))
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    Path(f"{out}.crt").write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    Path(f"{out}.key").write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                                     serialization.NoEncryption()))
    Path(f"{out}.key").chmod(0o600)
    typer.echo(f"Wrote {out}.crt and {out}.key for {', '.join(names)}.\n"
               f"Set tls_certfile/tls_keyfile and an https:// base_url in settings.yaml; clients must trust {out}.crt.")


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
