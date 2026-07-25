# JMeter Performance Test Suite

[![Performance Tests](https://github.com/pyrpapa/jmeter-performance-suite/actions/workflows/performance-tests.yml/badge.svg)](https://github.com/pyrpapa/jmeter-performance-suite/actions/workflows/performance-tests.yml)

**Live dashboard:** [pyrpapa.github.io/jmeter-performance-suite](https://pyrpapa.github.io/jmeter-performance-suite/) — rebuilt automatically on every push to `master`.

A portfolio-grade performance testing framework using Apache JMeter 5.6.3 and Maven, with automated CI/CD via GitHub Actions.

## Test Strategy

| Test Type | Users | Ramp Up | Goal |
|-----------|-------|---------|------|
| Smoke     | 1     | 1s      | Verify endpoints are alive |
| Load      | 50    | 60s     | Validate behavior under normal load |
| Stress    | 200   | 120s    | Find breaking point |
| Spike     | 300   | 5s      | Test recovery from sudden traffic burst |

## Target API

All tests run against [JSONPlaceholder](https://jsonplaceholder.typicode.com) — a free public REST API used as a realistic demo target.

**Endpoints tested:**
- `GET /posts` — retrieve all posts
- `GET /posts/{id}` — retrieve single post (data-driven via CSV)
- `POST /posts` — create a post with JSON body
- `GET /users` — retrieve all users
- `GET /comments` — retrieve all comments

## Project Structure

```
├── .github/workflows/
│   └── performance-tests.yml   # CI pipeline
├── scripts/
│   └── generate_dashboard.py   # builds the custom consolidated HTML dashboard
├── src/
│   └── test/
│       ├── jmeter/
│       │   ├── smoke-test.jmx
│       │   ├── load-test.jmx
│       │   ├── stress-test.jmx
│       │   └── spike-test.jmx
│       └── resources/
│           ├── test-data.csv       # parameterized user/post data
│           └── user.properties     # JMeter engine + save-service settings
├── results/                    # gitignored, generated at runtime
│   └── custom-report/          # output of generate_dashboard.py
└── pom.xml
```

## Prerequisites

- Java 11+
- Maven 3.9+
- JMeter 5.6.3 (for GUI editing only)
- Python 3.8+ (only needed for the custom dashboard — `scripts/generate_dashboard.py`)

## Running Tests Locally

**Smoke test:**
```bash
mvn verify -Psmoke
```

**Load test:**
```bash
mvn verify -Pload
```

**Stress test:**
```bash
mvn verify -Pstress
```

**Spike test:**
```bash
mvn verify -Pspike
```

Reports are generated in `target/jmeter/reports/` after each run (the stock JMeter HTML Report Dashboard). Running via `run-tests.ps1` additionally builds the custom consolidated dashboard described below at `results\custom-report\index.html`.

## CI/CD Pipeline

The GitHub Actions workflow runs automatically on every push to `master`:

1. **Smoke** runs first — gates everything else
2. **Load** runs after smoke passes
3. **Stress** runs after load passes
4. **Spike** runs in parallel with load/stress
5. **Publish Custom Dashboard** runs last (`if: always()`), downloads whichever test results are available, and builds the consolidated dashboard

Test results (raw `.jtl`/`.csv` + the stock JMeter dashboard) are uploaded as artifacts and retained for 30 days in the Actions tab. The consolidated custom dashboard is uploaded as its own `custom-dashboard` artifact, and — on pushes to `master` — also published to GitHub Pages at [pyrpapa.github.io/jmeter-performance-suite](https://pyrpapa.github.io/jmeter-performance-suite/), so that link always reflects the most recent run.

## Error Rate Thresholds

Maven will **fail the build** if error rates exceed:

| Test  | Threshold |
|-------|-----------|
| Smoke | 0%        |
| Load  | 2%        |
| Stress| 5%        |
| Spike | 5%        |

## Response Time SLAs (Duration Assertions)

Every HTTP request in every `.jmx` file also carries a **Duration Assertion** — a JMeter assertion that marks the sample failed if it takes longer than a configured SLA. A slow request therefore counts as an error toward the table above and can fail the build exactly like a wrong status code does, via the existing `jmeter-check-results` / `errorRateThresholdInPercent` gate — no extra Maven wiring was needed.

| Test   | Response Time SLA                                    | Variable(s) |
|--------|-------------------------------------------------------|-------------|
| Smoke  | 1500 ms                                                | `response_time_threshold_ms` |
| Load   | 1500 ms                                                | `response_time_threshold_ms` |
| Stress | 3000 ms                                                | `response_time_threshold_ms` |
| Spike  | 1200 ms during the baseline/recovery thread groups, 3000 ms during the 300-user burst | `response_time_threshold_ms`, `spike_response_time_threshold_ms` |

These defaults live in `pom.xml` (one Maven property per profile, passed to JMeter via `<propertiesUser>`) and as `__P()` fallback defaults directly in each `.jmx` Test Plan, so they can also be overridden without touching either file:

```bash
# Maven
mvn verify -Psmoke -Dresponse.time.threshold.ms=1000

# run-tests.ps1
.\run-tests.ps1 -TestType smoke -ResponseTimeThresholdMs 1000

# raw JMeter CLI
java -jar ApacheJMeter.jar -n -t src/test/jmeter/smoke-test.jmx -l results.jtl -Jresponse_time_threshold_ms=1000
```

## Custom HTML Dashboard

`scripts/generate_dashboard.py` builds a single self-contained HTML page that consolidates whichever `smoke`/`load`/`stress`/`spike` `.jtl` result files it finds, independent of the stock per-run JMeter dashboard. For each test type it shows:

- an overall PASS/FAIL badge against the same error-rate gate as the Maven build
- sample count, error rate, avg/p95 response time, and count of "slow" samples
- a per-endpoint breakdown table with a response-time bar (blue = within SLA, red = over) plotted against the threshold, plus its own independent recheck of elapsed time vs. SLA (so a misconfigured or disabled Duration Assertion can't silently hide a slow endpoint)

Run it locally after any test (it only depends on the Python standard library — no `pip install` needed):

```bash
python scripts/generate_dashboard.py --results-dir results --output results/custom-report/index.html
```

It exits non-zero if any test type it found breached its error-rate gate, so it can also be used as a standalone CI check if needed.