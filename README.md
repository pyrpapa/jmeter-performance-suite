# JMeter Performance Test Suite

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
├── .github/workflows/
│   └── performance-tests.yml   # CI pipeline
├── src/
│   └── test/
│       ├── jmeter/
│       │   ├── smoke-test.jmx
│       │   ├── load-test.jmx
│       │   ├── stress-test.jmx
│       │   └── spike-test.jmx
│       └── resources/
│           └── test-data.csv   # parameterized user/post data
├── results/                    # gitignored, generated at runtime
└── pom.xml

## Prerequisites

- Java 11+
- Maven 3.9+
- JMeter 5.6.3 (for GUI editing only)

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

Reports are generated in `target/jmeter/reports/` after each run.

## CI/CD Pipeline

The GitHub Actions workflow runs automatically on every push to `main`:

1. **Smoke** runs first — gates everything else
2. **Load** runs after smoke passes
3. **Stress** runs after load passes
4. **Spike** runs in parallel with load/stress

Test results are uploaded as artifacts and retained for 30 days in the Actions tab.

## Error Rate Thresholds

Maven will **fail the build** if error rates exceed:

| Test  | Threshold |
|-------|-----------|
| Smoke | 0%        |
| Load  | 2%        |
| Stress| 5%        |
| Spike | 5%        |