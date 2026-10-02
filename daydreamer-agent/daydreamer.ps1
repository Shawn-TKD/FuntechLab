# 白日梦想家 Windows 启动脚本：选择素材或恢复任务，然后交给 Python 执行业务流程。
# 默认运行完整流程；传入 -Extract 时只提取生活事件卡。
# 示例：.\daydreamer.ps1 "C:\素材\生活视频.mp4"
#       .\daydreamer.ps1 -Extract "C:\素材\生活视频.mp4"
#       .\daydreamer.ps1 -Run "任务编号"
param(
    # 第一个位置参数。沿用 Events 名称，但既可以接收事件卡 JSON，也可以接收视频路径。
    # 未提供路径且未指定 -Run 时，弹出文件选择窗口。
    [Parameter(Position=0)][string]$Events,
    # 仅理解原视频并导出事件卡，不继续生成幻想故事和视频。
    [switch]$Extract,
    # 恢复已有任务；与 -Extract 组合时恢复提取任务，否则恢复全流程或生成任务。
    [string]$Run,
    [ValidateSet('native', 'manual', 'off')][string]$AudioMode,
    [string]$AudioManifest,
    # 显示当前所选 Python 子命令的帮助，不打开文件选择窗口。
    [switch]$Help
)
# 将 PowerShell 非终止错误提升为终止错误，以便由下方 catch 统一处理。
# Python 进程的退出码则通过 $LASTEXITCODE 单独传回调用方。
$ErrorActionPreference = 'Stop'
# 使用脚本所在目录定位项目环境，不依赖用户从哪个目录启动脚本。
$taskPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
$taskEntry = Join-Path $PSScriptRoot 'daydreamer.py'
try {
    # 不回退到系统 Python，避免使用错误的解释器或环境。
    if (-not (Test-Path -LiteralPath $taskPython)) { throw 'Project Python environment is missing.' }
    $taskCommand = if ($Extract) { 'extract' } else { 'run' }
    if ($Extract -and ($AudioMode -or $AudioManifest)) { throw 'Audio options apply to generation, not extraction.' }
    if ($Help) {
        # & 调用路径所指向的程序；-X utf8 启用 Python UTF-8 模式。
        # --project 明确指定项目根目录，--help 由 Python 命令行解析器处理。
        & $taskPython -X utf8 $taskEntry --project $PSScriptRoot $taskCommand --help
        exit $LASTEXITCODE
    }
    # 恢复任务使用原有输入，不能同时指定新素材；此检查也适用于视频路径。
    if ($Run -and $Events) { throw 'Use either an event card or -Run, not both.' }
    # 用参数数组保留每项参数的边界，避免包含空格的路径被拆开。
    $taskArguments = @('-X', 'utf8', $taskEntry, '--project', $PSScriptRoot, $taskCommand)
    if ($AudioMode) { $taskArguments += @('--audio-mode', $AudioMode) }
    if ($AudioManifest) { $taskArguments += @('--audio', (Resolve-Path -LiteralPath $AudioManifest).Path) }
    if ($Run) {
        # 只传任务编号；检查点读取、状态判断和恢复操作由 Python 负责。
        $taskArguments += @('--run', $Run)
    } else {
        if (-not $Events) {
            # 未指定素材路径时，加载 Windows Forms 并创建单文件选择窗口。
            Add-Type -AssemblyName System.Windows.Forms
            $taskDialog = New-Object System.Windows.Forms.OpenFileDialog
            try {
                $taskDialog.Title = 'Select a source video or a life event card'
                # Filter 以“显示名称|匹配模式”成对设置；默认可选视频或事件卡 JSON。
                $taskDialog.Filter = 'Video files|*.mp4;*.mov;*.mkv;*.avi;*.webm;*.m4v;*.wmv;*.flv|Life event cards (*.json)|*.json'
                if ($Extract) {
                    # 提取模式面向原视频，保留“所有文件”选项；素材有效性由下游检查。
                    $taskDialog.Title = 'Select a video to create a life event card'
                    $taskDialog.Filter = 'Video files|*.mp4;*.mov;*.mkv;*.avi;*.webm;*.m4v;*.wmv;*.flv|All files|*.*'
                }
                # 要求所选文件存在、禁用多选，并在关闭窗口后恢复原工作目录。
                $taskDialog.CheckFileExists = $true
                $taskDialog.Multiselect = $false
                $taskDialog.RestoreDirectory = $true
                # 默认从项目的上一级目录开始选择，便于访问工作区内的生活视频。
                $taskInitial = Join-Path $PSScriptRoot '..'
                if (Test-Path -LiteralPath $taskInitial) {
                    $taskDialog.InitialDirectory = (Resolve-Path -LiteralPath $taskInitial).Path
                }
                # 取消选择属于正常退出；此时尚未调用 Python，也没有创建生成任务。
                if ($taskDialog.ShowDialog() -ne [System.Windows.Forms.DialogResult]::OK) {
                    Write-Host 'Canceled. No generation task was created.'
                    exit 0
                }
                $Events = $taskDialog.FileName
            # 无论正常选择、取消还是发生异常，都释放文件选择窗口占用的资源。
            } finally { $taskDialog.Dispose() }
        }
        # 解析为完整路径；LiteralPath 按字面处理路径中的方括号等字符。
        $taskEventPath = (Resolve-Path -LiteralPath $Events).Path
        # -ine 是不区分大小写的不等比较：普通模式将 .json 交给 --events。
        # 提取模式或其他扩展名均交给 --video；这里仅做入口分流，不验证文件内容。
        $taskInputFlag = if ($Extract -or [IO.Path]::GetExtension($taskEventPath) -ine '.json') { '--video' } else { '--events' }
        $taskArguments += @($taskInputFlag, $taskEventPath)
    }
    # @taskArguments 将数组逐项展开为命令行参数，同步等待 Python 流程结束。
    & $taskPython @taskArguments
    # 原样返回 Python 的退出码，让外层启动器能区分完成、中断或失败。
    exit $LASTEXITCODE
} catch {
    # 启动环境、路径或文件窗口等 PowerShell 错误统一写入标准错误，并返回 1。
    [Console]::Error.WriteLine($_.Exception.Message)
    exit 1
}
