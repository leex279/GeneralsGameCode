param(
    [Parameter(Mandatory = $true)][string]$Destination,
    [Parameter(Mandatory = $true)][string]$VoiceName,
    [Parameter(Mandatory = $true)][string]$TextBase64,
    [Parameter(Mandatory = $true)][int]$SampleRate
)

$ErrorActionPreference = 'Stop'

# TheSuperHackers @feature Leex 24/08/2026 Render one explicit installed SAPI voice to deterministic mono PCM WAV. (#TBD)
try {
    Add-Type -AssemblyName System.Speech
    $synthesizer = New-Object System.Speech.Synthesis.SpeechSynthesizer
    try {
        $installedNames = @($synthesizer.GetInstalledVoices() | ForEach-Object { $_.VoiceInfo.Name })
        if ($installedNames -cnotcontains $VoiceName) {
            [Console]::Error.WriteLine('voice_not_found')
            exit 21
        }
        $text = [System.Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($TextBase64))
        $bits = [System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen
        $channels = [System.Speech.AudioFormat.AudioChannel]::Mono
        $format = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo($SampleRate, $bits, $channels)
        $synthesizer.SelectVoice($VoiceName)
        $synthesizer.SetOutputToWaveFile($Destination, $format)
        $synthesizer.Speak($text)
        $synthesizer.SetOutputToNull()
    }
    finally {
        $synthesizer.Dispose()
    }
}
catch {
    [Console]::Error.WriteLine(('sapi_error: ' + $_.Exception.Message))
    exit 22
}

