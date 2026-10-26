# dictate.ps1: one turn of a claudevoice call on Windows, transcribed by Windows' own
# speech recognizer (Windows.Media.SpeechRecognition, the engine behind voice typing).
#
# Listens on the default microphone, phrase by phrase, until the Done or Stop flag
# file appears. While the Mute flag holds "1", phrases are dropped (the mic is
# "paused"); what came before is kept. Writes one JSON object to -Out:
#   {"text": "..."} | {"text": "", "cancelled": true} | {"error": "..."}
param(
    [Parameter(Mandatory = $true)][string]$Out,
    [Parameter(Mandatory = $true)][string]$DoneFlag,
    [Parameter(Mandatory = $true)][string]$StopFlag,
    [string]$MuteFlag = '',
    [string]$Heartbeat = '',
    [string]$Language = '',
    [double]$MaxSeconds = 3600
)
$ErrorActionPreference = 'Stop'

function Write-Result($obj) {
    $json = $obj | ConvertTo-Json -Compress
    $tmp = "$Out.tmp"
    [System.IO.File]::WriteAllText($tmp, $json, (New-Object System.Text.UTF8Encoding($false)))
    Move-Item -Force -LiteralPath $tmp -Destination $Out
}

try {
    Add-Type -AssemblyName System.Runtime.WindowsRuntime
    $asTaskOp = [System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
        $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and
        $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' } | Select-Object -First 1
    $asTaskAction = [System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
        $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and
        $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncAction' } | Select-Object -First 1
    function AsTask($op, [Type]$resultType) { $asTaskOp.MakeGenericMethod($resultType).Invoke($null, @($op)) }

    $null = [Windows.Media.SpeechRecognition.SpeechRecognizer, Windows.Media.SpeechRecognition, ContentType = WindowsRuntime]
    $null = [Windows.Globalization.Language, Windows.Globalization, ContentType = WindowsRuntime]
    if ($Language) {
        $recognizer = New-Object Windows.Media.SpeechRecognition.SpeechRecognizer ([Windows.Globalization.Language]::new($Language))
    } else {
        $recognizer = New-Object Windows.Media.SpeechRecognition.SpeechRecognizer
    }
    # No constraints added: the default is free dictation.
    $compiled = (AsTask ($recognizer.CompileConstraintsAsync()) ([Windows.Media.SpeechRecognition.SpeechRecognitionCompilationResult])).GetAwaiter().GetResult()
    if ($compiled.Status -ne [Windows.Media.SpeechRecognition.SpeechRecognitionResultStatus]::Success) {
        throw "the speech recognizer could not start ($($compiled.Status))"
    }
    # Hand back each phrase soon after a pause, and wake up regularly to check the flags.
    $recognizer.Timeouts.InitialSilenceTimeout = [TimeSpan]::FromSeconds(3)
    $recognizer.Timeouts.EndSilenceTimeout = [TimeSpan]::FromSeconds(0.8)
    $recognizer.Timeouts.BabbleTimeout = [TimeSpan]::FromSeconds(0)

    $parts = New-Object System.Collections.Generic.List[string]
    $started = Get-Date
    $finishing = $false
    while ($true) {
        $task = AsTask ($recognizer.RecognizeAsync()) ([Windows.Media.SpeechRecognition.SpeechRecognitionResult])
        $finishWait = $null
        while (-not $task.Wait(200)) {
            if (Test-Path -LiteralPath $StopFlag) { $task = $null; break }
            if ($Heartbeat -and ((Get-Date) - (Get-Item -LiteralPath $Heartbeat).LastWriteTime).TotalSeconds -gt 20) { $task = $null; break }
            if ((Test-Path -LiteralPath $DoneFlag) -and -not $finishing) { $finishing = $true; $finishWait = Get-Date }
            # Done pressed mid-phrase: give the phrase a moment to end, then stop waiting.
            if ($finishing -and $finishWait -and ((Get-Date) - $finishWait).TotalSeconds -gt 4) {
                try { $null = $asTaskAction.Invoke($null, @($recognizer.StopRecognitionAsync())) } catch { }
                $task.Wait(2000) | Out-Null
                break
            }
        }
        if ($null -eq $task) {
            Write-Result @{ text = ''; cancelled = $true }
            exit 0
        }
        if ($task.IsCompleted -and -not $task.IsFaulted -and -not $task.IsCanceled) {
            $result = $task.Result
            $muted = $MuteFlag -and (Test-Path -LiteralPath $MuteFlag) -and ((Get-Content -LiteralPath $MuteFlag -Raw).Trim() -eq '1')
            if (-not $muted -and $result.Status -eq [Windows.Media.SpeechRecognition.SpeechRecognitionResultStatus]::Success -and $result.Text) {
                $parts.Add($result.Text)
            }
        } elseif ($task.IsFaulted) {
            throw $task.Exception.InnerException
        }
        if ($finishing -or (Test-Path -LiteralPath $DoneFlag)) { break }
        if (((Get-Date) - $started).TotalSeconds -gt $MaxSeconds) { break }
    }
    Write-Result @{ text = ($parts -join ' ') }
} catch {
    $why = $_.Exception.Message
    if ($why -match 'privacy|0x80045509') {
        $why = 'turn on Online speech recognition in Windows Settings, Privacy & security, Speech (or pick Whisper in the voice settings)'
    }
    Write-Result @{ error = "Windows speech recognition failed: $why" }
}
