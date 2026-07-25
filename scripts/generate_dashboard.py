#!/usr/bin/env python3
"""
generate_dashboard.py

Builds a single self-contained HTML dashboard from the .jtl (CSV) result
files produced by the jmeter-maven-plugin / run-tests.ps1. Unlike the stock
JMeter HTML Report Dashboard (which is generated per test-type, per run),
this consolidates whichever of smoke/load/stress/spike results are present
into one branded summary: pass/fail against the same thresholds enforced by
pom.xml + the Duration Assertions in the .jmx files, response-time
percentiles per endpoint, and a flag for any sample that breached its
response-time SLA independent of JMeter's own bookkeeping.

Usage:
    python3 scripts/generate_dashboard.py \
        --results-dir results \
        --output results/custom-report/index.html

Only the Python standard library is used so this runs anywhere Python 3
is available (CI runners, local dev machines) with no extra installs.
"""

import argparse
import csv
import datetime
import glob
import html
import os
import statistics
import sys

# Test-type metadata: display name, error-rate gate (must match pom.xml's
# per-profile jmeter.error.rate.threshold), and the response-time SLA(s)
# enforced by the Duration Assertions added to each .jmx file.
#
# The actual .jtl filename varies by how the tests were run: run-tests.ps1
# writes "<type>-results.jtl", while the jmeter-maven-plugin (used in CI)
# writes "<type>-test.jtl" (or similar, depending on plugin version) under
# target/jmeter/results/. Rather than hardcode one convention, each test
# type is resolved by glob'ing the results directory for any *.jtl file
# whose name contains the test type keyword.
TEST_TYPES = {
    "smoke": {
        "label": "Smoke",
        "error_rate_threshold_pct": 0,
        "response_time_threshold_ms": 800,
    },
    "load": {
        "label": "Load",
        "error_rate_threshold_pct": 2,
        "response_time_threshold_ms": 1500,
    },
    "stress": {
        "label": "Stress",
        "error_rate_threshold_pct": 5,
        "response_time_threshold_ms": 3000,
    },
    "spike": {
        "label": "Spike",
        "error_rate_threshold_pct": 5,
        # Spike has two thresholds: a tight one for the baseline/recovery
        "response_time_threshold_ms": None,
    },
}


def find_jtl(results_dir, test_type):
    """Locate the .jtl file for a test type regardless of naming convention."""
    candidates = sorted(glob.glob(os.path.join(results_dir, f"*{test_type}*.jtl")))
    # Prefer files that don't belong to another test type when names overlap
    # (none currently do - smoke/load/stress/spike are mutually exclusive
    # substrings - but this keeps the match unambiguous if that changes).
    if not candidates:
        return None
    if len(candidates) > 1:
        print(
            f"warning: multiple .jtl files matched '{test_type}': {candidates}; using {candidates[0]}",
            file=sys.stderr,
        )
    return candidates[0]

SPIKE_BASELINE_THRESHOLD_MS = 1200
SPIKE_BURST_THRESHOLD_MS = 3000


def percentile(values, pct):
    """Linear-interpolation percentile, matching numpy's default method."""
    if not values:
        return 0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    k = (len(ordered) - 1) * (pct / 100.0)
    f = int(k)
    c = min(f + 1, len(ordered) - 1)
    if f == c:
        return ordered[f]
    return ordered[f] + (ordered[c] - ordered[f]) * (k - f)


def spike_phase(thread_name):
    if thread_name.startswith("Spike"):
        return "Spike burst", SPIKE_BURST_THRESHOLD_MS
    if thread_name.startswith("Baseline"):
        return "Baseline", SPIKE_BASELINE_THRESHOLD_MS
    if thread_name.startswith("Recovery"):
        return "Recovery", SPIKE_BASELINE_THRESHOLD_MS
    return "Other", SPIKE_BASELINE_THRESHOLD_MS


def load_jtl(path, test_type):
    """Parse one .jtl CSV file into per-row dicts with a resolved SLA."""
    rows = []
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                elapsed = int(row["elapsed"])
            except (KeyError, ValueError):
                continue
            success = row.get("success", "false").strip().lower() == "true"
            label = row.get("label", "unknown")
            thread_name = row.get("threadName", "")
            failure_message = row.get("failureMessage", "") or ""

            if test_type == "spike":
                phase, threshold = spike_phase(thread_name)
                group_key = f"{phase} · {label}"
            else:
                threshold = TEST_TYPES[test_type]["response_time_threshold_ms"]
                group_key = label

            rows.append(
                {
                    "group_key": group_key,
                    "label": label,
                    "elapsed": elapsed,
                    "success": success,
                    "threshold": threshold,
                    "slow": elapsed > threshold,
                    "response_code": row.get("responseCode", ""),
                    "failure_message": failure_message,
                }
            )
    return rows


