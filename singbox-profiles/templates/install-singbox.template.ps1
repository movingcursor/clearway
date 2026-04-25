#Requires -RunAsAdministrator
#Requires -Version 5.1

# install-singbox.ps1
# ------------------------------------------------------------------
# One-shot installer for sing-box on Windows 11 as an NSSM-managed
# service, plus a Scheduled Task that pulls a fresh config from
# __PROFILE_HOST__ every hour and restarts the service when it changes.
#
# WHAT it sets up:
#   1. C:\Program Files\sing-box\sing-box.exe       (pinned version)
#   2. C:\Program Files\sing-box\nssm.exe           (service wrapper)
#   3. C:\Program Files\sing-box\config.json        (initial fetch)
#   4. C:\Program Files\sing-box\update-config.ps1  (auto-updater)
#   5. NSSM Windows service "sing-box" (auto-start, restart on crash)
#   6. Scheduled Task "sing-box config updater"
#        - runs at boot
#        - repeats hourly under SYSTEM
#
# WHY NSSM wraps sing-box:
#   - `sing-box.exe` alone would die with the user session; NSSM makes it
#     a proper Windows service so it survives logoff and restarts on crash.
#   - NSSM also handles log rotation, so logs don't grow unbounded.
#
# Usage (admin PowerShell):
#   iwr https://__PROFILE_HOST__/p/<USER_SECRET>/install-singbox.ps1 -OutFile $env:TEMP\sb.ps1
#   & $env:TEMP\sb.ps1
#
# Re-running is SAFE: existing service is stopped + reinstalled, the
# previous config is kept as config.json.prev before overwrite.
# ------------------------------------------------------------------

$ErrorActionPreference = 'Stop'
# Force TLS 1.2 — older Windows defaults to TLS 1.0 and fails GitHub releases.
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12

# --- Version policy --------------------------------------------------
# sing-box version is read from $VERSION_URL at install time so the
# installer always matches whatever the server is running — no static
# pin to drift. NSSM 2.24 (last stable, works on Win11) and wintun 0.14.1
# are pinned in $NSSM_SHA256 / $WINTUN_SHA256 below because upstream
# moves glacially and sha256 integrity > auto-track.

# --- Fixed paths ----------------------------------------------------
$INSTALL_DIR = 'C:\Program Files\sing-box'
$LOG_DIR     = Join-Path $INSTALL_DIR 'logs'
$BIN         = Join-Path $INSTALL_DIR 'sing-box.exe'
$NSSM        = Join-Path $INSTALL_DIR 'nssm.exe'
$CONFIG      = Join-Path $INSTALL_DIR 'config.json'
$UPDATER     = Join-Path $INSTALL_DIR 'update-config.ps1'
$SVC_LOG     = Join-Path $LOG_DIR    'service.log'
$SVC_ERRLOG  = Join-Path $LOG_DIR    'service.err.log'

# --- Remote profile (per-user secret in the path) -------------------
# Values between __..__ are substituted by generate-installer.sh per user.
$REMOTE_URL  = 'https://__PROFILE_HOST__/p/__USER_SECRET__/__CONFIG_FILENAME__'
# Plain text file containing the server's currently-running sing-box
# version (e.g. "1.13.10"). Written by publish-version.sh on the server
# every 5 min. Lets the laptop auto-track server upgrades. Lives under
# the shared /mirror/ path because the version isn't secret and every
# Windows client should track the same server version.
$VERSION_URL = 'https://__PROFILE_HOST__/mirror/sing-box-version.txt'
# Optional notification webhook URL (Discord, Slack, generic JSON POST).
# Empty string disables notifications — typically reserved for admin users.
$WEBHOOK_URL = '__WEBHOOK_URL__'
# Mirror of nssm-2.24/win64/nssm.exe — served by our own nginx to avoid
# the flaky upstream nssm.cc. sha256 pinned below for integrity.
$NSSM_URL    = 'https://__PROFILE_HOST__/mirror/nssm.exe'
$NSSM_SHA256 = 'f689ee9af94b00e9e3f0bb072b34caaf207f32dcb4f5782fc9ca351df9a06c97'
# Mirror of wintun-0.14.1/bin/amd64/wintun.dll. sing-box releases don't
# bundle wintun.dll anymore, so we fetch it explicitly here.
$WINTUN_URL    = 'https://__PROFILE_HOST__/mirror/wintun.dll'
$WINTUN_SHA256 = 'e5da8447dc2c320edc0fc52fa01885c103de8c118481f683643cacc3220dafce'

