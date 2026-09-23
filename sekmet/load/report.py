"""Load-run report: JSON + self-contained HTML (two single-series charts with hover, tables for everything)."""
from __future__ import annotations

import html
import json
from pathlib import Path


def write_load_report(summary: dict, out_dir: str | Path) -> dict[str, str]:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    paths = {"json": str(out / "load.json"), "html": str(out / "load.html")}
    Path(paths["json"]).write_text(json.dumps(summary, indent=2))
    Path(paths["html"]).write_text(render(summary))
    return paths


def _chart(title: str, unit: str, points: list[tuple[int, float]], cid: str) -> str:
    """Single-series line chart (one axis), 2px line, recessive grid, crosshair + tooltip via inline JS."""
    w, h, pl, pr, pt, pb = 640, 200, 48, 12, 12, 28
    if not points:
        return f"<h3>{html.escape(title)}</h3><p class='mute'>no data</p>"
    xs = [p[0] for p in points]
    ymax = max(p[1] for p in points) or 1
    nice = 10 ** len(str(int(ymax))) / 10
    ymax = max(nice, ((ymax // nice) + 1) * nice)
    xmax = max(xs) or 1
    X = lambda x: pl + (w - pl - pr) * x / xmax  # noqa: E731
    Y = lambda y: pt + (h - pt - pb) * (1 - y / ymax)  # noqa: E731
    d = " ".join(f"{'M' if i == 0 else 'L'}{X(x):.1f},{Y(y):.1f}" for i, (x, y) in enumerate(points))
    grid = "".join(f"<line x1='{pl}' x2='{w - pr}' y1='{Y(ymax * f):.1f}' y2='{Y(ymax * f):.1f}' class='grid'/>"
                   f"<text x='{pl - 6}' y='{Y(ymax * f) + 4:.1f}' class='tick' text-anchor='end'>{ymax * f:g}</text>"
                   for f in (0, .25, .5, .75, 1))
    xt = "".join(f"<text x='{X(x):.1f}' y='{h - 8}' class='tick' text-anchor='middle'>{x}s</text>"
                 for x in sorted({0, xmax // 2, xmax}))
    data = json.dumps([[x, y] for x, y in points])
    return f"""<figure class="chart"><figcaption>{html.escape(title)} <span class="mute">({unit})</span></figcaption>
<svg id="{cid}" viewBox="0 0 {w} {h}" role="img" aria-label="{html.escape(title)} over time">
{grid}{xt}<path d="{d}" class="line"/><line class="xhair" y1="{pt}" y2="{h - pb}" x1="-10" x2="-10"/>
<circle class="dot" r="4" cx="-10" cy="-10"/></svg><div class="tip" id="{cid}-tip"></div></figure>
<script>(function(){{const s=document.getElementById('{cid}'),t=document.getElementById('{cid}-tip'),D={data};
const X=x=>{pl}+({w - pl - pr})*x/{xmax},Y=y=>{pt}+({h - pt - pb})*(1-y/{ymax});
s.addEventListener('mousemove',e=>{{const r=s.getBoundingClientRect(),vx=(e.clientX-r.left)*{w}/r.width;
let b=D[0];for(const p of D)if(Math.abs(X(p[0])-vx)<Math.abs(X(b[0])-vx))b=p;
s.querySelector('.xhair').setAttribute('x1',X(b[0]));s.querySelector('.xhair').setAttribute('x2',X(b[0]));
const c=s.querySelector('.dot');c.setAttribute('cx',X(b[0]));c.setAttribute('cy',Y(b[1]));
t.textContent=b[0]+'s: '+b[1]+' {unit}';t.style.left=(e.clientX-r.left+12)+'px';t.style.opacity=1;}});
s.addEventListener('mouseleave',()=>{{t.style.opacity=0;s.querySelector('.xhair').setAttribute('x1',-10);
s.querySelector('.xhair').setAttribute('x2',-10);s.querySelector('.dot').setAttribute('cx',-10);}});}})();</script>"""


def render(s: dict) -> str:
    e = html.escape
    c = s["config"]
    tiles = [("Throughput", f"{s['throughput_rps']} req/s"), ("p95 latency", f"{s['latency']['p95']} ms"),
             ("5xx / transport errors", f"{s['server_error_rate_pct']}%"),
             ("Iterations", f"{s['iterations']} ({s['iterations_per_s']}/s)"),
             ("Iteration failures", f"{s['iteration_failure_rate_pct']}%"),
             ("Requests", str(s["requests"]))]
    rows = "".join(f"<tr><td class='mono'>{e(x['endpoint'])}</td><td>{x['count']}</td><td>{x['server_errors']}</td>"
                   f"<td>{x['client_errors']}</td><td>{x['p50']}</td><td>{x['p95']}</td><td>{x['p99']}</td>"
                   f"<td>{x['max']}</td></tr>" for x in s["endpoints"])
    fails = "".join(f"<tr><td>{f['count']}</td><td class='mono'>{e(f['detail'])}</td></tr>" for f in s["failures"]) \
        or "<tr><td colspan='2' class='mute'>none</td></tr>"
    tl = s["timeline"]
    tl_rows = "".join(f"<tr><td>{t['second']}</td><td>{t['requests']}</td><td>{t['errors']}</td><td>{t['p95']}</td></tr>"
                      for t in tl)
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>SEKMET load run</title><style>
:root{{--surface:#fcfcfb;--ink:#0b0b0b;--ink2:#52514e;--line:#e4e1d8;--series:#2a78d6;--card:#fff}}
@media(prefers-color-scheme:dark){{:root{{--surface:#1a1a19;--ink:#fff;--ink2:#c3c2b7;--line:#2e2d29;--series:#3987e5;--card:#20201e}}}}
body{{font:14px/1.5 ui-sans-serif,system-ui,sans-serif;background:var(--surface);color:var(--ink);margin:0 auto;
max-width:1100px;padding:24px 16px}} h1{{font-size:20px;margin:0}} .mute{{color:var(--ink2)}}
.tiles{{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin:16px 0}}
.tile{{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:10px 12px}}
.tile b{{display:block;font-size:20px}} .tile span{{color:var(--ink2);font-size:12px}}
.charts{{display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:16px}}
figure.chart{{margin:0;background:var(--card);border:1px solid var(--line);border-radius:8px;padding:10px;position:relative}}
figcaption{{font-weight:600;margin-bottom:4px}} svg{{width:100%;height:auto;display:block}}
.grid{{stroke:var(--line);stroke-width:1}} .tick{{fill:var(--ink2);font-size:10px}}
.line{{fill:none;stroke:var(--series);stroke-width:2;stroke-linejoin:round;stroke-linecap:round}}
.xhair{{stroke:var(--ink2);stroke-width:1;stroke-dasharray:3 3}} .dot{{fill:var(--series);stroke:var(--card);stroke-width:2}}
.tip{{position:absolute;top:28px;opacity:0;background:var(--ink);color:var(--surface);font-size:12px;padding:2px 6px;
border-radius:4px;pointer-events:none;transition:opacity .1s}}
table{{border-collapse:collapse;width:100%;font-size:13px;margin:8px 0 20px}} th,td{{text-align:left;padding:5px 8px;
border-top:1px solid var(--line)}} th{{color:var(--ink2);font-weight:600}} .mono{{font-family:ui-monospace,monospace;font-size:12px}}
.scroll{{overflow-x:auto}} details summary{{cursor:pointer;color:var(--ink2)}}
</style></head><body>
<h1>SEKMET load run — {e(c['scenario'])}</h1>
<p class="mute">peer <code>{e(c['peer'])}</code> ({e(s['peer_base'])}) · {c['users']} users · ramp {c['ramp_s']}s ·
{s['elapsed_s']}s · think {c['think_ms']} ms · started {e(s['started'])}</p>
<div class="tiles">{''.join(f"<div class='tile'><span>{e(k)}</span><b>{e(v)}</b></div>" for k, v in tiles)}</div>
<div class="charts">{_chart("Throughput", "req/s", [(t['second'], t['requests']) for t in tl], "c1")}
{_chart("p95 latency per second", "ms", [(t['second'], t['p95']) for t in tl if t['requests']], "c2")}</div>
<h2 style="font-size:16px">Endpoints</h2><div class="scroll"><table><tr><th>Endpoint</th><th>Count</th><th>5xx/transport</th>
<th>4xx</th><th>p50 ms</th><th>p95 ms</th><th>p99 ms</th><th>max ms</th></tr>{rows}</table></div>
<h2 style="font-size:16px">Iteration failures (scenario assertions under load)</h2>
<div class="scroll"><table><tr><th>Count</th><th>First failing step</th></tr>{fails}</table></div>
<details><summary>Per-second table</summary><div class="scroll"><table><tr><th>Second</th><th>Requests</th>
<th>Errors</th><th>p95 ms</th></tr>{tl_rows}</table></div></details>
</body></html>"""
