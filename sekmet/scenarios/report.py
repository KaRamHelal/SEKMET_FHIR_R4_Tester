"""Scenario reports: JSON, JUnit XML (for CI) and a self-contained HTML page."""
from __future__ import annotations

import html
import json
from pathlib import Path
from xml.etree import ElementTree as ET


def write_reports(results: list[dict], out_dir: str | Path, traffic_lookup=None) -> dict[str, str]:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    paths = {"json": str(out / "results.json"), "junit": str(out / "junit.xml"), "html": str(out / "report.html")}
    Path(paths["json"]).write_text(json.dumps(results, indent=2))
    Path(paths["junit"]).write_text(junit(results))
    Path(paths["html"]).write_text(html_report(results, traffic_lookup))
    return paths


def junit(results: list[dict]) -> str:
    suites = ET.Element("testsuites", name="sekmet")
    for r in results:
        steps = r["steps"]
        suite = ET.SubElement(suites, "testsuite", name=r["name"], tests=str(len(steps)),
                              failures=str(sum(s["status"] == "failed" for s in steps)),
                              errors=str(sum(s["status"] == "error" for s in steps)),
                              skipped=str(sum(s["status"] == "skipped" for s in steps)),
                              time=f"{r['duration_ms'] / 1000:.3f}", timestamp=r["started"])
        ET.SubElement(ET.SubElement(suite, "properties"), "property", name="peer", value=r["peer"])
        for s in steps:
            case = ET.SubElement(suite, "testcase", classname=r["name"], name=f"{s['index']:02d} {s['name']}",
                                 time=f"{s['duration_ms'] / 1000:.3f}")
            if s["status"] == "failed":
                ET.SubElement(case, "failure", message=s["message"][:500]).text = _checks_text(s)
            elif s["status"] == "error":
                ET.SubElement(case, "error", message=s["message"][:500]).text = s["message"]
            elif s["status"] == "skipped":
                ET.SubElement(case, "skipped", message=s["message"])
            if s.get("traffic_ids"):
                ET.SubElement(case, "system-out").text = f"traffic ids: {s['traffic_ids']}"
    ET.indent(suites)
    return ET.tostring(suites, encoding="unicode", xml_declaration=True)


def _checks_text(s: dict) -> str:
    return "\n".join(f"[{'PASS' if c['ok'] else 'FAIL'}] {c['check']} -> {c['detail']}" for c in s.get("checks", []))


def html_report(results: list[dict], traffic_lookup=None) -> str:
    e = html.escape
    total = {k: sum(r["status"] == k for r in results) for k in ("passed", "failed", "error", "skipped")}
    parts = [f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><title>SEKMET run report</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
:root{{--bg:#fbfaf7;--fg:#1d1d1b;--mute:#6b6a65;--line:#e4e1d8;--pass:#2f7d4f;--fail:#b3261e;--skip:#8a7a2f;--card:#fff}}
@media(prefers-color-scheme:dark){{:root{{--bg:#161614;--fg:#ecebe6;--mute:#9d9b93;--line:#2e2d29;--card:#1e1e1b;--pass:#6fcf97;--fail:#ff8a80;--skip:#d9c56b}}}}
body{{font:14px/1.5 ui-sans-serif,system-ui,sans-serif;background:var(--bg);color:var(--fg);margin:0;padding:24px 16px;max-width:1100px;margin:auto}}
h1{{font-size:20px;margin:0 0 4px}} .mute{{color:var(--mute)}} .card{{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:12px 16px;margin:16px 0}}
.passed{{color:var(--pass)}} .failed,.error{{color:var(--fail)}} .skipped{{color:var(--skip)}}
table{{border-collapse:collapse;width:100%}} td,th{{text-align:left;padding:6px 8px;border-top:1px solid var(--line);vertical-align:top}}
code,pre{{font:12px ui-monospace,monospace}} pre{{white-space:pre-wrap;margin:4px 0;overflow-x:auto}}
.badge{{font-weight:600;text-transform:uppercase;font-size:11px;letter-spacing:.04em}}
</style></head><body><h1>SEKMET FHIR R4 Tester &mdash; run report</h1>
<p class="mute">{len(results)} scenario(s): {total['passed']} passed, {total['failed']} failed, {total['error']} error, {total['skipped']} skipped</p>"""]
    for r in results:
        parts.append(f"""<div class="card"><h2 style="font-size:16px;margin:0">{e(r['name'])}
<span class="badge {r['status']}">{r['status']}</span></h2>
<div class="mute">{e(r.get('description') or '')}<br>peer <code>{e(r['peer'])}</code> &middot; run <code>{e(r['run_id'])}</code>
&middot; {r['duration_ms'] / 1000:.1f}s &middot; {e(r['started'])}</div><table><tr><th>#</th><th>Step</th><th>Status</th><th>Details</th></tr>""")
        for s in r["steps"]:
            checks = "".join(f"<div class=\"{'passed' if c['ok'] else 'failed'}\">{'&#10003;' if c['ok'] else '&#10007;'} "
                             f"{e(c['check'])} <span class=\"mute\">&rarr; {e(str(c['detail'])[:300])}</span></div>"
                             for c in s.get("checks", []))
            traffic = f"<div class=\"mute\">traffic: {', '.join(map(str, s.get('traffic_ids', [])[:20]))}</div>" if s.get("traffic_ids") else ""
            parts.append(f"<tr><td>{s['index']}</td><td>{e(s['name'])}<div class=\"mute\">{e(s['kind'])} &middot; "
                         f"{s['duration_ms']:.0f} ms</div></td><td class=\"badge {s['status']}\">{s['status']}</td>"
                         f"<td><pre>{e(s['message'])}</pre>{checks}{traffic}</td></tr>")
        parts.append("</table></div>")
    parts.append("</body></html>")
    return "".join(parts)
