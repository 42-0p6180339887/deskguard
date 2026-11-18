param([string]$Config = (Join-Path $PSScriptRoot 'app-update.json'))

$script:UpdateApi = 'https://api.github.com/repos/42-0p6180339887/app-updates'
$script:UpdateHome = Join-Path ([Environment]::GetFolderPath('LocalApplicationData')) 'PrivateAppUpdates'
$script:UpdateAuth = Join-Path $script:UpdateHome 'authorization.dpapi'

function Test-PositiveInteger($Value) {
    return ($Value -is [int] -or $Value -is [long]) -and $Value -gt 0
}

function Read-AppUpdateConfig([string]$Path) {
    $app = Get-Content -LiteralPath $Path -Raw -Encoding UTF8 -ErrorAction Stop | ConvertFrom-Json -ErrorAction Stop
    if ($app.appId -cnotmatch '^[a-z][a-z0-9-]{0,63}$' -or
        -not (Test-PositiveInteger $app.versionCode) -or -not $app.versionName -or
        -not $app.title -or -not $app.installInstructions) { throw '更新配置不完整。' }
    if (-not $app.channel) { Add-Member -InputObject $app -NotePropertyName channel -NotePropertyValue stable -Force }
    if ($app.channel -cnotin @('stable','beta') -or ($app.clientId -and $app.clientId -cnotmatch '^[a-zA-Z0-9_]{8,64}$')) {
        throw '更新通道或 GitHub 应用标识无效。'
    }
    return $app
}

function Assert-AppUpdateManifest($Item, $App) {
    if (-not (Test-PositiveInteger $Item.schemaVersion) -or $Item.schemaVersion -ne 1 -or
        $Item.appId -cne $App.appId -or $Item.platform -cne 'windows-x64' -or
        $Item.channel -cne $App.channel -or $Item.available -isnot [bool]) { throw '更新清单与此应用不匹配。' }
    if (-not $Item.available) { return }
    if (-not (Test-PositiveInteger $Item.versionCode) -or
        -not (Test-PositiveInteger $Item.bytes) -or $Item.bytes -ge 2GB -or
        $Item.versionName -cnotmatch '^[a-zA-Z0-9][a-zA-Z0-9._-]{0,63}$' -or
        $Item.sha256 -cnotmatch '^[0-9a-f]{64}$' -or $Item.notes -isnot [string] -or
        $Item.notes.Length -gt 10000 -or
        $Item.releaseAssetName -cnotmatch '^[A-Za-z0-9][A-Za-z0-9._-]{0,150}\.(zip|exe|msi)$' -or
        $Item.releaseAssetName -match '^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])\.') { throw '更新包元数据无效。' }
    $tag = "$($App.appId)--windows-x64--$($App.channel)--v$($Item.versionName)--$($Item.versionCode)"
    if ($Item.releaseTag -cne $tag) { throw '发布标签与清单不匹配。' }
}

function Read-AppUpdateAuth {
    if (-not [IO.File]::Exists($script:UpdateAuth)) { return }
    try {
        $sealed = [IO.File]::ReadAllBytes($script:UpdateAuth)
        $bytes = [Security.Cryptography.ProtectedData]::Unprotect($sealed, $null, 'CurrentUser')
        return [Text.Encoding]::UTF8.GetString($bytes) | ConvertFrom-Json -ErrorAction Stop
    } catch { throw '无法读取本机的更新授权，请重新授权。' }
}

function Write-AppUpdateAuth($Record) {
    [void][IO.Directory]::CreateDirectory($script:UpdateHome)
    $bytes = [Text.Encoding]::UTF8.GetBytes(($Record | ConvertTo-Json -Compress))
    $sealed = [Security.Cryptography.ProtectedData]::Protect($bytes, $null, 'CurrentUser')
    $temporary = Join-Path $script:UpdateHome ([Guid]::NewGuid().ToString() + '.partial')
    try {
        [IO.File]::WriteAllBytes($temporary, $sealed)
        if ([IO.File]::Exists($script:UpdateAuth)) { [IO.File]::Replace($temporary, $script:UpdateAuth, $null) }
        else { [IO.File]::Move($temporary, $script:UpdateAuth) }
    } finally { [IO.File]::Delete($temporary) }
}