# --- Service / task names ------------------------------------------
$SVC_NAME     = 'sing-box'
$TASK_NAME    = 'sing-box config updater'
$UPDATE_HOURS = 1

function Log($msg) { Write-Host "[install-singbox] $msg" -ForegroundColor Cyan }

# Retry wrapper for Invoke-WebRequest. Every fetch in this installer
# crosses __PROFILE_HOST__ over whatever network the user happens to be on —
# GFW-adjacent TLS handshakes routinely stall once on the first packet
# and succeed immediately on a retry. Three attempts with 2s + 5s
# backoff turns a one-packet hiccup from "install aborted" into a
# sub-10s delay. For calls that already have a fallback URL (mirror ->
# GitHub), callers pass -MaxAttempts 2 to keep the fallback path snappy.
function Invoke-WebRequestRetry {
    param(
        [Parameter(Mandatory=$true)][string]$Uri,
        [string]$OutFile,
        [int]$TimeoutSec = 30,
        [int]$MaxAttempts = 3
    )
    $delays = @(2, 5, 10)
    for ($attempt = 1; $attempt -le $MaxAttempts; $attempt++) {
        try {
            if ($OutFile) {
                return Invoke-WebRequest -Uri $Uri -OutFile $OutFile -UseBasicParsing -TimeoutSec $TimeoutSec
            } else {
                return Invoke-WebRequest -Uri $Uri -UseBasicParsing -TimeoutSec $TimeoutSec
            }
        } catch {
            if ($attempt -eq $MaxAttempts) { throw }
            $delay = $delays[$attempt - 1]
            Log "fetch $Uri failed (attempt $attempt/$MaxAttempts): $($_.Exception.Message); retrying in ${delay}s"
            Start-Sleep -Seconds $delay
        }
    }
}

# 1. Make install + log dirs (idempotent) -----------------------------
Log "Preparing $INSTALL_DIR + $LOG_DIR"
New-Item -ItemType Directory -Force -Path $INSTALL_DIR, $LOG_DIR | Out-Null

# 1a. DO NOT stop the service yet ------------------------------------
# We stop it only in step 5, after all downloads are complete. Reason:
# CN-resident users reach __PROFILE_HOST__ ONLY through the sing-box tunnel
# (Oracle IP is GFW-blocked on the direct ISP path). Stopping the
# service first would tear down the only network route to our own
# mirror, making the installer unrunnable on re-provisioning. Instead,
# all downloads land in $env:TEMP while the tunnel stays up, then we
# stop the service, copy into $INSTALL_DIR (locked files), and restart.

# 2. Download + extract sing-box -------------------------------------
# Resolve the target version from the server's published version file,
# then prefer the /mirror/ copy (reachable from CN) over GitHub.
# Use Get-ChildItem -Recurse to find the binary so we don't depend on
# the exact folder layout inside the release zip (it's changed before).
Log "Resolving sing-box version from $VERSION_URL"
$SB_VERSION = (Invoke-WebRequestRetry -Uri $VERSION_URL -TimeoutSec 15).Content.Trim()
if ($SB_VERSION -notmatch '^\d+\.\d+\.\d+$') {
    throw "VERSION_URL returned non-semver '$SB_VERSION'"
}

