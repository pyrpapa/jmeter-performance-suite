param(
    [string]$TestType = "smoke",
    [string]$Env = "dev"
)

$JAVA = "C:\Program Files\Eclipse Adoptium\jdk-21.0.11.10-hotspot\bin\java.exe"
$JMETER_JAR = "C:\jmeter\apache-jmeter-5.6.3\bin\ApacheJMeter.jar"
$TEST_FILE = "src\test\jmeter\$TestType-test.jmx"
$RESULTS_FILE = "results\$TestType-results.jtl"
$REPORT_DIR = "results\$TestType-report"

# Clean previous results and report
if (Test-Path $RESULTS_FILE) { Remove-Item $RESULTS_FILE -Force }
if (Test-Path $REPORT_DIR) { Remove-Item $REPORT_DIR -Recurse -Force }


Write-Host "Running $TestType test against $Env environment..." -ForegroundColor Cyan

& $JAVA -jar $JMETER_JAR -n `
    -t $TEST_FILE `
    -l $RESULTS_FILE `
    -p src\test\resources\user.properties `
    -Jenv=$Env

# Generate report from results
& $JAVA -jar $JMETER_JAR -g $RESULTS_FILE -o $REPORT_DIR `
    -p src\test\resources\user.properties

Write-Host "Done. Report at: $REPORT_DIR\index.html" -ForegroundColor Green