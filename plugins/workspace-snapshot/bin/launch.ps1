$ErrorActionPreference = 'Stop'
$root = if ($env:PLUGIN_ROOT) { $env:PLUGIN_ROOT } else { Split-Path $PSScriptRoot -Parent }
# Windows ARM64 can use the x64 runtime when Windows x64 emulation is available.
$exe = Join-Path $root 'bin\windows-amd64\session-archive.exe'
& $exe @args
exit $LASTEXITCODE