$sbZip     = Join-Path $env:TEMP "sing-box-$SB_VERSION.zip"
$sbExtract = Join-Path $env:TEMP "sing-box-extract"
$mirrorSbUrl = "https://__PROFILE_HOST__/mirror/sing-box-$SB_VERSION-windows-amd64.zip"
$mirrorShaUrl = "$mirrorSbUrl.sha256"
$ghSbUrl      = "https://github.com/SagerNet/sing-box/releases/download/v$SB_VERSION/sing-box-$SB_VERSION-windows-amd64.zip"

$sbFetched = $false
try {
    Log "Downloading sing-box $SB_VERSION from mirror"
    # MaxAttempts=2: we have a GitHub fallback, so don't spend 6+ minutes
    # retrying a genuinely-broken mirror before falling through.
    Invoke-WebRequestRetry -Uri $mirrorSbUrl -OutFile $sbZip -TimeoutSec 120 -MaxAttempts 2 | Out-Null
    try {
        $expected = (Invoke-WebRequestRetry -Uri $mirrorShaUrl -TimeoutSec 15 -MaxAttempts 2).Content.Split()[0].ToLower()
        $got = (Get-FileHash -Algorithm SHA256 $sbZip).Hash.ToLower()
        if ($expected -and $got -eq $expected) {
            Log "mirror sha256 verified"
            $sbFetched = $true
        } else {
            Log "mirror sha256 mismatch (expected $expected, got $got); trying GitHub"
        }
    } catch {
        Log "mirror sha256 not published; accepting mirror copy anyway"
        $sbFetched = $true
    }
} catch {
    Log "mirror unreachable ($($_.Exception.Message)); trying GitHub"
}

if (-not $sbFetched) {
    Log "Downloading sing-box $SB_VERSION from GitHub"
    Invoke-WebRequestRetry -Uri $ghSbUrl -OutFile $sbZip -TimeoutSec 120 | Out-Null
}

Remove-Item -Recurse -Force $sbExtract -ErrorAction SilentlyContinue
Expand-Archive -Path $sbZip -DestinationPath $sbExtract -Force

$sbExe = Get-ChildItem -Path $sbExtract -Filter 'sing-box.exe' -Recurse | Select-Object -First 1
if (-not $sbExe) { throw "sing-box.exe not found in release zip" }
# Stash the source path; copy into $BIN happens after we stop the service.
$sbExeSrc = $sbExe.FullName

# wintun.dll is required by sing-box's TUN inbound on Windows and must
# sit next to sing-box.exe. Download to TEMP while the service is still
# up (and therefore our tunnel is still carrying traffic to the mirror);
# we'll copy it into $INSTALL_DIR after the service stop.
Log "Downloading wintun.dll 0.14.1 (mirrored)"
$wintunTmp = Join-Path $env:TEMP 'wintun.dll'
Invoke-WebRequestRetry -Uri $WINTUN_URL -OutFile $wintunTmp -TimeoutSec 60 | Out-Null
$gotWintunSha = (Get-FileHash -Algorithm SHA256 $wintunTmp).Hash.ToLower()
if ($gotWintunSha -ne $WINTUN_SHA256.ToLower()) {
    throw "wintun.dll sha256 mismatch: expected $WINTUN_SHA256, got $gotWintunSha"
}

# 3. Download NSSM (mirrored from our server, not nssm.cc) -----------
# Same TEMP-first pattern: nssm.exe is the service host and is locked
# while the service runs, so we can't write it directly yet.
Log "Downloading NSSM 2.24 (win64, mirrored)"
$nssmTmp = Join-Path $env:TEMP 'nssm.exe'
Invoke-WebRequestRetry -Uri $NSSM_URL -OutFile $nssmTmp -TimeoutSec 60 | Out-Null

