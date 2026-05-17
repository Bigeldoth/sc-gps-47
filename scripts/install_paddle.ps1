# Installs PaddleOCR (standard recognizer) into a dedicated venv at
# <repo_root>/.venv-paddle so the host app can run on Python 3.13/3.14
# while PaddleOCR keeps using its required CPython 3.12.
#
# The Python sidecar worker (scripts/paddle_worker.py) is launched from
# this venv by src/paddle_service.py — see those files for the IPC details.
#
# Usage:
#   pwsh scripts/install_paddle.ps1 [-Python <path-to-python.exe>]
#                                   [-Device cpu|gpu|blackwell]
#                                   [-CudaLabel cu118|cu123|cu126|cu129]
#
# When -Device is gpu and -CudaLabel is empty, the script defaults to cu118
# (widest compatibility). Pick blackwell for RTX 50-series GPUs.

param(
    [string]$Python = "",
    [ValidateSet("cpu", "gpu", "blackwell")]
    [string]$Device = "cpu",
    [string]$CudaLabel = ""
)

$ErrorActionPreference = "Stop"

$RepoRoot = Split-Path -Parent $PSScriptRoot
$VenvDir = Join-Path $RepoRoot ".venv-paddle"

Write-Host "==> Installing PaddleOCR sidecar into $VenvDir"
Write-Host "==> Device: $Device"

# Resolve a Python 3.12 interpreter. The `py` launcher is the most reliable
# source on Windows. PaddlePaddle wheels stop at cp312.
if (-not $Python) {
    if (Get-Command py -ErrorAction SilentlyContinue) {
        $Python = (py -3.12 -c "import sys; print(sys.executable)").Trim()
        if (-not $Python) {
            Write-Error "py launcher could not resolve Python 3.12. Pass -Python <path> explicitly."
        }
    } else {
        Write-Error "No 'py' launcher found. Install Python 3.12 from https://www.python.org/downloads/ or pass -Python <path>."
    }
}
Write-Host "==> Using Python: $Python"

# Create the venv if it doesn't already exist.
$VenvPython = Join-Path $VenvDir "Scripts/python.exe"
if (-not (Test-Path $VenvPython)) {
    Write-Host "==> Creating venv"
    & $Python -m venv $VenvDir
    if ($LASTEXITCODE -ne 0) { Write-Error "venv creation failed" }
}

Write-Host "==> Upgrading pip"
& $VenvPython -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) { Write-Error "pip upgrade failed" }

switch ($Device) {
    "cpu" {
        Write-Host "==> Installing paddlepaddle + paddleocr (CPU)"
        & $VenvPython -m pip install --upgrade paddlepaddle paddleocr
        if ($LASTEXITCODE -ne 0) { Write-Error "paddleocr CPU install failed" }
    }
    "gpu" {
        if (-not $CudaLabel) { $CudaLabel = "cu118" }
        $IndexUrl = "https://www.paddlepaddle.org.cn/packages/stable/$CudaLabel/"
        Write-Host "==> Installing paddlepaddle-gpu from $IndexUrl"
        & $VenvPython -m pip install --upgrade paddlepaddle-gpu -i $IndexUrl
        if ($LASTEXITCODE -ne 0) { Write-Error "paddlepaddle-gpu install failed" }
        Write-Host "==> Installing paddleocr (PyPI)"
        & $VenvPython -m pip install --upgrade paddleocr
        if ($LASTEXITCODE -ne 0) { Write-Error "paddleocr install failed" }
    }
    "blackwell" {
        $IndexUrl = "https://www.paddlepaddle.org.cn/packages/stable/cu129/"
        Write-Host "==> Installing paddlepaddle-gpu==3.2.1 from cu129 index (Blackwell sm_120)"
        & $VenvPython -m pip uninstall -y paddlepaddle paddlepaddle-gpu 2>$null
        & $VenvPython -m pip install paddlepaddle-gpu==3.2.1 -i $IndexUrl
        if ($LASTEXITCODE -ne 0) { Write-Error "paddlepaddle-gpu (blackwell) install failed" }
        Write-Host "==> Installing paddleocr (PyPI)"
        & $VenvPython -m pip install --upgrade paddleocr
        if ($LASTEXITCODE -ne 0) { Write-Error "paddleocr install failed" }

        # Drop the sentinel file so detect_gpu_paddle_status() can recognize
        # this install as Blackwell-capable without re-running an inference probe.
        $PaddleDir = & $VenvPython -c "import paddle, pathlib; print(pathlib.Path(paddle.__file__).resolve().parent)"
        if ($PaddleDir) {
            $Sentinel = Join-Path $PaddleDir.Trim() ".spacedrive-blackwell-cu129"
            "Installed via install_paddle.ps1 -Device blackwell" | Out-File -FilePath $Sentinel -Encoding utf8
            Write-Host "==> Wrote Blackwell sentinel: $Sentinel"
        }
    }
}

Write-Host "==> Done. Verify with:"
Write-Host "    $VenvPython -c `"import paddleocr; print(paddleocr.__version__)`""
