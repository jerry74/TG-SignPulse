param(
    [string]$ProjectId = 'ffa02fd2-7390-41b7-8326-b48601344cf8',
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^ghcr\.io/jerry74/tg-signplus:sha-[0-9a-f]{12}$')]
    [string]$Image
)

$ErrorActionPreference = 'Stop'
$bws = 'C:\Users\jerry\.codex\skills\bitwarden-secrets-cli\scripts\Invoke-BwsWithStoredToken.ps1'

function Get-SecretValue([string]$Key) {
    $rawInventory = & $bws secret list $ProjectId --output json
    if ($LASTEXITCODE) { throw "Unable to list Bitwarden secrets" }
    $inventory = $rawInventory | ConvertFrom-Json
    $match = @($inventory | Where-Object { $_.key -eq $Key })
    if ($match.Count -ne 1) { throw "Expected exactly one secret named $Key" }
    $secretId = [string]$match[0].id
    $rawRecord = & $bws secret get $secretId --output json
    if ($LASTEXITCODE) { throw "Unable to read Bitwarden secret $Key" }
    $record = $rawRecord | ConvertFrom-Json
    $value = [string]$record.value
    if ([string]::IsNullOrWhiteSpace($value)) { throw "Bitwarden secret $Key is empty" }
    return $value
}

$lines = @(
    "TG_SIGNPLUS_IMAGE=$Image"
    "APP_MASTER_KEY=$(Get-SecretValue 'TG_SIGNPLUS__APP_MASTER_KEY')"
    'ADMIN_USERNAME=admin'
    "ADMIN_PASSWORD=$(Get-SecretValue 'TG_SIGNPLUS__ADMIN_PASSWORD')"
    "TG_API_ID=$(Get-SecretValue 'TG_SIGNPLUS__TG_API_ID')"
    "TG_API_HASH=$(Get-SecretValue 'TG_SIGNPLUS__TG_API_HASH')"
    'TG_PROXY='
)
$content = ($lines -join "`n") + "`n"
$env:TGSP_ENV_CONTENT_BASE64 = [Convert]::ToBase64String(
    [Text.Encoding]::UTF8.GetBytes($content)
)
try {
    python "$PSScriptRoot\write_remote_env.py"
    if ($LASTEXITCODE) { exit $LASTEXITCODE }
    Write-Output 'remote_env_written=true'
}
finally {
    Remove-Item Env:TGSP_ENV_CONTENT_BASE64 -ErrorAction SilentlyContinue
    $content = $null
    $lines = $null
}