def summarize(rows):
    """Aggregate a list of rows (all belonging to one group_key) into stats."""
    elapsed_values = [r["elapsed"] for r in rows]
    error_count = sum(1 for r in rows if not r["success"])
    slow_count = sum(1 for r in rows if r["slow"])
    total = len(rows)
    threshold = rows[0]["threshold"] if rows else 0
    return {
        "count": total,
        "error_count": error_count,
        "error_rate_pct": (error_count / total * 100) if total else 0,
        "slow_count": slow_count,
        "slow_rate_pct": (slow_count / total * 100) if total else 0,
        "avg": statistics.fmean(elapsed_values) if elapsed_values else 0,
        "median": statistics.median(elapsed_values) if elapsed_values else 0,
        "p90": percentile(elapsed_values, 90),
        "p95": percentile(elapsed_values, 95),
        "max": max(elapsed_values) if elapsed_values else 0,
        "threshold": threshold,
    }


def build_test_type_report(test_type, rows):
    meta = TEST_TYPES[test_type]
    overall = summarize(rows)
    overall_pass = overall["error_rate_pct"] <= meta["error_rate_threshold_pct"]

    groups = {}
    for r in rows:
        groups.setdefault(r["group_key"], []).append(r)

    group_summaries = []
    for key, group_rows in groups.items():
        s = summarize(group_rows)
        s["group_key"] = key
        group_summaries.append(s)
    # Slowest / least healthy groups first
    group_summaries.sort(key=lambda s: (-s["error_count"], -s["slow_count"], -s["p95"]))

    return {
        "test_type": test_type,
        "meta": meta,
        "overall": overall,
        "overall_pass": overall_pass,
        "groups": group_summaries,
    }


def status_badge(passed):
    if passed:
        return '<span class="badge badge-good">&#10003; PASS</span>'
    return '<span class="badge badge-critical">&#10007; FAIL</span>'


def bar_cell(value_ms, threshold_ms, max_scale_ms):
    """A thin horizontal bar sized relative to max_scale_ms, red past threshold."""
    if max_scale_ms <= 0:
        max_scale_ms = max(value_ms, threshold_ms, 1)
    pct = max(0.0, min(100.0, (value_ms / max_scale_ms) * 100))
    over = value_ms > threshold_ms
    color = "var(--status-critical)" if over else "var(--series-1)"
    threshold_pct = max(0.0, min(100.0, (threshold_ms / max_scale_ms) * 100))
    return (
        '<div class="bar-track">'
        f'<div class="bar-fill" style="width:{pct:.1f}%;background:{color};"></div>'
        f'<div class="bar-threshold" style="left:{threshold_pct:.1f}%;" '
        f'title="SLA threshold: {threshold_ms} ms"></div>'
        "</div>"
    )


def render_group_rows(groups):
    if not groups:
        return '<tr><td colspan="8" class="muted">No samples recorded.</td></tr>'
    max_scale = max((g["p95"] for g in groups), default=0)
    max_scale = max(max_scale * 1.15, max(g["threshold"] for g in groups) * 1.15, 1)
    out = []
    for g in groups:
        row_status = "critical" if (g["error_count"] > 0 or g["slow_count"] > 0) else "good"
        out.append(
            "<tr>"
            f'<td>{html.escape(g["group_key"])}</td>'
            f'<td class="num">{g["count"]}</td>'
            f'<td class="num">{g["avg"]:.0f} ms</td>'
            f'<td class="num">{g["p95"]:.0f} ms</td>'
            f'<td class="num">{g["max"]:.0f} ms</td>'
            f"<td>{bar_cell(g['p95'], g['threshold'], max_scale)}</td>"
            f'<td class="num">{g["error_count"]} ({g["error_rate_pct"]:.1f}%)</td>'
            f'<td class="num">{g["slow_count"]} ({g["slow_rate_pct"]:.1f}%)</td>'
            f'<td><span class="dot dot-{row_status}"></span></td>'
            "</tr>"
        )
    return "\n".join(out)


