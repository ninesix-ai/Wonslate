@echo off
rem run_av_baseline.bat -- Windows entry point for the AV-domain baseline runner.
rem All real logic lives in script/run_av_baseline.py; this file just makes the
rem "double-click to run" story work on Windows without a shell.
rem
rem Common variants:
rem     run_av_baseline.bat --preflight-only
rem     run_av_baseline.bat --quick
rem     run_av_baseline.bat --report-only
setlocal
set SCRIPT_DIR=%~dp0
python "%SCRIPT_DIR%script\run_av_baseline.py" %*
set EXITCODE=%ERRORLEVEL%
echo.
echo [run_av_baseline] exit %EXITCODE%
echo [run_av_baseline] full log: %SCRIPT_DIR%run_av_baseline.log
rem Keep the window open so a double-click does not vanish before the Summary
rem is read. Press any key to close. Scripted callers should invoke
rem script/run_av_baseline.py directly to avoid this pause.
echo.
pause >nul
exit /b %EXITCODE%