$gotSha = (Get-FileHash -Algorithm SHA256 $nssmTmp).Hash.ToLower()
if ($gotSha -ne $NSSM_SHA256.ToLower()) {
    throw "nssm.exe sha256 mismatch: expected $NSSM_SHA256, got $gotSha"
}

# 4. Initial config fetch + validation -------------------------------
# Validate BEFORE installing the service — a broken config means a
# crash-loop on first start, which we'd rather catch here.
Log "Fetching initial config from $REMOTE_URL"
$tmpCfg = Join-Path $env:TEMP 'singbox-windows.initial.json'
Invoke-WebRequestRetry -Uri $REMOTE_URL -OutFile $tmpCfg -TimeoutSec 30 | Out-Null

Log "Validating with sing-box check (using freshly downloaded binary)"
# Use the freshly-downloaded $sbExeSrc rather than $BIN: $BIN may be
# locked by the running service, and may also be an older version whose
# schema rejects a newer config. Validate under the version we're about
# to install.
& $sbExeSrc check -c $tmpCfg
if ($LASTEXITCODE -ne 0) { throw "sing-box check failed on freshly downloaded config" }

# --- Stop the service now that all downloads are in TEMP -------------
# From here on we write into $INSTALL_DIR (locked paths). This is the
# only window the tunnel is down; on CN networks the rest of the
# installer must not require internet access to __PROFILE_HOST__.
$existingSvc = Get-Service -Name $SVC_NAME -ErrorAction SilentlyContinue
if ($existingSvc -and $existingSvc.Status -ne 'Stopped') {
    Log "Stopping running $SVC_NAME service before swapping binaries"
    Stop-Service -Name $SVC_NAME -Force -ErrorAction SilentlyContinue
    for ($i = 0; $i -lt 10; $i++) {
        Start-Sleep -Milliseconds 500
        $s = (Get-Service -Name $SVC_NAME -ErrorAction SilentlyContinue).Status
        if ($s -eq 'Stopped') { break }
    }
}

# --- Copy TEMP artifacts into $INSTALL_DIR ---------------------------
Copy-Item $sbExeSrc $BIN -Force
Copy-Item $wintunTmp (Join-Path $INSTALL_DIR 'wintun.dll') -Force
Copy-Item $nssmTmp $NSSM -Force

if (Test-Path $CONFIG) { Copy-Item $CONFIG "$CONFIG.prev" -Force }
Move-Item $tmpCfg $CONFIG -Force
Log "Config installed at $CONFIG"

# 5. Write updater script (called hourly by Scheduled Task) ----------
# Single-quoted here-string so $variables aren't interpolated; we then
# substitute the install-time values via .Replace() so the on-disk
# script is fully self-contained (no env lookups at run time).
Log "Writing updater to $UPDATER"
$updaterContent = @'
# update-config.ps1 — fetches the remote sing-box config AND binary
# version pointer, applies either/both if they have changed, and
# restarts the Windows service exactly once per run (or not at all if
# nothing changed). Logs every run to logs\update-config.log.
# Triggered by Scheduled Task hourly + at boot under SYSTEM.

$ErrorActionPreference = 'Stop'
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12

$REMOTE_URL  = '__REMOTE_URL__'
$VERSION_URL = '__VERSION_URL__'
$WEBHOOK_URL = '__WEBHOOK_URL__'
$INSTALL_DIR = '__INSTALL_DIR__'
$BIN     = Join-Path $INSTALL_DIR 'sing-box.exe'
$CONFIG  = Join-Path $INSTALL_DIR 'config.json'
$LOG     = Join-Path $INSTALL_DIR 'logs\update-config.log'
$SVC     = '__SVC_NAME__'
$HOST_TAG = "[laptop $env:COMPUTERNAME]"

function Log($msg) {
    $line = "$(Get-Date -Format o) $msg"
    Add-Content -Path $LOG -Value $line
}