def render_test_type_section(report):
    meta = report["meta"]
    overall = report["overall"]
    if overall["count"] == 0:
        return (
            f'<section class="card">'
            f'<h2>{meta["label"]} Test <span class="muted">(no data)</span></h2>'
            f'<p class="muted">No matching *{report["test_type"]}*.jtl file found in the results directory.</p>'
            f"</section>"
        )

    threshold_note = (
        f'{meta["response_time_threshold_ms"]} ms'
        if meta["response_time_threshold_ms"]
        else f"{SPIKE_BASELINE_THRESHOLD_MS} ms baseline/recovery, {SPIKE_BURST_THRESHOLD_MS} ms during burst"
    )

    return f"""
    <section class="card">
      <div class="card-header">
        <h2>{meta['label']} Test</h2>
        {status_badge(report['overall_pass'])}
      </div>
      <div class="stat-row">
        <div class="stat-tile">
          <div class="stat-label">Samples</div>
          <div class="stat-value">{overall['count']}</div>
        </div>
        <div class="stat-tile">
          <div class="stat-label">Error rate</div>
          <div class="stat-value">{overall['error_rate_pct']:.2f}%</div>
          <div class="stat-sub">gate: &le;{meta['error_rate_threshold_pct']}%</div>
        </div>
        <div class="stat-tile">
          <div class="stat-label">Avg response</div>
          <div class="stat-value">{overall['avg']:.0f} ms</div>
        </div>
        <div class="stat-tile">
          <div class="stat-label">p95 response</div>
          <div class="stat-value">{overall['p95']:.0f} ms</div>
        </div>
        <div class="stat-tile">
          <div class="stat-label">Slow samples</div>
          <div class="stat-value">{overall['slow_count']}</div>
          <div class="stat-sub">SLA: {threshold_note}</div>
        </div>
      </div>
      <table class="data-table">
        <thead>
          <tr>
            <th>Request</th>
            <th class="num">Count</th>
            <th class="num">Avg</th>
            <th class="num">p95</th>
            <th class="num">Max</th>
            <th>p95 vs SLA</th>
            <th class="num">Errors</th>
            <th class="num">Slow (&gt; SLA)</th>
            <th>Status</th>
          </tr>
        </thead>
        <tbody>
          {render_group_rows(report['groups'])}
        </tbody>
      </table>
    </section>
    """


def render_html(reports, generated_at, source_label):
    present = [r for r in reports if r["overall"]["count"] > 0]
    overall_pass = all(r["overall_pass"] for r in present) if present else True
    sections = "\n".join(render_test_type_section(r) for r in reports)

    summary_chips = []
    for r in reports:
        if r["overall"]["count"] == 0:
            continue
        summary_chips.append(
            f'<div class="summary-chip">'
            f'<span>{r["meta"]["label"]}</span>'
            f'{status_badge(r["overall_pass"])}'
            f"</div>"
        )
    summary_chips_html = "\n".join(summary_chips) if summary_chips else '<p class="muted">No result files found yet.</p>'

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>JMeter Performance Suite - Dashboard</title>
<style>
  .viz-root {{
    color-scheme: light;
    --surface-1:      #fcfcfb;
    --page-plane:     #f9f9f7;
    --text-primary:   #0b0b0b;
    --text-secondary: #52514e;
    --text-muted:     #898781;
    --gridline:       #e1e0d9;
    --baseline:       #c3c2b7;
    --border:         rgba(11,11,11,0.10);
    --series-1:       #2a78d6;
    --status-good:     #0ca30c;
    --status-warning:  #fab219;
    --status-serious:  #ec835a;
    --status-critical: #d03b3b;
  }}
  @media (prefers-color-scheme: dark) {{
    .viz-root {{
      color-scheme: dark;
      --surface-1:      #1a1a19;
      --page-plane:     #0d0d0d;
      --text-primary:   #ffffff;
      --text-secondary: #c3c2b7;
      --text-muted:     #898781;
      --gridline:       #2c2c2a;
      --baseline:       #383835;
      --border:         rgba(255,255,255,0.10);
      --series-1:       #3987e5;
      --status-good:     #0ca30c;
      --status-warning:  #fab219;
      --status-serious:  #ec835a;
      --status-critical: #e66767;
    }}
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0;
    font-family: system-ui, -apple-system, "Segoe UI", sans-serif;
    background: var(--page-plane);
    color: var(--text-primary);
  }}
  .viz-root {{ padding: 32px 24px 64px; max-width: 1080px; margin: 0 auto; }}
  header.top {{ margin-bottom: 24px; }}
  header.top h1 {{ margin: 0 0 4px; font-size: 22px; }}
  header.top .meta {{ color: var(--text-secondary); font-size: 13px; }}
  .summary-strip {{
    display: flex; flex-wrap: wrap; gap: 10px; margin: 16px 0 28px;
  }}
  .summary-chip {{
    display: flex; align-items: center; gap: 8px;
    background: var(--surface-1); border: 1px solid var(--border);
    border-radius: 8px; padding: 8px 14px; font-size: 14px;
  }}
  .card {{
    background: var(--surface-1);
    border: 1px solid var(--border);
    border-radius: 12px;
    padding: 20px 24px;
    margin-bottom: 20px;
  }}
  .card-header {{ display: flex; align-items: center; justify-content: space-between; }}
  .card h2 {{ margin: 0; font-size: 17px; }}
  .badge {{
    display: inline-flex; align-items: center; gap: 6px;
    font-size: 12px; font-weight: 600; letter-spacing: 0.02em;
    padding: 4px 10px; border-radius: 999px;
  }}
  .badge-good {{ background: color-mix(in srgb, var(--status-good) 16%, transparent); color: var(--status-good); }}
  .badge-critical {{ background: color-mix(in srgb, var(--status-critical) 16%, transparent); color: var(--status-critical); }}
  .stat-row {{
    display: grid; grid-template-columns: repeat(5, 1fr); gap: 12px;
    margin: 18px 0 20px;
  }}
  .stat-tile {{
    background: var(--page-plane); border: 1px solid var(--border);
    border-radius: 8px; padding: 10px 12px;
  }}
  .stat-label {{ font-size: 11px; color: var(--text-muted); text-transform: uppercase; letter-spacing: 0.04em; }}
  .stat-value {{ font-size: 20px; font-weight: 600; margin-top: 2px; }}
  .stat-sub {{ font-size: 11px; color: var(--text-muted); margin-top: 2px; }}
  table.data-table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
  table.data-table th {{
    text-align: left; font-weight: 600; color: var(--text-secondary);
    border-bottom: 1px solid var(--gridline); padding: 8px 8px;
    font-size: 11px; text-transform: uppercase; letter-spacing: 0.03em;
  }}
  table.data-table td {{ padding: 8px 8px; border-bottom: 1px solid var(--gridline); vertical-align: middle; }}
  table.data-table td.num, table.data-table th.num {{ text-align: right; font-variant-numeric: tabular-nums; }}
  .muted {{ color: var(--text-muted); }}
  .bar-track {{
    position: relative; width: 140px; height: 8px;
    background: var(--gridline); border-radius: 4px; overflow: visible;
  }}
  .bar-fill {{ position: absolute; top: 0; left: 0; height: 100%; border-radius: 4px; }}
  .bar-threshold {{
    position: absolute; top: -3px; width: 2px; height: 14px;
    background: var(--baseline);
  }}
  .dot {{ display: inline-block; width: 10px; height: 10px; border-radius: 50%; }}
  .dot-good {{ background: var(--status-good); }}
  .dot-critical {{ background: var(--status-critical); }}
  footer {{ color: var(--text-muted); font-size: 12px; margin-top: 24px; }}
