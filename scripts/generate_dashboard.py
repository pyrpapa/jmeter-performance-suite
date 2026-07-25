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
    """Locate the .jtl file for a test type regardless of naming convention
    or how deeply nested it ended up (CI artifact downloads can preserve
    the original target/jmeter/... folder structure depending on how many
    paths were included in the matching upload-artifact step)."""
    pattern = os.path.join(results_dir, "**", f"*{test_type}*.jtl")
    candidates = sorted(glob.glob(pattern, recursive=True))
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


def is_duration_only_failure(failure_message):
    """True if a failed sample's assertion-failure text indicates it was
    marked failed *purely* for being too slow (the Duration Assertion),
    as opposed to a wrong status code or some other functional break.
    Relies on jmeter.save.saveservice.assertion_results_failure_message=true
    being set in user.properties so failureMessage is actually populated."""
    msg = (failure_message or "").lower()
    return "duration assertion" in msg


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

            # A sample can be "not successful" for two very different reasons:
            # it was genuinely broken (wrong status code, exception, a
            # non-timing assertion), or it was otherwise fine but just too
            # slow (the Duration Assertion tripped). Keep those separate so
            # the report never conflates "the API is broken" with "the API
            # works but is slow" under one generic "error".
            functional_error = (not success) and not is_duration_only_failure(failure_message)
            timing_failure = (not success) and is_duration_only_failure(failure_message)

            rows.append(
                {
                    "group_key": group_key,
                    "label": label,
                    "elapsed": elapsed,
                    "success": success,
                    "threshold": threshold,
                    "slow": elapsed > threshold,
                    "functional_error": functional_error,
                    "timing_failure": timing_failure,
                    "response_code": row.get("responseCode", ""),
                    "failure_message": failure_message,
                }
            )
    return rows


