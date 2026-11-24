$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$compiler = Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'
if (-not (Test-Path -LiteralPath $compiler)) { throw '本机缺少 .NET Framework C# 编译器。' }
$source = Join-Path $PSScriptRoot 'Program.cs'
$icon = Join-Path $PSScriptRoot 'suyuan.ico'
$output = Join-Path $root '启动溯源.exe'
& $compiler /nologo /target:winexe /optimize+ /r:System.Windows.Forms.dll /r:System.Drawing.dll "/win32icon:$icon" "/out:$output" $source
if ($LASTEXITCODE -ne 0) { throw '启动器编译失败。' }
Write-Output "已生成 $output"
