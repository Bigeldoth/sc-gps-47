# Installs PaddleOCR-VL into a dedicated venv at <repo_root>/.venv-paddle-vl
#
# Why a separate venv:
# Per the official Blackwell guide
# (https://www.paddleocr.ai/main/en/version3.x/pipeline_usage/PaddleOCR-VL-NVIDIA-Blackwell.html#workflow-guide-for-this-hardware),
# the transformers / vLLM / SGLang backends used by genai_server pin
# `transformers` versions that conflict with the standard paddleocr install.
# Keeping them in an isolated environment avoids breaking the main OCR path.
#
# Usage:
#   pwsh scripts/install_paddle_vl.ps1 [-Python <path-to-python.exe>] [-Backend transformers|vllm|sglang]

param(
    [string]$Python = "",
    [string]$Backend = "transformers"
)

$ErrorActionPreference = "Stop"

$RepoRoot = Split-Path -Parent $PSScriptRoot
$VenvDir = Join-Path $RepoRoot ".venv-paddle-vl"

Write-Host "==> Installing PaddleOCR-VL into $VenvDir"

# Resolve a Python 3.12 interpreter. Reusing the standard `py` launcher first
# (set up by the python.org installer); fallback to whatever python.exe the
# user passed via -Python.
if (-not $Python) {
    if (Get-Command py -ErrorAction SilentlyContinue) {
        $Python = (py -3.12 -c "import sys; print(sys.executable)").Trim()
        if (-not $Python) {
            Write-Error "py launcher could not resolve Python 3.12. Pass -Python <path> explicitly."
        }
    } else {
        Write-Error "No 'py' launcher found. Pass -Python <path-to-python.exe> explicitly."
    }
}
Write-Host "==> Using Python: $Python"

# Create the venv if it doesn't already exist.
if (-not (Test-Path (Join-Path $VenvDir "Scripts/python.exe"))) {
    Write-Host "==> Creating venv"
    & $Python -m venv $VenvDir
    if ($LASTEXITCODE -ne 0) { Write-Error "venv creation failed" }
}

$VenvPython = Join-Path $VenvDir "Scripts/python.exe"
$VenvPaddleOCR = Join-Path $VenvDir "Scripts/paddleocr.exe"

Write-Host "==> Upgrading pip"
& $VenvPython -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) { Write-Error "pip upgrade failed" }

Write-Host "==> Installing paddleocr[doc-parser]"
& $VenvPython -m pip install -U "paddleocr[doc-parser]"
if ($LASTEXITCODE -ne 0) { Write-Error "paddleocr[doc-parser] install failed" }

Write-Host "==> Installing genai_server deps for backend: $Backend"
& $VenvPaddleOCR install_genai_server_deps $Backend
if ($LASTEXITCODE -ne 0) {
    Write-Warning "install_genai_server_deps $Backend failed — you may need to retry or pick a different backend"
}

Write-Host "==> Done. Verify with:"
Write-Host "    $VenvPaddleOCR genai_server --model_name PaddleOCR-VL-1.5-0.9B --backend $Backend --port 8118"
