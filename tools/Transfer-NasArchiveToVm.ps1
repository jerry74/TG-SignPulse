param(
    [Parameter(Mandatory = $true)][string]$NasArchivePath,
    [Parameter(Mandatory = $true)][string]$VmArchivePath,
    [string]$ProjectId = 'ffa02fd2-7390-41b7-8326-b48601344cf8'
)

$ErrorActionPreference = 'Stop'
$bws = 'C:\Users\jerry\.codex\skills\bitwarden-secrets-cli\scripts\Invoke-BwsWithStoredToken.ps1'
$raw = & $bws secret list $ProjectId --output json
if ($LASTEXITCODE) { throw 'Unable to list Bitwarden secrets' }
$inventory = $raw | ConvertFrom-Json
$match = @($inventory | Where-Object { $_.key -eq 'INFRA__NAS__SSH_PASSWORD' })
if ($match.Count -ne 1) { throw 'Expected one NAS SSH password secret' }
$secretId = [string]$match[0].id
$record = (& $bws secret get $secretId --output json) | ConvertFrom-Json
if ($LASTEXITCODE) { throw 'Unable to read NAS SSH password' }
$env:NAS_PASSWORD = [string]$record.value
$env:NAS_ARCHIVE_PATH = $NasArchivePath
$env:VM_ARCHIVE_PATH = $VmArchivePath
try {
    python "$PSScriptRoot\transfer_nas_archive.py"
    if ($LASTEXITCODE) { exit $LASTEXITCODE }
    Write-Output 'archive_transferred=true'
}
finally {
    Remove-Item Env:NAS_PASSWORD,Env:NAS_ARCHIVE_PATH,Env:VM_ARCHIVE_PATH -ErrorAction SilentlyContinue
}