function Send-AppUpdateRequest([uri]$Uri, [string]$Token, [string]$Accept, $Form, $Cancellation) {
    if ($Uri.Scheme -cne 'https' -or -not $Uri.IsDefaultPort -or $Uri.UserInfo) { throw '更新连接地址无效。' }
    if ($Token -and $Uri.Host -cne 'api.github.com') { throw '授权请求地址无效。' }
    $method = [Net.Http.HttpMethod]::Get
    if ($Form) {
        if ($Uri.Host -cne 'github.com') { throw '登录地址无效。' }
        $method = [Net.Http.HttpMethod]::Post
    }
    $request = [Net.Http.HttpRequestMessage]::new($method, $Uri)
    try {
        $request.Headers.UserAgent.ParseAdd('PrivateAppUpdates/1.0')
        $request.Headers.Accept.ParseAdd($Accept)
        if ($Token) { $request.Headers.Authorization = [Net.Http.Headers.AuthenticationHeaderValue]::new('Bearer', $Token) }
        if ($Form) {
            $encoded = ($Form.GetEnumerator() | ForEach-Object {
                [uri]::EscapeDataString([string]$_.Key) + '=' + [uri]::EscapeDataString([string]$_.Value)
            }) -join '&'
            $request.Content = [Net.Http.StringContent]::new($encoded,[Text.Encoding]::UTF8,'application/x-www-form-urlencoded')
        }
        return $script:UpdateHttp.SendAsync($request,[Net.Http.HttpCompletionOption]::ResponseHeadersRead,$Cancellation).GetAwaiter().GetResult()
    } catch { throw '无法连接 GitHub，请检查网络后重试。' }
    finally { $request.Dispose() }
}

function Assert-AppUpdateResponse($Response) {
    $status = [int]$Response.StatusCode
    if ($status -eq 401) { throw 'GitHub 授权已失效，请重新授权。' }
    if ($status -eq 429 -or ($status -eq 403 -and $Response.Headers.Contains('X-RateLimit-Remaining') -and
        ($Response.Headers.GetValues('X-RateLimit-Remaining') -contains '0'))) { throw 'GitHub 请求过多，请稍后再查。' }
    if ($status -in @(403,404)) { throw '无法读取更新，请核对 GitHub 仓库权限。' }
    if ($status -ne 200) { throw "GitHub 返回 HTTP $status，请稍后重试。" }
}

function Copy-AppUpdateStream($Response, $Target, [long]$Limit, $Cancellation) {
    $stream = $Response.Content.ReadAsStreamAsync().GetAwaiter().GetResult()
    try {
        $buffer = New-Object byte[] 65536
        while (($length = $stream.ReadAsync($buffer,0,$buffer.Length,$Cancellation).GetAwaiter().GetResult()) -gt 0) {
            if ($Target.Length + $length -gt $Limit) { throw '文件长度超过预期。' }
            $Target.Write($buffer,0,$length)
        }
    } finally { $stream.Dispose() }
}

function Read-AppUpdateJson([string]$Url, [string]$Token = '', [string]$Accept = 'application/vnd.github+json', $Form = $null) {
    $cancel = [Threading.CancellationTokenSource]::new(20000)
    $buffer = [IO.MemoryStream]::new()
    $response = $null
    try {
        $response = Send-AppUpdateRequest $Url $Token $Accept $Form $cancel.Token
        Assert-AppUpdateResponse $response
        Copy-AppUpdateStream $response $buffer 1MB $cancel.Token
        try { return [Text.Encoding]::UTF8.GetString($buffer.ToArray()) | ConvertFrom-Json -ErrorAction Stop }
        catch { throw 'GitHub 返回的更新信息无法读取。' }
    } finally {
        if ($response) { $response.Dispose() }
        $buffer.Dispose()
        $cancel.Dispose()
    }
}

function Get-AppUpdateManifest($App, [string]$Token) {
    $path = "channels/$($App.appId)/windows-x64/$($App.channel).json"
    $item = Read-AppUpdateJson ("$script:UpdateApi/contents/" + $path + '?ref=main') $Token 'application/vnd.github.raw+json'
    Assert-AppUpdateManifest $item $App
    return $item
}

function New-AppDeviceAuthorization($Reply, [string]$ClientId) {
    if ($Reply.access_token -cnotmatch '^ghu_[a-zA-Z0-9_]+$') { throw '未获 GitHub 授权，请重新登录。' }
    $expiry = 0
    if ($Reply.expires_in -gt 0) { $expiry = [DateTimeOffset]::UtcNow.AddSeconds($Reply.expires_in).ToUnixTimeSeconds() }
    return @{ access=$Reply.access_token; refresh=$Reply.refresh_token; expires=$expiry; clientId=$ClientId }
}

