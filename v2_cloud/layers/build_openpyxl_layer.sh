#!/bin/bash
# Build the openpyxl Lambda Layer
# Run this ONCE before sam build/deploy
# Creates the correct directory structure for a Python Lambda Layer

set -e

LAYER_DIR="$(dirname "$0")/openpyxl"
TARGET_DIR="${LAYER_DIR}/python"

echo "Building openpyxl Lambda Layer..."

# Clean previous build
rm -rf "${TARGET_DIR}"
mkdir -p "${TARGET_DIR}"

# Install openpyxl into the layer structure
pip install openpyxl==3.1.5 -t "${TARGET_DIR}" --no-cache-dir --quiet

# Remove unnecessary files to keep layer small
find "${TARGET_DIR}" -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
find "${TARGET_DIR}" -type d -name "*.dist-info" -exec rm -rf {} + 2>/dev/null || true
find "${TARGET_DIR}" -name "*.pyc" -delete 2>/dev/null || true

echo "✅ Layer built at: ${TARGET_DIR}"
echo "   Contents:"
ls -la "${TARGET_DIR}"
echo ""
echo "   Size: $(du -sh "${TARGET_DIR}" | cut -f1)"
