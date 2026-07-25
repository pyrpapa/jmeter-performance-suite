param(
    [string]$TestType = "smoke",
    [string]$Env = "dev",
    [int]$ResponseTimeThresholdMs = 0,
    [int]$SpikeResponseTimeThresholdMs = 0
)

$JAVA = "C:\Program Files\Eclipse Adoptium\jdk-21.0.11.10-hotspot\bin\java.exe"
$JMETER_JAR = "C:\jmeter\apache-jmeter-5.6.3\bin\ApacheJMeter.jar"
$TEST_FILE = "src\test\jmeter\$TestType-test.jmx"
$RESULTS_FILE = "results\$TestType-results.jtl"
$REPORT_DIR = "results\$TestType-report"
$CUSTOM_REPORT_DIR = "results\custom-report"

# Default response-time SLA per test type, matching pom.xml's per-profile
# response.time.threshold.ms and the Duration Assertions baked into each .jmx.
if ($ResponseTimeThresholdMs -eq 0) {
    $ResponseTimeThresholdMs = switch ($TestType) {
        "smoke"  { 800 }
        "load"   { 1500 }
        "stress" { 3000 }
        "spike"  { 1200 }  # baseline/recovery; burst uses SpikeResponseTimeThresholdMs
        default  { 1000 }
    }
}
if ($SpikeResponseTimeThresholdMs -eq 0) { $SpikeResponseTimeThresholdMs = 3000 }

# Clean previous results and report
if (Test-Path $RESULTS_FILE) { Remove-Item $RESULTS_FILE -Force }
if (Test-Path $REPORT_DIR) { Remove-Item $REPORT_DIR -Recurse -Force }


Write-Host "Running $TestType test against $Env environment (response-time SLA: ${ResponseTimeThresholdMs}ms)..." -ForegroundColor Cyan

& $JAVA -jar $JMETER_JAR -n `
    -t $TEST_FILE `
    -l $RESULTS_FILE `
    -p src\test\resources\user.properties `
    -Jenv=$Env `
    -Jresponse_time_threshold_ms=$ResponseTimeThresholdMs `
    -Jspike_response_time_threshold_ms=$SpikeResponseTimeThresholdMs

# Generate the stock JMeter HTML Report Dashboard from results
& $JAVA -jar $JMETER_JAR -g $RESULTS_FILE -o $REPORT_DIR `
    -p src\test\resources\user.properties

Write-Host "Done. Stock report at: $REPORT_DIR\index.html" -ForegroundColor Green

# Build the custom consolidated dashboard (picks up any other *-results.jtl
# files already sitting in .\results, e.g. from previous runs of other test
# types) so a single open of custom-report\index.html shows everything run
# so far. Requires Python 3 on PATH.
$python = Get-Command python -ErrorAction SilentlyContinue
if (-not $python) { $python = Get-Command python3 -ErrorAction SilentlyContinue }
if ($python) {
    & $python.Source scripts\generate_dashboard.py --results-dir results --output "$CUSTOM_REPORT_DIR\index.html"
    Write-Host "Done. Custom dashboard at: $CUSTOM_REPORT_DIR\index.html" -ForegroundColor Green
} else {
    Write-Host "Python not found on PATH - skipping custom dashboard generation. Install Python 3 and re-run:" -ForegroundColor Yellow
    Write-Host "  python scripts\generate_dashboard.py --results-dir results --output $CUSTOM_REPORT_DIR\index.html" -ForegroundColor Yellow
}