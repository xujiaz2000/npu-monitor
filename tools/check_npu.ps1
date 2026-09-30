# =============================================================================
#  check_npu.ps1   (v2 - streaming / fast-feedback)
#
#  Check NPU (Huawei Ascend "npu-smi") usage on remote Atlas A3 servers
#  over SSH with passwordless (key) login.
#
#  Servers  : 192.168.13.174 / 192.168.13.197 / 192.168.13.198 (default)
#
#  Usage examples:
#     powershell -NoProfile -ExecutionPolicy Bypass -File .\check_npu.ps1
#     powershell -NoProfile -ExecutionPolicy Bypass -File .\check_npu.ps1 -Watch 15
#     powershell -NoProfile -ExecutionPolicy Bypass -File .\check_npu.ps1 -User root
#     powershell -NoProfile -ExecutionPolicy Bypass -File .\check_npu.ps1 `
#         -Command "hostname; npu-smi info -t usages -i 0"
#
#  v2 speed changes:
#  - Results print AS EACH SERVER FINISHES (no waiting for the slowest one).
#  - SSH uses publickey auth only (no wasted round-trips on other methods).
#  - Shorter timeouts: connect 5s, exec 30s.
#  - Per-server elapsed time is shown, to tell "network slow" vs "npn-smi slow".
# =============================================================================

param(
    [string[]]$Servers   = @('192.168.13.174', '192.168.13.197', '192.168.13.198', '192.168.13.159'),
    [string]$User        = 'root',
    [int]$Port           = 22,
    [string]$Command     = 'hostname; npu-smi info',
    [int]$ConnectTimeout = 5,    # SSH TCP connect timeout (seconds)
    [int]$ExecTimeout    = 30,   # max wait for remote command output (seconds)
    [int]$Watch          = 0     # >0: auto refresh every N seconds (watch mode)
)

try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }

if (-not (Get-Command ssh -ErrorAction SilentlyContinue)) {
    Write-Host '[FAIL] ssh client not found. Install/enable OpenSSH client (Windows Settings > Optional Features) or add ssh.exe to PATH.' -ForegroundColor Red
    exit 1
}

# -o BatchMode=yes            fast-fail instead of hanging on a password prompt
# -o PreferredAuthentications=publickey   skip other auth methods (fewer round-trips)
$SshAuthArgs = '-o BatchMode=yes -o PreferredAuthentications=publickey -o PasswordAuthentication=no -o StrictHostKeyChecking=accept-new'

function New-SshJob {
    param([string]$Server)

    $target   = if ($User) { "$User@$Server" } else { $Server }
    $quoted   = '"' + $Command + '"'
    $portArgs = if ($Port -ne 22) { "-p $Port " } else { '' }

    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName               = 'ssh'
    $psi.Arguments              = ('-o ConnectTimeout={0} {1} {2}{3} {4}' -f $ConnectTimeout, $SshAuthArgs, $portArgs, $target, $quoted)
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError  = $true
    $psi.UseShellExecute        = $false
    $psi.CreateNoWindow         = $true

    $proc  = [System.Diagnostics.Process]::Start($psi)
    $ready = $proc.StandardOutput.ReadToEndAsync()
    $errs  = $proc.StandardError.ReadToEndAsync()

    return [pscustomobject]@{
        Server  = $Server
        Proc    = $proc
        OutTask = $ready
        ErrTask = $errs
        Sw      = [System.Diagnostics.Stopwatch]::StartNew()
    }
}