# Retry wrapper — same rationale as install-time. The hourly task runs
# unattended, so without retries a single packet hiccup silently eats
# that hour's update. 3 attempts + 2s/5s backoff; MaxAttempts=2 on
# mirror calls that already fall back to GitHub.
function Invoke-WebRequestRetry {
    param(
        [Parameter(Mandatory=$true)][string]$Uri,
        [string]$OutFile,
        [int]$TimeoutSec = 30,
        [int]$MaxAttempts = 3
    )
    $delays = @(2, 5, 10)
    for ($attempt = 1; $attempt -le $MaxAttempts; $attempt++) {
        try {
            if ($OutFile) {
                return Invoke-WebRequest -Uri $Uri -OutFile $OutFile -UseBasicParsing -TimeoutSec $TimeoutSec
            } else {
                return Invoke-WebRequest -Uri $Uri -UseBasicParsing -TimeoutSec $TimeoutSec
            }
        } catch {
            if ($attempt -eq $MaxAttempts) { throw }
            $delay = $delays[$attempt - 1]
            Log "fetch $Uri failed (attempt $attempt/$MaxAttempts): $($_.Exception.Message); retrying in ${delay}s"
            Start-Sleep -Seconds $delay
        }
    }
}

# Fire-and-forget Discord notification. Any failure (no internet, 4xx, 5xx)
# is swallowed — notifications are best-effort, we never want them to
# break the updater itself.
function Notify($msg) {
    if ([string]::IsNullOrEmpty($WEBHOOK_URL)) { return }
    try {
        $body = @{ content = "$HOST_TAG $msg" } | ConvertTo-Json -Compress
        Invoke-WebRequest -Uri $WEBHOOK_URL -Method POST `
            -ContentType 'application/json' -Body $body `
            -UseBasicParsing -TimeoutSec 10 | Out-Null
    } catch { Log "notify: failed ($($_.Exception.Message))" }
}

# Returns $true if the config was replaced and a service restart is needed.
function Update-Config {
    $tmp = Join-Path $env:TEMP 'singbox-windows.fetched.json'
    Invoke-WebRequestRetry -Uri $REMOTE_URL -OutFile $tmp -TimeoutSec 30 | Out-Null

    $newHash = (Get-FileHash -Algorithm SHA256 $tmp).Hash
    $oldHash = if (Test-Path $CONFIG) { (Get-FileHash -Algorithm SHA256 $CONFIG).Hash } else { '' }

    if ($newHash -eq $oldHash) {
        Log "config: no change (sha256 $newHash)"
        Remove-Item $tmp -Force
        return $false
    }

    # Validate the new config BEFORE swapping. If sing-box rejects it
    # we keep the current config + service running.
    & $BIN check -c $tmp 2>&1 | Out-Null
    if ($LASTEXITCODE -ne 0) {
        Log "config: FAIL sing-box check rejected fetched config; keeping current"
        Notify "config update FAILED: sing-box check rejected fetched config; keeping current"
        Remove-Item $tmp -Force
        return $false
    }

    if (Test-Path $CONFIG) { Copy-Item $CONFIG "$CONFIG.prev" -Force }
    Move-Item $tmp $CONFIG -Force
    $shortOld = if ($oldHash) { $oldHash.Substring(0,8) } else { '(none)' }
    $shortNew = $newHash.Substring(0,8)
    Log "config: replaced (sha256 $oldHash -> $newHash)"
    Notify "config updated ($shortOld -> $shortNew)"
    return $true
}

