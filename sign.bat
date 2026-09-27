@echo off
rem SPDX-License-Identifier: Apache-2.0
rem Copyright (c) 2026 ninesix-ai studio
rem Windows one-click launcher: forwards all arguments to the local signing tool
setlocal
set "TARGET=%~dp0script\misc\sign_wonslate.py"
where py >nul 2>nul
if %errorlevel%==0 (
    py "%TARGET%" %*
) else (
    python "%TARGET%" %*
)
endlocal