function Get-SshResult {
    param($Job)

    $output = ''
    $err    = ''
    $ok     = $false
    $exit   = $null

    # Process has exited: its pipes are closing, give the readers a moment.
    if ($Job.Proc.HasExited) {
        try { $null = $Job.OutTask.Wait(2000) } catch { }
        try { $null = $Job.ErrTask.Wait(2000) } catch { }
        try   { $exit = $Job.Proc.ExitCode } catch { }
        $ok   = ($exit -eq 0)
        if (-not $ok -and -not $err) { $err = "ssh exit code: $exit" }
    } else {
        try { $Job.Proc.Kill() } catch { }
        $err = "timeout after ${ExecTimeout}s (killed)"
    }

    try { if ($Job.OutTask.IsCompleted) { $output = [string]$Job.OutTask.Result } } catch { }
    try { if ($Job.ErrTask.IsCompleted) { $err = (($err, ([string]$Job.ErrTask.Result).Trim() -join '  ')).Trim() } } catch { }
    try { $Job.Proc.Dispose() } catch { }

    [pscustomobject]@{
        Server   = $Job.Server
        Ok       = $ok
        ExitCode = $exit
        Elapsed  = $Job.Sw.Elapsed.TotalSeconds
        Output   = $output
        Error    = $err
    }
}

function Show-Result {
    param($R)

    $state = if ($R.Ok) { '[ OK ]' } else { '[FAIL]' }
    $color = if ($R.Ok) { 'Green' } else { 'Red' }

    Write-Host ''
    Write-Host ('=' * 64) -ForegroundColor DarkCyan
    Write-Host ('  {0}   {1}   {2:N1}s' -f $R.Server, $state, $R.Elapsed) -ForegroundColor $color
    if (-not $R.Ok) { Write-Host ('  ({0})' -f $R.Error) -ForegroundColor Yellow }
    Write-Host ('=' * 64) -ForegroundColor DarkCyan

    $body = $R.Output.TrimEnd()
    if ($body) {
        if ($R.Ok) { Write-Host $body } else { Write-Host $body -ForegroundColor Gray }
    }
}

function Invoke-Check {
    $total = [System.Diagnostics.Stopwatch]::StartNew()

    Write-Host ("[{0}]  Querying {1} servers in parallel (first result appears as soon as each server replies)..." -f (Get-Date -Format 'HH:mm:ss'), @($Servers).Count) -ForegroundColor Cyan

    $all = New-Object System.Collections.Generic.List[object]

    # Launch every connection immediately.
    $pending = @()
    foreach ($s in $Servers) {
        try   { $pending += New-SshJob $s }
        catch {
            $res = [pscustomobject]@{ Server = $s; Ok = $false; ExitCode = $null; Elapsed = 0; Output = ''; Error = 'cannot start ssh: ' + $_.Exception.Message }
            $all.Add($res)
            Show-Result $res
        }
    }

    # Print each result as soon as that server finishes.
    while ($pending.Count -gt 0) {
        $finished = @($pending | Where-Object { $_.Proc.HasExited })
        foreach ($j in $finished) {
            $res = Get-SshResult $j
            $all.Add($res)
            Show-Result $res
            $pending = @($pending | Where-Object { $_ -ne $j })
        }
        if ($pending.Count -gt 0 -and $total.Elapsed.TotalSeconds -ge $ExecTimeout) { break }
        Start-Sleep -Milliseconds 100
    }

    # Deadline reached: kill and report the rest.
    foreach ($j in @($pending)) {
        $res = Get-SshResult $j
        $all.Add($res)
        Show-Result $res
    }

    $okCount = 0
    foreach ($r in $all) { if ($r.Ok) { $okCount++ } }

    Write-Host ''
    Write-Host ("Summary: {0}/{1} servers OK   total {2:N1}s   [{3}]" -f $okCount, @($Servers).Count, $total.Elapsed.TotalSeconds, (Get-Date -Format 'yyyy-MM-dd HH:mm:ss')) -ForegroundColor Cyan
}

# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------
if ($Watch -gt 0) {
    while ($true) {
        Clear-Host
        Invoke-Check
        Write-Host ''
        Write-Host ('Auto refresh: every ' + $Watch + 's  (Ctrl+C to stop)') -ForegroundColor Gray
        Start-Sleep -Seconds $Watch
    }
} else {
    Invoke-Check
}
