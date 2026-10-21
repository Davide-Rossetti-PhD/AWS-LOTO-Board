# Build the openpyxl Lambda Layer (Windows PowerShell)
# Run this ONCE before sam build/deploy
# Creates the correct directory structure for a Python Lambda Layer

$LayerDir = Join-Path $PSScriptRoot "openpyxl"
$TargetDir = Join-Path $LayerDir "python"

Write-Host "Building openpyxl Lambda Layer..." -ForegroundColor Cyan

# Clean previous build
if (Test-Path $TargetDir) { Remove-Item -Recurse -Force $TargetDir }
New-Item -ItemType Directory -Path $TargetDir -Force | Out-Null

# Install openpyxl into the layer structure
pip install openpyxl==3.1.5 -t $TargetDir --no-cache-dir --quiet

# Remove unnecessary files
Get-ChildItem -Path $TargetDir -Recurse -Directory -Filter "__pycache__" | Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
Get-ChildItem -Path $TargetDir -Recurse -Directory -Filter "*.dist-info" | Remove-Item -Recurse -Force -ErrorAction SilentlyContinue

Write-Host "✅ Layer built at: $TargetDir" -ForegroundColor Green
Write-Host "   Contents:"
Get-ChildItem $TargetDir | Format-Table Name, Length -AutoSize
