[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"

function Test-Python312 {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Command,
        [string[]]$PrefixArguments = @()
    )

    $probe = "import platform,sys; print(platform.python_implementation()); print('%d.%d.%d' % sys.version_info[:3]); raise SystemExit(0 if platform.python_implementation() == 'CPython' and sys.version_info[:2] == (3, 12) else 1)"
    $output = & $Command @PrefixArguments -c $probe 2>$null
    if ($LASTEXITCODE -ne 0) {
        return $null
    }
    return @($output)
}

try {
    $projectRoot = $PSScriptRoot
    $requirements = Join-Path $projectRoot "requirements.txt"
    $venvDirectory = Join-Path $projectRoot ".venv"
    $venvPython = Join-Path $venvDirectory "Scripts\python.exe"

    if (-not (Test-Path -LiteralPath $requirements -PathType Leaf)) {
        throw "Required dependency manifest was not found: $requirements"
    }

    if (Test-Path -LiteralPath $venvPython -PathType Leaf) {
        $venvVersion = Test-Python312 -Command $venvPython
        if ($null -eq $venvVersion) {
            throw "The existing .venv is not CPython 3.12.x. Remove or rename '$venvDirectory', then rerun bootstrap.ps1."
        }
        Write-Host "Using existing .venv ($($venvVersion[-1]))."
    }
    else {
        $pythonCommand = $null
        $pythonArguments = @()

        $pyLauncher = Get-Command "py" -ErrorAction SilentlyContinue
        if ($null -ne $pyLauncher) {
            $pyVersion = Test-Python312 -Command $pyLauncher.Source -PrefixArguments @("-3.12")
            if ($null -ne $pyVersion) {
                $pythonCommand = $pyLauncher.Source
                $pythonArguments = @("-3.12")
                Write-Host "Found CPython $($pyVersion[-1]) through 'py -3.12'."
            }
        }

        if ($null -eq $pythonCommand) {
            $python = Get-Command "python" -ErrorAction SilentlyContinue
            if ($null -ne $python) {
                $pythonVersion = Test-Python312 -Command $python.Source
                if ($null -ne $pythonVersion) {
                    $pythonCommand = $python.Source
                    Write-Host "Found CPython $($pythonVersion[-1]) through 'python'."
                }
            }
        }

        if ($null -eq $pythonCommand) {
            throw "CPython 3.12.x was not found. Install it and make 'py -3.12' or 'python' available, then rerun bootstrap.ps1."
        }

        Write-Host "Creating .venv..."
        & $pythonCommand @pythonArguments -m venv $venvDirectory
        if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $venvPython -PathType Leaf)) {
            throw "Python failed to create '$venvDirectory'."
        }
    }

    Write-Host "Installing pinned Python dependencies..."
    & $venvPython -m pip install -r $requirements
    if ($LASTEXITCODE -ne 0) {
        throw "pip failed to install requirements.txt (exit code $LASTEXITCODE). Review the pip error above and retry when package network access is available."
    }

    $verify = "import platform,sys,yaml,pytest,jsonschema; from importlib.metadata import version; assert platform.python_implementation() == 'CPython' and sys.version_info[:2] == (3, 12); print('Python=' + platform.python_version()); print('PyYAML=' + yaml.__version__); print('pytest=' + pytest.__version__); print('jsonschema=' + version('jsonschema'))"
    & $venvPython -c $verify
    if ($LASTEXITCODE -ne 0) {
        throw "The virtual environment failed Python 3.12/PyYAML/pytest/jsonschema verification."
    }

    Write-Host "Bootstrap complete. Start CL_AO with 启动CLAO.bat."
    exit 0
}
catch {
    Write-Error ("Bootstrap failed: " + $_.Exception.Message)
    exit 1
}