def summarize(rows):
    """Aggregate a list of rows (all belonging to one group_key) into stats."""
    elapsed_values = [r["elapsed"] for r in rows]
    # error_count/error_rate_pct mirrors exactly what the Maven
    # errorRateThresholdInPercent gate counts (any success=false sample,
    # timing-related or not) - kept as-is so the PASS/FAIL badge here always
    # matches the real build outcome. functional_error_count/slow_count below
    # are the transparent breakdown of *why*.
    error_count = sum(1 for r in rows if not r["success"])
    functional_error_count = sum(1 for r in rows if r["functional_error"])
    slow_count = sum(1 for r in rows if r["slow"])
    total = len(rows)
    threshold = rows[0]["threshold"] if rows else 0
    return {
        "count": total,
        "error_count": error_count,
        "error_rate_pct": (error_count / total * 100) if total else 0,
        "functional_error_count": functional_error_count,
        "functional_error_rate_pct": (functional_error_count / total * 100) if total else 0,
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
    group_summaries.sort(key=lambda s: (-s["functional_error_count"], -s["slow_count"], -s["p95"]))

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
    color = "var(--red)" if over else "var(--purple)"
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
        return '<tr><td colspan="10" class="muted">No samples recorded.</td></tr>'
    max_scale = max((g["p95"] for g in groups), default=0)
    max_scale = max(max_scale * 1.15, max(g["threshold"] for g in groups) * 1.15, 1)
    out = []
    for g in groups:
        # Broken (functional_error_count > 0) always wins - a wrong status
        # code or exception is worse than "just slow". Slow-only rows (the
        # call worked, it just took too long) get their own warning color
        # instead of being lumped in with genuine breakage.
        if g["functional_error_count"] > 0:
            row_status = "critical"
        elif g["slow_count"] > 0:
            row_status = "warning"
        else:
            row_status = "good"
        out.append(
            "<tr>"
            f'<td>{html.escape(g["group_key"])}</td>'
            f'<td class="num">{g["count"]}</td>'
            f'<td class="num">{g["avg"]:.0f} ms</td>'
            f'<td class="num">{g["p95"]:.0f} ms</td>'
            f'<td class="num">{g["max"]:.0f} ms</td>'
            f'<td class="num">{g["threshold"]:.0f} ms</td>'
            f"<td>{bar_cell(g['p95'], g['threshold'], max_scale)}</td>"
            f'<td class="num" title="Wrong status code or other non-timing assertion failure">{g["functional_error_count"]} ({g["functional_error_rate_pct"]:.1f}%)</td>'
            f'<td class="num" title="Call completed but took longer than the SLA, regardless of pass/fail">{g["slow_count"]} ({g["slow_rate_pct"]:.1f}%)</td>'
            f'<td><span class="dot dot-{row_status}"></span></td>'
            "</tr>"
        )
    return "\n".join(out)


def render_test_type_section(report):
    meta = report["meta"]
    overall = report["overall"]
    if overall["count"] == 0:
        message = report.get("no_data_message") or (
            f"A *{report['test_type']}*.jtl file was found but contained no sample rows."
        )
        return (
            f'<section class="card">'
            f'<h2>{meta["label"]} Test <span class="muted">(no data)</span></h2>'
            f'<p class="muted">{html.escape(message)}</p>'
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
          <div class="stat-sub">gate: &le;{meta['error_rate_threshold_pct']}% &middot; {overall['functional_error_count']} broken, {overall['error_count'] - overall['functional_error_count']} slow-only</div>
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
            <th class="num">SLA</th>
            <th>p95 vs SLA</th>
            <th class="num" title="Wrong status code or other non-timing assertion failure - the call is actually broken">Broken</th>
            <th class="num" title="Call completed but took longer than the SLA, independent of pass/fail">Slow (&gt; SLA)</th>
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
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:wght@400;500;600;700&family=IBM+Plex+Mono:wght@400;500;600&display=swap" rel="stylesheet">
<style>
  :root {{
    --bg:#F5F7FA; --surface:#FFFFFF; --surfaceAlt:#EEF1F6; --border:#DDE3EC;
    --purple:#2563EB; --purpleLight:#1D4ED8; --purpleDim:#EAF1FE;
    --green:#16A34A; --greenDim:#DCFCE7;
    --text:#1B2436; --textMuted:#5B6577; --textDim:#8C96A6;
    --white:#0F172A; --orange:#B45309; --red:#DC2626; --blue:#0D9488;
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0;
    font-family: 'IBM Plex Sans', system-ui, -apple-system, "Segoe UI", sans-serif;
    background: var(--bg);
    color: var(--text);
    -webkit-font-smoothing: antialiased;
  }}
  .wrap {{ max-width: 1080px; margin: 0 auto; padding: 0 0 60px; }}
  header.top {{
    background: var(--surface); border-bottom: 1px solid var(--border);
    padding: 13px 24px; display: flex; align-items: center; gap: 12px;
    position: sticky; top: 0; z-index: 50; flex-wrap: wrap;
  }}
  .brand {{ display: flex; align-items: center; gap: 10px; }}
  .logo {{
    width: 32px; height: 32px; border-radius: 8px;
    background: linear-gradient(135deg, var(--purple), var(--green));
    display: flex; align-items: center; justify-content: center;
    font-weight: 800; font-size: 15px; color: #fff; flex: 0 0 auto;
  }}
  .brand-text .title {{ font-size: 15px; font-weight: 700; color: var(--white); line-height: 1.2; }}
  .brand-text .subtitle {{ font-size: 10px; color: var(--textMuted); text-transform: uppercase; letter-spacing: 0.08em; }}
  header.top .meta {{ color: var(--textMuted); font-size: 12px; margin-left: auto; }}
  .content {{ padding: 24px 24px 0; }}
  .summary-strip {{
    display: flex; flex-wrap: wrap; gap: 10px; margin: 0 0 20px;
  }}
  .summary-chip {{
    display: flex; align-items: center; gap: 8px;
    background: var(--surface); border: 1px solid var(--border);
    border-radius: 8px; padding: 8px 14px; font-size: 14px;
  }}
  .card {{
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 14px;
    padding: 20px 20px;
    margin-bottom: 20px;
  }}
  .card-header {{ display: flex; align-items: center; justify-content: space-between; margin-bottom: 4px; }}
  .card h2 {{ margin: 0; font-size: 14px; text-transform: uppercase; letter-spacing: 0.06em; color: var(--textMuted); font-weight: 700; }}
  .badge {{
    display: inline-flex; align-items: center; gap: 6px;
    font-size: 12px; font-weight: 700; letter-spacing: 0.02em;
    padding: 4px 10px; border-radius: 999px;
  }}
  .badge-good {{ background: var(--greenDim); color: var(--green); }}
  .badge-critical {{ background: #FDE8E8; color: var(--red); }}
  .stat-row {{
    display: grid; grid-template-columns: repeat(5, 1fr); gap: 12px;
    margin: 18px 0 20px;
  }}
  .stat-tile {{
    background: var(--surfaceAlt); border: 1px solid var(--border);
    border-radius: 10px; padding: 10px 12px;
  }}
  .stat-label {{ font-size: 10px; color: var(--textDim); text-transform: uppercase; letter-spacing: 0.06em; }}
  .stat-value {{ font-family: 'IBM Plex Mono', monospace; font-size: 20px; font-weight: 600; color: var(--white); margin-top: 3px; }}
  .stat-sub {{ font-size: 11px; color: var(--textDim); margin-top: 2px; }}
  table.data-table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
  table.data-table th {{
    text-align: left; font-weight: 700; color: var(--textMuted);
    border-bottom: 1px solid var(--border); padding: 8px 8px;
    font-size: 11px; text-transform: uppercase; letter-spacing: 0.06em;
  }}
  table.data-table td {{ padding: 8px 8px; border-bottom: 1px solid var(--border); vertical-align: middle; }}
  table.data-table td.num, table.data-table th.num {{ text-align: right; font-family: 'IBM Plex Mono', monospace; }}
  .muted {{ color: var(--textDim); }}
  .bar-track {{
    position: relative; width: 140px; height: 8px;
    background: var(--surfaceAlt); border: 1px solid var(--border); border-radius: 4px; overflow: visible;
  }}
  .bar-fill {{ position: absolute; top: 0; left: 0; height: 100%; border-radius: 4px; }}
  .bar-threshold {{
    position: absolute; top: -3px; width: 2px; height: 14px;
    background: var(--textDim);
  }}
  .dot {{ display: inline-block; width: 8px; height: 8px; border-radius: 50%; }}
  .dot-good {{ background: var(--green); }}
  .dot-warning {{ background: var(--orange); }}
  .dot-critical {{ background: var(--red); }}
  footer {{ color: var(--textDim); font-size: 12px; margin-top: 24px; line-height: 1.6; }}
  footer p {{ margin: 0 0 10px; }}
  footer p:last-child {{ margin-bottom: 0; }}
  footer .legend {{ display: flex; align-items: center; flex-wrap: wrap; gap: 4px; }}
  footer .legend .dot {{ margin-left: 4px; }}
</style>
</head>
<body>
<div class="wrap">
  <header class="top">
    <div class="brand">
      <div class="logo">J</div>
      <div class="brand-text"><div class="title">JMeter Performance Suite</div><div class="subtitle">Custom Dashboard</div></div>
    </div>
    <div class="meta">Generated {generated_at} &middot; source: {html.escape(source_label)} &middot; overall: {status_badge(overall_pass)}</div>
  </header>
  <div class="content">
  <div class="summary-strip">
    {summary_chips_html}
  </div>
  {sections}
  <footer>
    <p>Each request row is checked against two independent gates: JMeter's Duration
    Assertion (fails the sample, feeding the Maven error-rate gate) and this
    report's own re-check of elapsed time against the same SLA &mdash; so a
    misconfigured assertion can't silently hide a slow endpoint.</p>
    <p><strong>Broken</strong> vs <strong>Slow</strong> are kept separate on purpose: Broken means the
    call itself failed (wrong status code or another non-timing assertion) &mdash; something's
    actually wrong. Slow means the call succeeded but took longer than its SLA &mdash; the API works,
    it's just not fast enough.</p>
    <p class="legend">The status dot follows the worse of the two:
    <span class="dot dot-critical"></span> broken &nbsp;
    <span class="dot dot-warning"></span> slow only &nbsp;
    <span class="dot dot-good"></span> healthy</p>
  </footer>
  </div>
</div>
</body>
</html>
"""


def parse_job_status(raw):
    """Parse a 'smoke=success,load=failure,stress=skipped,spike=success' string
    (fed from GitHub Actions' needs.<job>.result context) into a dict. Returns
    {} if not provided, e.g. when running locally via run-tests.ps1."""
    status = {}
    if not raw:
        return status
    for pair in raw.split(","):
        pair = pair.strip()
        if not pair or "=" not in pair:
            continue
        key, value = pair.split("=", 1)
        status[key.strip().lower()] = value.strip().lower()
    return status


def no_data_message(test_type, job_status):
    """Explain *why* a test type has no data, distinguishing pipeline gating
    (expected) from an actual failure or reporting problem (not expected),
    instead of a bare 'no file found' that reads like a report bug either way."""
    status = job_status.get(test_type)

    if status is None:
        # No CI job-status info available (e.g. local run) - fall back to the
        # plain filesystem-level explanation.
        return f"No matching *{test_type}*.jtl file found in the results directory."

    if status == "skipped":
        upstream = {"stress": "load-test", "load": "smoke-test", "spike": "smoke-test"}.get(test_type)
        dep_note = f" (it only runs after {upstream} passes)" if upstream else ""
        return f"Skipped this run{dep_note} — this is expected pipeline gating, not a report bug. Its job never ran, so there's no .jtl to show."

    if status == "failure":
        return f"The {test_type}-test job failed before producing usable results — check that job's workflow logs for the actual error. This is a real test failure, not a dashboard problem."

    if status == "cancelled":
        return f"The {test_type}-test job was cancelled before it finished — no results were produced."

    if status == "success":
        return (
            f"The {test_type}-test job succeeded, but no matching .jtl file could be found in the downloaded "
            f"artifacts. Unlike the other cases above, this one does look like a report/artifact-wiring issue "
            f"worth investigating rather than expected behavior."
        )

    return f"No matching *{test_type}*.jtl file found in the results directory (job status: {status})."


def main():
    parser = argparse.ArgumentParser(description="Build the consolidated custom HTML dashboard from JMeter .jtl results.")
    parser.add_argument("--results-dir", default="results", help="Directory containing *-results.jtl files")
    parser.add_argument("--output", default=None, help="Output HTML file path (default: <results-dir>/custom-report/index.html)")
    parser.add_argument(
        "--job-status",
        default=None,
        help="Optional 'smoke=success,load=failure,stress=skipped,spike=success' string "
        "(feed it needs.<job>.result from the calling GitHub Actions job) so a missing "
        "test type's section can explain *why* instead of just 'no file found'.",
    )
    args = parser.parse_args()
    job_status = parse_job_status(args.job_status)

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
            report = build_test_type_report(test_type, [])
            report["no_data_message"] = no_data_message(test_type, job_status)
            reports.append(report)

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