</style>
</head>
<body>
<div class="viz-root">
  <header class="top">
    <h1>JMeter Performance Suite &mdash; Custom Dashboard</h1>
    <div class="meta">Generated {generated_at} &middot; source: {html.escape(source_label)} &middot; overall: {status_badge(overall_pass)}</div>
  </header>
  <div class="summary-strip">
    {summary_chips_html}
  </div>
  {sections}
  <footer>
    Each request row is checked against two independent gates: JMeter's Duration
    Assertion (fails the sample, feeding the Maven error-rate gate) and this
    report's own re-check of elapsed time against the same SLA &mdash; so a
    misconfigured assertion can't silently hide a slow endpoint.
  </footer>
</div>
</body>
</html>
"""


def main():
    parser = argparse.ArgumentParser(description="Build the consolidated custom HTML dashboard from JMeter .jtl results.")
    parser.add_argument("--results-dir", default="results", help="Directory containing *-results.jtl files")
    parser.add_argument("--output", default=None, help="Output HTML file path (default: <results-dir>/custom-report/index.html)")
    args = parser.parse_args()

    output = args.output or os.path.join(args.results_dir, "custom-report", "index.html")
    os.makedirs(os.path.dirname(output), exist_ok=True)

    reports = []
    found_any = False
    for test_type, meta in TEST_TYPES.items():
        path = find_jtl(args.results_dir, test_type)
        if path:
            found_any = True
            rows = load_jtl(path, test_type)
            reports.append(build_test_type_report(test_type, rows))
        else:
            reports.append(build_test_type_report(test_type, []))

    if not found_any:
        print(f"warning: no *.jtl files found under {args.results_dir}", file=sys.stderr)

    generated_at = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    html_out = render_html(reports, generated_at, args.results_dir)

    with open(output, "w", encoding="utf-8") as f:
        f.write(html_out)

    print(f"Dashboard written to {output}")

    # Exit non-zero if any present test type breached its own gate, so CI can
    # optionally use this as an additional check alongside the Maven build.
    any_fail = any(r["overall"]["count"] > 0 and not r["overall_pass"] for r in reports)
    if any_fail:
        print("One or more test types breached their error-rate threshold.", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