# Returns $true if the binary was replaced and a service restart is needed.
# Fails soft on any error — we never want a binary-upgrade problem to break
# an otherwise-working VPN. On failure we log and leave the old binary in place.
function Update-Binary {
    try {
        $wantVer = (Invoke-WebRequestRetry -Uri $VERSION_URL -TimeoutSec 15).Content.Trim()
        if ($wantVer -notmatch '^\d+\.\d+\.\d+$') {
            Log "binary: server returned non-semver '$wantVer'; skipping"
            return $false
        }

        # `sing-box version` prints "sing-box version X.Y.Z" on the first line.
        $haveVer = ''
        try {
            $verLine = (& $BIN version 2>&1) | Select-Object -First 1
            if ($verLine -match 'version\s+(\d+\.\d+\.\d+)') { $haveVer = $Matches[1] }
        } catch { }

        if ($haveVer -eq $wantVer) {
            Log "binary: up to date ($haveVer)"
            return $false
        }
        Log "binary: upgrade $haveVer -> $wantVer"

        # Fetch the release zip from our own mirror first (reachable from
        # CN, where github.com release downloads are routinely throttled
        # or blocked). Fall back to GitHub if the mirror doesn't have
        # this version yet — publish-version.sh runs every 5 min server-
        # side, but a client polling in that window would otherwise fail.
        $tmpZip = Join-Path $env:TEMP "sing-box-$wantVer.zip"
        $tmpDir = Join-Path $env:TEMP "sing-box-$wantVer-extract"
        $mirrorZipUrl = "https://__PROFILE_HOST__/mirror/sing-box-$wantVer-windows-amd64.zip"
        $mirrorShaUrl = "$mirrorZipUrl.sha256"
        $ghUrl        = "https://github.com/SagerNet/sing-box/releases/download/v$wantVer/sing-box-$wantVer-windows-amd64.zip"

        $fetched = $false
        # Try mirror first. If sha256 is published, verify; a mismatch
        # (or any fetch error) drops through to GitHub so we never apply
        # a corrupted mirror copy.
        try {
            # MaxAttempts=2: GitHub fallback below handles a genuinely-down mirror.
            Invoke-WebRequestRetry -Uri $mirrorZipUrl -OutFile $tmpZip -TimeoutSec 120 -MaxAttempts 2 | Out-Null
            try {
                $expected = (Invoke-WebRequestRetry -Uri $mirrorShaUrl -TimeoutSec 15 -MaxAttempts 2).Content.Split()[0].ToLower()
                $got = (Get-FileHash -Algorithm SHA256 $tmpZip).Hash.ToLower()
                if ($expected -and $got -eq $expected) {
                    Log "binary: fetched $wantVer from mirror (sha256 verified)"
                    $fetched = $true
                } else {
                    Log "binary: mirror sha256 mismatch (expected $expected, got $got); falling back to GitHub"
                    Remove-Item $tmpZip -Force -ErrorAction SilentlyContinue
                }
            } catch {
                # No .sha256 file published — accept the mirror copy anyway.
                # Mirror is same-origin as the config/version files we
                # already trust, so this is consistent with the overall
                # trust model.
                Log "binary: fetched $wantVer from mirror (no sha256 published)"
                $fetched = $true
            }
        } catch {
            Log "binary: mirror fetch failed ($($_.Exception.Message)); falling back to GitHub"
        }

        if (-not $fetched) {
            Invoke-WebRequestRetry -Uri $ghUrl -OutFile $tmpZip -TimeoutSec 120 | Out-Null
            Log "binary: fetched $wantVer from GitHub"
        }

        Remove-Item -Recurse -Force $tmpDir -ErrorAction SilentlyContinue
        Expand-Archive -Path $tmpZip -DestinationPath $tmpDir -Force
        $newExe = Get-ChildItem -Path $tmpDir -Filter 'sing-box.exe' -Recurse | Select-Object -First 1
        if (-not $newExe) {
            Log "binary: sing-box.exe not in release zip; skipping"
            Notify "binary upgrade to $wantVer FAILED: sing-box.exe not in release zip"
            return $false
        }

        # Smoke-test the downloaded binary BEFORE stopping the service.
        # A broken exe that can't even print --version should NOT replace
        # the working one.
        $smoke = & $newExe.FullName version 2>&1
        if ($LASTEXITCODE -ne 0 -or "$smoke" -notmatch 'version\s+\d+\.\d+\.\d+') {
            Log "binary: smoke-test failed on downloaded exe; skipping"
            Notify "binary upgrade to $wantVer FAILED: downloaded exe smoke-test failed"
            return $false
        }

        # Also validate the CURRENT config parses under the new binary —
        # minor version bumps have broken config schemas before, so catch
        # that before we tear the service down.
        & $newExe.FullName check -c $CONFIG 2>&1 | Out-Null
        if ($LASTEXITCODE -ne 0) {
            Log "binary: new exe rejects current config; keeping old binary"
            Notify "binary upgrade to $wantVer SKIPPED: new exe rejects current config (schema drift?); keeping $haveVer"
            return $false
        }

        # Stop service so we can replace the locked exe, then swap + let
        # the caller restart. Keep the previous exe as .prev for manual
        # rollback.
        Stop-Service -Name $SVC -Force -ErrorAction SilentlyContinue
        for ($i = 0; $i -lt 10; $i++) {
            Start-Sleep -Milliseconds 500
            if ((Get-Service -Name $SVC).Status -eq 'Stopped') { break }
        }

        if (Test-Path $BIN) { Copy-Item $BIN "$BIN.prev" -Force }
        Copy-Item $newExe.FullName $BIN -Force
        Log "binary: installed sing-box.exe $wantVer"
        Notify "sing-box upgraded $haveVer -> $wantVer"
        return $true
    }
    catch {
        Log "binary: ERROR $($_.Exception.Message)"
        Notify "binary upgrade ERROR: $($_.Exception.Message)"
        return $false
    }
}

