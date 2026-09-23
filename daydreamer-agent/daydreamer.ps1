param(
    [Parameter(Position=0)][string]$Events,
    [switch]$Extract,
    [string]$Run,
    [switch]$Help
)
$ErrorActionPreference = 'Stop'
$taskPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
$taskEntry = Join-Path $PSScriptRoot 'daydreamer.py'
try {
    if (-not (Test-Path -LiteralPath $taskPython)) { throw 'Project Python environment is missing.' }
    $taskCommand = if ($Extract) { 'extract' } else { 'run' }
    if ($Help) {
        & $taskPython -X utf8 $taskEntry --project $PSScriptRoot $taskCommand --help
        exit $LASTEXITCODE
    }
    if ($Run -and $Events) { throw 'Use either an event card or -Run, not both.' }
    $taskArguments = @('-X', 'utf8', $taskEntry, '--project', $PSScriptRoot, $taskCommand)
    if ($Run) {
        $taskArguments += @('--run', $Run)
    } else {
        if (-not $Events) {
            Add-Type -AssemblyName System.Windows.Forms
            $taskDialog = New-Object System.Windows.Forms.OpenFileDialog
            try {
                $taskDialog.Title = 'Select a source video or a life event card'
                $taskDialog.Filter = 'Video files|*.mp4;*.mov;*.mkv;*.avi;*.webm;*.m4v;*.wmv;*.flv|Life event cards (*.json)|*.json'
                if ($Extract) {
                    $taskDialog.Title = 'Select a video to create a life event card'
                    $taskDialog.Filter = 'Video files|*.mp4;*.mov;*.mkv;*.avi;*.webm;*.m4v;*.wmv;*.flv|All files|*.*'
                }
                $taskDialog.CheckFileExists = $true
                $taskDialog.Multiselect = $false
                $taskDialog.RestoreDirectory = $true
                $taskInitial = Join-Path $PSScriptRoot '..'
                if (Test-Path -LiteralPath $taskInitial) {
                    $taskDialog.InitialDirectory = (Resolve-Path -LiteralPath $taskInitial).Path
                }
                if ($taskDialog.ShowDialog() -ne [System.Windows.Forms.DialogResult]::OK) {
                    Write-Host 'Canceled. No generation task was created.'
                    exit 0
                }
                $Events = $taskDialog.FileName
            } finally { $taskDialog.Dispose() }
        }
        $taskEventPath = (Resolve-Path -LiteralPath $Events).Path
        $taskInputFlag = if ($Extract -or [IO.Path]::GetExtension($taskEventPath) -ine '.json') { '--video' } else { '--events' }
        $taskArguments += @($taskInputFlag, $taskEventPath)
    }
    & $taskPython @taskArguments
    exit $LASTEXITCODE
} catch {
    [Console]::Error.WriteLine($_.Exception.Message)
    exit 1
}
