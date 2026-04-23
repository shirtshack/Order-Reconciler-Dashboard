@echo off
title Order Reconciler Workspace

cd /d "C:\Users\info\dev\Create Printify Order Send\divotclub"

echo.
echo ====================================================
echo   Order Reconciler workspace starting...
echo.
echo   Dashboard will open at http://localhost:8502
echo   Claude Code will start in this window
echo.
echo   When you're done:
echo    - Close THIS window to stop Claude Code
echo    - Close the minimized "Dashboard" window to stop the server
echo ====================================================
echo.

REM Launch the Streamlit dashboard in its own minimized window (stays open until you close it)
start "Order Reconciler Dashboard" /min cmd /k "cd /d \"C:\Users\info\dev\Create Printify Order Send\divotclub\" && python -m streamlit run app.py --server.port=8502 --server.headless=true --browser.gatherUsageStats=false"

REM Open browser after 4 seconds (non-blocking)
start "" /min cmd /c "timeout /t 4 /nobreak >nul & start http://localhost:8502"

REM Small pause so the dashboard messages don't clobber the Claude prompt
timeout /t 2 /nobreak >nul

REM Launch Claude Code in THIS window, from the project directory
claude

echo.
echo ====================================================
echo Claude Code has exited.
echo The dashboard window (in your taskbar) is still running.
echo Close it separately when you're done with it.
echo ====================================================
pause