try {
    $cfgChanged = Update-Config
    $binChanged = Update-Binary

    if ($cfgChanged -or $binChanged) {
        # Single restart covers both changes. If Update-Binary stopped the
        # service, Restart-Service handles the Stopped->Running transition.
        Restart-Service -Name $SVC -Force
        Start-Sleep -Seconds 2
        $st = (Get-Service -Name $SVC).Status
        Log "$SVC restarted (cfg=$cfgChanged bin=$binChanged, status=$st)"
        if ($st -ne 'Running') {
            Notify "service did NOT reach Running after restart (status=$st, cfg=$cfgChanged bin=$binChanged)"
        }
    }
}
catch {
    Log "ERROR $($_.Exception.Message)"
    Notify "updater ERROR: $($_.Exception.Message)"
    exit 1
}
'@
$updaterContent = $updaterContent.
    Replace('__REMOTE_URL__', $REMOTE_URL).
    Replace('__VERSION_URL__', $VERSION_URL).
    Replace('__WEBHOOK_URL__', $WEBHOOK_URL).
    Replace('__INSTALL_DIR__', $INSTALL_DIR).
    Replace('__SVC_NAME__', $SVC_NAME)
Set-Content -Path $UPDATER -Value $updaterContent -Encoding UTF8

# 6. Install / refresh the NSSM service ------------------------------
# If the service exists, stop + remove first so we get clean nssm
# parameters even if a previous install used different flags.
$existing = Get-Service -Name $SVC_NAME -ErrorAction SilentlyContinue
if ($existing) {
    Log "Stopping + removing existing $SVC_NAME service"
    & $NSSM stop   $SVC_NAME         | Out-Null
    & $NSSM remove $SVC_NAME confirm | Out-Null
}

