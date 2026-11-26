$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$envf = Join-Path $scriptDir 'server\.env'
Write-Output '=== .env key report (secret values hidden) ==='
if (-not (Test-Path $envf)) { Write-Output "MISSING .env at $envf (copy server\.env.example to server\.env)"; }
else {
  Get-Content $envf | ForEach-Object {
    $l = $_.Trim()
    if ($l -eq '' -or $l.StartsWith('#')) { return }
    if ($l.StartsWith('export ')) { $l = $l.Substring(7) }
    if ($l -notmatch '=') { return }
    $parts = $l -split '=', 2
    $k = $parts[0].Trim()
    $v = $parts[1].Trim().Trim('"').Trim("'")
    if ($k -eq 'CONFLUENCE_BASE_URL') {
      if ($v -like 'https://*') { $scheme = 'https OK' }
      elseif ($v -like 'http://*') { $scheme = 'HTTP -> WILL FAIL (server requires https)' }
      else { $scheme = 'no/invalid scheme' }
      Write-Output ("{0} = [set] host: {1} | {2}" -f $k, ($v -replace '^https?://',''), $scheme)
    }
    elseif ($k -eq 'CONFLUENCE_SPACE') { Write-Output ("{0} = {1}" -f $k, $v) }
    else { Write-Output ("{0} = [set, length {1}]" -f $k, $v.Length) }
  }
}
Write-Output ''
Write-Output '=== Toolchain ==='
foreach ($cmd in @('uvx','uv','python')) {
  $c = Get-Command $cmd -ErrorAction SilentlyContinue
  if ($c) { Write-Output ("{0}: {1}" -f $cmd, $c.Source) } else { Write-Output ("{0}: NOT FOUND on PATH" -f $cmd) }
}
Write-Output ''
Write-Output '=== Kiro powers / settings files ==='
$files = @(
  (Join-Path $env:USERPROFILE '.kiro\powers\registries\user-added.json'),
  (Join-Path $env:USERPROFILE '.kiro\powers\installed.json'),
  (Join-Path $env:USERPROFILE '.kiro\settings\mcp.json')
)
foreach ($f in $files) {
  if (Test-Path $f) { Write-Output "EXISTS:  $f" } else { Write-Output "MISSING: $f" }
}
Write-Output ''
Write-Output '=== Server inspect (tool count) ==='
$server = Join-Path $scriptDir 'server\confluence_mcp.py'
Write-Output "Run manually to verify tools load:"
Write-Output "  uvx --from fastmcp fastmcp inspect `"$server`""
