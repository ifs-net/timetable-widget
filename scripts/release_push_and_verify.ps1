param(
    [string]$ImageName = "ifsnet/timetable-widget",
    [int]$DigestWaitRetries = 10,
    [int]$DigestWaitSeconds = 6
)

$ErrorActionPreference = "Stop"

function Get-DockerDigest {
    param([Parameter(Mandatory = $true)][string]$Reference)

    $output = docker buildx imagetools inspect $Reference 2>$null
    if ($LASTEXITCODE -ne 0 -or -not $output) {
        return $null
    }

    foreach ($line in $output) {
        if ($line -match '^Digest:\s*(sha256:[0-9a-f]+)$') {
            return $Matches[1]
        }
    }

    return $null
}

function Wait-DockerDigest {
    param(
        [Parameter(Mandatory = $true)][string]$Reference,
        [Parameter(Mandatory = $true)][int]$Retries,
        [Parameter(Mandatory = $true)][int]$SleepSeconds
    )

    for ($i = 1; $i -le $Retries; $i++) {
        $digest = Get-DockerDigest -Reference $Reference
        if ($digest) {
            return $digest
        }
        Start-Sleep -Seconds $SleepSeconds
    }

    return $null
}

$version = (Get-Content VERSION -Raw).Trim()
if (-not $version) {
    throw "VERSION-Datei ist leer oder fehlt."
}

$gitSha = (git rev-parse --short HEAD).Trim()
if (-not $gitSha) {
    throw "Git SHA konnte nicht ermittelt werden."
}

$buildDate = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")

$versionRef = "{0}:{1}" -f $ImageName, $version
$latestRef = "{0}:latest" -f $ImageName

Write-Host "[release] Version: $version"
Write-Host "[release] Git SHA: $gitSha"
Write-Host "[release] Build Date (UTC): $buildDate"
Write-Host "[release] Ziel-Image: $ImageName"

$beforeLatestDigest = Get-DockerDigest -Reference $latestRef
if ($beforeLatestDigest) {
    Write-Host "[release] Vorheriger latest-Digest: $beforeLatestDigest"
} else {
    Write-Host "[release] Kein vorheriger latest-Digest gefunden (erstes Release oder noch nicht verfuegbar)."
}

$buildArgs = @(
    "buildx", "build",
    "--platform", "linux/amd64,linux/arm64",
    "--provenance=false",
    "--sbom=false",
    "--build-arg", "APP_VERSION=$version",
    "--build-arg", "APP_GIT_SHA=$gitSha",
    "--build-arg", "APP_BUILD_DATE=$buildDate",
    "-t", $versionRef,
    "-t", $latestRef,
    "--push", "."
)

Write-Host "[release] Starte Build+Push..."
& docker @buildArgs
if ($LASTEXITCODE -ne 0) {
    throw "docker buildx build --push ist fehlgeschlagen."
}

Write-Host "[release] Warte auf Registry-Digests..."
$versionDigest = Wait-DockerDigest -Reference $versionRef -Retries $DigestWaitRetries -SleepSeconds $DigestWaitSeconds
$latestDigest = Wait-DockerDigest -Reference $latestRef -Retries $DigestWaitRetries -SleepSeconds $DigestWaitSeconds

if (-not $versionDigest) {
    throw "Digest fuer $versionRef konnte nicht ermittelt werden."
}
if (-not $latestDigest) {
    throw "Digest fuer $latestRef konnte nicht ermittelt werden."
}

Write-Host "[release] Digest $versionRef = $versionDigest"
Write-Host "[release] Digest $latestRef  = $latestDigest"

if ($versionDigest -ne $latestDigest) {
    throw "Digest-Mismatch: latest zeigt nicht auf dasselbe Manifest wie die Version $version."
}

if ($beforeLatestDigest -and $beforeLatestDigest -eq $latestDigest) {
    throw "latest-Digest hat sich nicht geaendert. Synology wird vermutlich kein Update erkennen."
}

Write-Host "[release] OK: latest und Versions-Tag zeigen auf dasselbe neue Manifest."