Log "Installing $SVC_NAME service via NSSM"
& $NSSM install $SVC_NAME $BIN | Out-Null
# Use paths RELATIVE to AppDirectory so there are no spaces in
# AppParameters. PowerShell's native-command argument passing strips
# inner quotes around "C:\Program Files\..." before nssm.exe receives
# them, leaving sing-box to parse "C:\Program" as the config path
# ("Incorrect function" at startup). Relative paths sidestep that
# entirely — sing-box resolves them against AppDirectory.
& $NSSM set $SVC_NAME AppParameters 'run -c config.json -D .' | Out-Null
& $NSSM set $SVC_NAME AppDirectory   $INSTALL_DIR | Out-Null
& $NSSM set $SVC_NAME DisplayName    'sing-box' | Out-Null
& $NSSM set $SVC_NAME Description    'sing-box VPN client (NSSM-managed; config auto-updated hourly from __PROFILE_HOST__)' | Out-Null
& $NSSM set $SVC_NAME Start          SERVICE_AUTO_START | Out-Null
# Log to disk + rotate at 10MiB so the disk doesn't fill up over time.
& $NSSM set $SVC_NAME AppStdout      $SVC_LOG    | Out-Null
& $NSSM set $SVC_NAME AppStderr      $SVC_ERRLOG | Out-Null
& $NSSM set $SVC_NAME AppRotateFiles 1           | Out-Null
& $NSSM set $SVC_NAME AppRotateOnline 1          | Out-Null
& $NSSM set $SVC_NAME AppRotateBytes 10485760    | Out-Null
# Auto-restart on crash with a 5s back-off.
& $NSSM set $SVC_NAME AppExit Default Restart    | Out-Null
& $NSSM set $SVC_NAME AppRestartDelay 5000       | Out-Null

# 7. Scheduled Task: hourly + at boot, runs as SYSTEM ----------------
# SYSTEM so it works without a logged-in user; -WindowStyle Hidden so
# even if a user is logged in it doesn't flash a console.
Log "Registering Scheduled Task '$TASK_NAME'"
$action = New-ScheduledTaskAction -Execute 'powershell.exe' `
    -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$UPDATER`""
$triggerBoot   = New-ScheduledTaskTrigger -AtStartup
# Start in 2 min so the first hourly run happens after install settles,
# then repeat every $UPDATE_HOURS hour indefinitely (default duration
# in modern Windows is unbounded when -RepetitionDuration is omitted).
$triggerHourly = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(2) `
    -RepetitionInterval (New-TimeSpan -Hours $UPDATE_HOURS)
$principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
$settings  = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 5)
Register-ScheduledTask -TaskName $TASK_NAME -Action $action `
    -Trigger @($triggerBoot, $triggerHourly) `
    -Principal $principal -Settings $settings -Force | Out-Null

# 8. Start the service + sanity check --------------------------------
Log "Starting $SVC_NAME"
& $NSSM start $SVC_NAME | Out-Null
Start-Sleep -Seconds 3
$svc = Get-Service -Name $SVC_NAME
Log "Service status: $($svc.Status)"
if ($svc.Status -ne 'Running') {
    Log "WARNING service did not reach Running. Inspect $SVC_ERRLOG"
}

Log "DONE."
Log "  Service log:  $SVC_LOG"
Log "  Updater log:  $($LOG_DIR)\update-config.log"
Log "  Updater fires every $UPDATE_HOURS h + at boot."

# Fire one Discord notification from the installer too, so the operator
# sees in the same channel when a laptop is (re)provisioned. Same
# best-effort swallow-all-errors pattern as the updater's Notify.
try {
    if ($WEBHOOK_URL) {
        $installedVer = ''
        try {
            $verLine = (& $BIN version 2>&1) | Select-Object -First 1
            if ($verLine -match 'version\s+(\d+\.\d+\.\d+)') { $installedVer = $Matches[1] }
        } catch { }
        $msg = "installer completed on $env:COMPUTERNAME (sing-box $installedVer, service=$($svc.Status))"
        $body = @{ content = "[laptop $env:COMPUTERNAME] $msg" } | ConvertTo-Json -Compress
        Invoke-WebRequest -Uri $WEBHOOK_URL -Method POST `
            -ContentType 'application/json' -Body $body `
            -UseBasicParsing -TimeoutSec 10 | Out-Null
    }
} catch { Log "notify: install-complete notification failed ($($_.Exception.Message))" }
