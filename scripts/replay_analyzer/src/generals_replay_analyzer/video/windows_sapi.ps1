param(
    [Parameter(Mandatory = $true)][string]$Destination,
    [Parameter(Mandatory = $true)][string]$VoiceName,
    [Parameter(Mandatory = $true)][string]$TextBase64,
    [Parameter(Mandatory = $true)][int]$SampleRate
)

$ErrorActionPreference = 'Stop'

# TheSuperHackers @bugfix Leex 24/08/2026 Select the exact configured SAPI token name and render deterministic mono PCM without fragile System.Speech enumeration. (#TBD)
try {
    if ($SampleRate -ne 48000) {
        [Console]::Error.WriteLine('unsupported_sample_rate')
        exit 23
    }
    $synthesizer = New-Object -ComObject SAPI.SpVoice
    $stream = $null
    try {
        $selectedVoice = $null
        foreach ($token in @($synthesizer.GetVoices())) {
            $name = [string]$token.GetAttribute('Name')
            if ($name -ceq $VoiceName) {
                $selectedVoice = $token
                break
            }
        }
        if ($null -eq $selectedVoice) {
            [Console]::Error.WriteLine('voice_not_found')
            exit 21
        }
        $text = [System.Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($TextBase64))
        $synthesizer.Voice = $selectedVoice
        $format = New-Object -ComObject SAPI.SpAudioFormat
        # SAPI format type 38 is 48 kHz, 16-bit, mono PCM.
        $format.Type = 38
        $stream = New-Object -ComObject SAPI.SpFileStream
        $stream.Format = $format
        $stream.Open($Destination, 3, $false)
        $synthesizer.AudioOutputStream = $stream
        $synthesizer.Speak($text)
        $stream.Close()
    }
    finally {
        if ($null -ne $stream) {
            [System.Runtime.InteropServices.Marshal]::FinalReleaseComObject($stream) | Out-Null
        }
        if ($null -ne $synthesizer) {
            [System.Runtime.InteropServices.Marshal]::FinalReleaseComObject($synthesizer) | Out-Null
        }
    }
}
catch {
    [Console]::Error.WriteLine(('sapi_error: ' + $_.Exception.Message))
    exit 22
}

