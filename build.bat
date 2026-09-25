@echo off
rem SPDX-License-Identifier: Apache-2.0
rem Copyright (c) 2026 ninesix-ai studio
rem Windows one-click launcher: forwards all arguments to build.py
setlocal
set "TARGET=%~dp0script\build.py"
where py >nul 2>nul
if %errorlevel%==0 (
    py "%TARGET%" %*
) else (
    python "%TARGET%" %*
)
endlocal
