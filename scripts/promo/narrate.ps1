$ErrorActionPreference = 'Stop'
$promoProject = (Resolve-Path (Join-Path $PSScriptRoot '../..')).Path
$promoOutput = Join-Path $promoProject 'test-results/promo-v0.1.1'
New-Item -ItemType Directory -Force -Path $promoOutput | Out-Null
$promoLines = Get-Content -LiteralPath (Join-Path $promoProject 'docs/media/narration.json') -Raw -Encoding UTF8 | ConvertFrom-Json
$promoSpeaker = New-Object -ComObject SAPI.SpVoice
$promoVoice = @($promoSpeaker.GetVoices() | Where-Object { $_.GetDescription() -eq 'Microsoft Huihui Desktop - Chinese (Simplified)' })
if ($promoVoice.Count -ne 1) { throw 'Microsoft Huihui Chinese voice is required for this demonstration.' }
$promoSpeaker.Voice = $promoVoice[0]
$promoSpeaker.Rate = 1
$promoSpeaker.Volume = 100
foreach ($promoLine in $promoLines) {
    $promoStream = New-Object -ComObject SAPI.SpFileStream
    $promoStream.Format.Type = 22 # 22.05 kHz / 16-bit / mono PCM
    $promoStream.Open((Join-Path $promoOutput ($promoLine.id + '.wav')), 3, $false)
    try {
        $promoSpeaker.AudioOutputStream = $promoStream
        [void]$promoSpeaker.Speak($promoLine.text)
    } finally {
        $promoStream.Close()
    }
}
Write-Output 'Created five local Chinese narration clips.'