function Get-AppUpdateToken($App) {
    $record = Read-AppUpdateAuth
    if (-not $record.access) { throw '请先完成 GitHub 更新授权。' }
    if ($record.expires -and $record.expires -le [DateTimeOffset]::UtcNow.ToUnixTimeSeconds() + 60) {
        if (-not $App.clientId -or $record.clientId -cne $App.clientId -or -not $record.refresh) {
            throw 'GitHub 授权已过期，请重新授权。'
        }
        $reply = Read-AppUpdateJson 'https://github.com/login/oauth/access_token' '' 'application/json' @{
            client_id=$App.clientId; grant_type='refresh_token'; refresh_token=$record.refresh
        }
        $record = New-AppDeviceAuthorization $reply $App.clientId
        Write-AppUpdateAuth $record
    }
    return $record.access
}

function Test-AppUpdatePackage([string]$Path, $Item) {
    return [IO.File]::Exists($Path) -and (Get-Item -LiteralPath $Path).Length -eq $Item.bytes -and
        (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash -ieq $Item.sha256
}

function Save-AppUpdatePackage($App, $Item, [string]$Token) {
    Assert-AppUpdateManifest $Item $App
    if (-not $Item.available -or $Item.versionCode -le $App.versionCode) { throw '此版本无需安装。' }
    $release = Read-AppUpdateJson "$script:UpdateApi/releases/tags/$($Item.releaseTag)" $Token
    if ($release.tag_name -cne $Item.releaseTag -or $release.draft -ne $false -or
        ($App.channel -ceq 'stable' -and $release.prerelease -ne $false)) { throw '此发布尚不可下载。' }
    $assets = @($release.assets | Where-Object { $_.name -ceq $Item.releaseAssetName })
    if ($assets.Count -ne 1 -or -not (Test-PositiveInteger $assets[0].id) -or $assets[0].size -ne $Item.bytes) {
        throw '发布附件与更新清单不符。'
    }
    $folder = Join-Path $script:UpdateHome "downloads\$($App.appId)\$($Item.releaseTag)"
    [void][IO.Directory]::CreateDirectory($folder)
    $output = Join-Path $folder $Item.releaseAssetName
    if (Test-AppUpdatePackage $output $Item) { return $output }
    $temporary = Join-Path $folder ([Guid]::NewGuid().ToString() + '.partial')
    $cancel = [Threading.CancellationTokenSource]::new(300000)
    $response = $null
    try {
        $uri = [uri]"$script:UpdateApi/releases/assets/$($assets[0].id)"
        $authorization = $Token
        for ($hop = 0; $hop -lt 5; $hop++) {
            $response = Send-AppUpdateRequest $uri $authorization 'application/octet-stream' $null $cancel.Token
            if ([int]$response.StatusCode -notin @(301,302,303,307,308)) { break }
            $location = $response.Headers.Location
            $response.Dispose()
            $response = $null
            if (-not $location) { throw 'GitHub 未返回下载地址。' }
            $uri = [uri]::new($uri,$location)
            if ($uri.Host -cnotmatch '^[a-z0-9-]+\.githubusercontent\.com$') { throw 'GitHub 返回了未知的附件存储地址。' }
            $authorization = ''
        }
        if (-not $response) { throw '下载跳转次数过多。' }
        Assert-AppUpdateResponse $response
        $file = [IO.File]::Open($temporary,'CreateNew','Write','None')
        try { Copy-AppUpdateStream $response $file $Item.bytes $cancel.Token }
        finally { $file.Dispose() }
        if (-not (Test-AppUpdatePackage $temporary $Item)) { throw '安装包校验失败。' }
        if ([IO.File]::Exists($output)) { [IO.File]::Replace($temporary,$output,$null) }
        else { [IO.File]::Move($temporary,$output) }
        return $output
    } finally {
        if ($response) { $response.Dispose() }
        $cancel.Dispose()
        [IO.File]::Delete($temporary)
    }
}

function Show-AppUpdateWindow($App) {
    Add-Type -AssemblyName System.Windows.Forms
    Add-Type -AssemblyName System.Drawing
    [Windows.Forms.Application]::EnableVisualStyles()
    $form = [Windows.Forms.Form]::new()
    $form.Text = "$($App.title) · 应用更新"
    $form.StartPosition = 'CenterScreen'
    $form.ClientSize = [Drawing.Size]::new(640,310)
    $form.FormBorderStyle = 'FixedDialog'
    $form.MaximizeBox = $false
    $form.Font = [Drawing.Font]::new('Microsoft YaHei UI',10)
    $version = [Windows.Forms.Label]::new()
    $version.SetBounds(16,14,605,26)
    $version.Text = "已安装 $($App.versionName)    $($App.channel)"
    $status = [Windows.Forms.Label]::new()
    $status.SetBounds(16,44,605,45)
    $status.Text = '从私有仓库检查可用更新。'
    $notes = [Windows.Forms.TextBox]::new()
    $notes.SetBounds(16,92,605,166)
    $notes.Multiline = $true
    $notes.ReadOnly = $true
    $notes.ScrollBars = 'Vertical'
    $notes.Text = $App.installInstructions
    $bar = [Windows.Forms.FlowLayoutPanel]::new()
    $bar.SetBounds(16,264,605,40)
    $buttons = @{}
    foreach ($label in @('检查更新','下载更新','GitHub 登录','设置只读令牌','清除此机授权')) {
        $button = [Windows.Forms.Button]::new()
        $button.AutoSize = $true
        $button.Text = $label
        $buttons[$label] = $button
        $bar.Controls.Add($button)
    }
    $form.Controls.AddRange(@($version,$status,$notes,$bar))
    $check = $buttons['检查更新']
    $download = $buttons['下载更新']
    $login = $buttons['GitHub 登录']
    $download.Enabled = $false
    $login.Visible = [bool]$App.clientId
    $state = @{ candidate=$null; device=$null }
    $authPath = $script:UpdateAuth
    $poll = [Windows.Forms.Timer]::new()
    $checkAction = {
        $state.candidate = $null
        $download.Enabled = $false
        $status.Text = '正在检查更新…'
        $status.Refresh()
        try {
            $item = Get-AppUpdateManifest $App (Get-AppUpdateToken $App)
            if ($item.available -and $item.versionCode -gt $App.versionCode) {
                $state.candidate = $item
                $notes.Text = ($item.notes -replace '\r?\n',[Environment]::NewLine) + [Environment]::NewLine + [Environment]::NewLine + $App.installInstructions
                $status.Text = "发现 $($item.versionName)"
                $download.Enabled = $true
            } else {
                $status.Text = '此通道没有更新的版本。'
                $notes.Text = $App.installInstructions
            }
        } catch { $status.Text = $_.Exception.Message }
    }.GetNewClosure()
    $check.Add_Click($checkAction)
    $download.Add_Click({
        $status.Text = '正在下载并校验安装包。请等待…'
        $status.Refresh()
        try {
            $path = Save-AppUpdatePackage $App $state.candidate (Get-AppUpdateToken $App)
            $status.Text = '校验通过。请按下方说明安装，保留原有数据。'
            Invoke-Item -LiteralPath ([IO.Path]::GetDirectoryName($path)) -ErrorAction Stop
        } catch { $status.Text = $_.Exception.Message }
    }.GetNewClosure())
    $buttons['设置只读令牌'].Add_Click({
        $prompt = [Windows.Forms.Form]::new()
        $prompt.Text = 'GitHub 更新授权'
        $prompt.StartPosition = 'CenterParent'
        $prompt.ClientSize = [Drawing.Size]::new(545,145)
        $hint = [Windows.Forms.Label]::new()
        $hint.SetBounds(12,12,520,42)
        $hint.Text = '输入你自己的细粒度 GitHub 令牌。仅给 app-updates 仓库 Contents: read 权限。'
        $field = [Windows.Forms.TextBox]::new()
        $field.SetBounds(12,60,520,25)
        $field.UseSystemPasswordChar = $true
        $accept = [Windows.Forms.Button]::new()
        $accept.SetBounds(425,99,107,29)
        $accept.Text = '验证并保存'
        $accept.DialogResult = 'OK'
        $prompt.Controls.AddRange(@($hint,$field,$accept))
        $prompt.AcceptButton = $accept
        try {
            if ($prompt.ShowDialog($form) -ne 'OK') { return }
            $token = $field.Text.Trim()
            if ($token -cnotmatch '^github_pat_[A-Za-z0-9_]+$') { throw '请输入细粒度 GitHub 令牌。' }
            $null = Get-AppUpdateManifest $App $token
            $poll.Stop()
            $state.device = $null
            $login.Enabled = $true
            Write-AppUpdateAuth @{access=$token}
            & $checkAction
        } catch { $status.Text = $_.Exception.Message }
        finally { $field.Clear(); $prompt.Dispose() }
    }.GetNewClosure())
    $buttons['清除此机授权'].Add_Click({
        $poll.Stop()
        $state.device = $null
        $login.Enabled = $true
        try {
            [IO.File]::Delete($authPath)
            $download.Enabled = $false
            $state.candidate = $null
            $status.Text = '本机保存的更新授权已清除。'
        } catch { $status.Text = '无法清除本机授权。' }
    }.GetNewClosure())
    $login.Add_Click({
        try {
            $device = Read-AppUpdateJson 'https://github.com/login/device/code' '' 'application/json' @{client_id=$App.clientId}
            if (-not $device.device_code -or $device.user_code -cnotmatch '^[A-Z0-9-]{4,20}$' -or $device.expires_in -le 0) {
                throw '无法获取 GitHub 登录码。'
            }
            $state.device = @{code=$device.device_code; expires=[DateTimeOffset]::UtcNow.AddSeconds($device.expires_in)}
            $poll.Interval = [int]([Math]::Max(5,$device.interval) * 1000)
            $status.Text = "在 GitHub 页面输入设备码 $($device.user_code)。"
            $login.Enabled = $false
            Start-Process 'https://github.com/login/device' -ErrorAction Stop
            $poll.Start()
        } catch { $status.Text = $_.Exception.Message; $login.Enabled = $true }
    }.GetNewClosure())
    $poll.Add_Tick({
        $poll.Stop()
        try {
            if ([DateTimeOffset]::UtcNow -ge $state.device.expires) { throw '登录码已过期。' }
            $reply = Read-AppUpdateJson 'https://github.com/login/oauth/access_token' '' 'application/json' @{
                client_id=$App.clientId
                device_code=$state.device.code
                grant_type='urn:ietf:params:oauth:grant-type:device_code'
            }
            if ($reply.error -eq 'authorization_pending') { $poll.Start(); return }
            if ($reply.error -eq 'slow_down') { $poll.Interval += 5000; $poll.Start(); return }
            $record = New-AppDeviceAuthorization $reply $App.clientId
            $null = Get-AppUpdateManifest $App $record.access
            Write-AppUpdateAuth $record
            $login.Enabled = $true
            $state.device = $null
            & $checkAction
        } catch { $status.Text = $_.Exception.Message; $login.Enabled = $true; $state.device = $null }
    }.GetNewClosure())
    $form.Add_Shown({ if ([IO.File]::Exists($authPath)) { & $checkAction } }.GetNewClosure())
    try { [void]$form.ShowDialog() }
    finally { $poll.Dispose(); $form.Dispose() }
}

if ($MyInvocation.InvocationName -ne '.') {
    $ErrorActionPreference = 'Stop'
    $mutex = $null
    $ownsMutex = $false
    try {
        Add-Type -AssemblyName System.Security
        Add-Type -AssemblyName System.Net.Http
        $app = Read-AppUpdateConfig $Config
        $mutex = [Threading.Mutex]::new($false,'Local\PrivateAppUpdates-v1')
        try { $ownsMutex = $mutex.WaitOne(0) }
        catch [Threading.AbandonedMutexException] { $ownsMutex = $true }
        if (-not $ownsMutex) { throw '已有应用更新窗口打开，请先关闭它。' }
        $handler = [Net.Http.HttpClientHandler]::new()
        $handler.AllowAutoRedirect = $false
        $script:UpdateHttp = [Net.Http.HttpClient]::new($handler)
        try { Show-AppUpdateWindow $app } finally { $script:UpdateHttp.Dispose() }
    } catch {
        Add-Type -AssemblyName System.Windows.Forms
        [void][Windows.Forms.MessageBox]::Show($_.Exception.Message,'应用更新')
        exit 1
    } finally {
        if ($ownsMutex) { $mutex.ReleaseMutex() }
        if ($mutex) { $mutex.Dispose() }
    }
}
