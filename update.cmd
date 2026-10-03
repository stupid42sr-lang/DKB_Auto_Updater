@echo off
setlocal
if not defined MDT_SHARED_RUNTIME_HOME set "MDT_SHARED_RUNTIME_HOME=%~dp0..\BIOL_Runtime"
"%MDT_SHARED_RUNTIME_HOME%\1.1.0\python\python.exe" -B "%~dp0updater.py" %*
exit /b %errorlevel%
