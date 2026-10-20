@echo off
rem SPDX-License-Identifier: Apache-2.0
rem Copyright (c) 2026 ninesix-ai studio
rem Windows one-click launcher: forwards all arguments to voice_acceptance.py
rem (the WPF app under acceptance is Windows-only, so there is no .sh twin)
setlocal
set "TARGET=%~dp0script\voice_acceptance.py"
where py >nul 2>nul
if %errorlevel%==0 (
    py "%TARGET%" %*
) else (
    python "%TARGET%" %*
)
endlocal
