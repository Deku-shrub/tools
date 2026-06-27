# Switch-ClaudeMode.ps1
# Toggles Claude Code between subscription (Pro/Max) and API key mode
# Usage: .\Switch-ClaudeMode.ps1 [-Mode sub|api|status|toggle] [-ApiKey "sk-ant-..."]

param(
    [ValidateSet("sub", "api", "status", "toggle")]
    [string]$Mode = "toggle",
    [string]$ApiKey = ""
)

$SettingsPath = "$env:USERPROFILE\.claude\settings.json"
$KeyStorePath  = "$env:USERPROFILE\.claude\api_key.txt"

# ── Helpers ────────────────────────────────────────────────────────────────────

function GetCurrentMode {
    if (Test-Path $SettingsPath) {
        $raw = Get-Content $SettingsPath -Raw -ErrorAction SilentlyContinue
        $settings = $raw | ConvertFrom-Json -ErrorAction SilentlyContinue
        if ($settings -and $settings.env -and $settings.env.ANTHROPIC_API_KEY) {
            return "api"
        }
    }
    return "sub"
}

function GetStoredApiKey {
    if (Test-Path $KeyStorePath) {
        return (Get-Content $KeyStorePath -Raw).Trim()
    }
    return $null
}

function SaveApiKey($key) {
    New-Item -ItemType Directory -Force -Path (Split-Path $KeyStorePath) | Out-Null
    $key | Set-Content $KeyStorePath
    Write-Host "  API key saved to $KeyStorePath" -ForegroundColor DarkGray
}

function LoadSettings {
    if (Test-Path $SettingsPath) {
        $raw = Get-Content $SettingsPath -Raw
        $parsed = $raw | ConvertFrom-Json -ErrorAction SilentlyContinue
        if ($parsed) { return $parsed }
    }
    return [PSCustomObject]@{}
}

function SaveSettings($settings) {
    New-Item -ItemType Directory -Force -Path (Split-Path $SettingsPath) | Out-Null
    $settings | ConvertTo-Json -Depth 10 | Set-Content $SettingsPath
}

# ── Mode switchers ─────────────────────────────────────────────────────────────

function SwitchToSubscription {
    $settings = LoadSettings

    if ($settings.PSObject.Properties["env"] -and $settings.env.PSObject.Properties["ANTHROPIC_API_KEY"]) {
        $settings.env.PSObject.Properties.Remove("ANTHROPIC_API_KEY")
        $remaining = ($settings.env.PSObject.Properties | Measure-Object).Count
        if ($remaining -eq 0) {
            $settings.PSObject.Properties.Remove("env")
        }
    }

    SaveSettings $settings

    Write-Host ""
    Write-Host "  > Switched to SUBSCRIPTION mode" -ForegroundColor Green
    Write-Host "  Claude Code will use your Pro/Max plan." -ForegroundColor DarkGray
    Write-Host "  Run /logout then /login inside Claude Code if previously in API mode." -ForegroundColor Yellow
    Write-Host ""
}

function SwitchToApi($key) {
    if (-not $key) { $key = GetStoredApiKey }
    if (-not $key) { $key = $env:ANTHROPIC_API_KEY }
    if (-not $key) {
        Write-Host ""
        $key = Read-Host "  Enter your Anthropic API key (sk-ant-...)"
        if (-not $key.StartsWith("sk-ant-")) {
            Write-Host "  X That doesn't look like a valid Anthropic API key." -ForegroundColor Red
            exit 1
        }
        $save = Read-Host "  Save key for future switches? (y/n)"
        if ($save -eq "y") { SaveApiKey $key }
    }

    $settings = LoadSettings

    if (-not $settings.PSObject.Properties["env"]) {
        $settings | Add-Member -MemberType NoteProperty -Name "env" -Value ([PSCustomObject]@{})
    }

    if ($settings.env.PSObject.Properties["ANTHROPIC_API_KEY"]) {
        $settings.env.ANTHROPIC_API_KEY = $key
    }
    else {
        $settings.env | Add-Member -MemberType NoteProperty -Name "ANTHROPIC_API_KEY" -Value $key
    }

    SaveSettings $settings

    Write-Host ""
    Write-Host "  > Switched to API KEY mode" -ForegroundColor Green
    Write-Host "  Claude Code will bill to your Anthropic Console account." -ForegroundColor DarkGray
    Write-Host "  Restart Claude Code or open a new session for this to take effect." -ForegroundColor Yellow
    Write-Host ""
}

function ShowStatus {
    $current = GetCurrentMode
    $storedKey = GetStoredApiKey

    Write-Host ""
    Write-Host "  +-- Claude Code Mode --------------------------------+" -ForegroundColor DarkGray

    if ($current -eq "api") {
        Write-Host "  |  Current mode : " -NoNewline -ForegroundColor DarkGray
        Write-Host "API KEY  [key]" -ForegroundColor Yellow
        Write-Host "  |  Billing      : Anthropic Console (pay-per-token)" -ForegroundColor DarkGray
    }
    else {
        Write-Host "  |  Current mode : " -NoNewline -ForegroundColor DarkGray
        Write-Host "SUBSCRIPTION  [plan]" -ForegroundColor Cyan
        Write-Host "  |  Billing      : Pro/Max plan allowance" -ForegroundColor DarkGray
    }

    if ($storedKey) {
        $masked = $storedKey.Substring(0, [Math]::Min(12, $storedKey.Length)) + "........"
        Write-Host "  |  Saved key    : $masked" -ForegroundColor DarkGray
    }
    else {
        Write-Host "  |  Saved key    : none" -ForegroundColor DarkGray
    }

    Write-Host "  |  Settings file: $SettingsPath" -ForegroundColor DarkGray
    Write-Host "  +----------------------------------------------------+" -ForegroundColor DarkGray
    Write-Host ""
    Write-Host "  Tip: run /status inside Claude Code to confirm active auth." -ForegroundColor DarkGray
    Write-Host ""
}

# ── Main ───────────────────────────────────────────────────────────────────────

Write-Host ""
Write-Host "  Claude Code Switcher" -ForegroundColor Magenta

if ($Mode -eq "status") {
    ShowStatus
}
elseif ($Mode -eq "sub") {
    SwitchToSubscription
}
elseif ($Mode -eq "api") {
    SwitchToApi $ApiKey
}
else {
    # toggle
    $current = GetCurrentMode
    Write-Host "  Current mode: $($current.ToUpper())" -ForegroundColor DarkGray
    if ($current -eq "api") {
        SwitchToSubscription
    }
    else {
        SwitchToApi $ApiKey
    }
}
