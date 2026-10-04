@echo off
rem SPDX-License-Identifier: Apache-2.0
rem Copyright (c) 2026 ninesix-ai studio
rem Windows one-click launcher for the Wonslate MCP stdio server.
rem
rem The server is a cargo auto-target of translator-engine (src/bin/wonslate-mcp.rs),
rem i.e. the "standalone Rust bin" option allowed by the MCP iron rule 4 - it adds no
rem dependency to the engine crate. This script only resolves and launches it; point
rem your MCP client at this file instead of at target\release\wonslate-mcp.exe so the
rem absolute build path never has to be hand-edited into client config.
setlocal
set "TARGET=%~dp0..\translator-engine\target\release\wonslate-mcp.exe"
if not exist "%TARGET%" (
    echo [run-mcp] MCP server not built yet: %TARGET%
    echo [run-mcp] build it first:  python script\build.py
    echo [run-mcp]   or:            cd translator-engine ^&^& cargo build --release
    exit /b 2
)
"%TARGET%" %*
set "RC=%ERRORLEVEL%"
endlocal
exit /b %RC%
