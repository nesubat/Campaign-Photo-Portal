@echo off
cd C:\Users\sbasnet\Campaign-Photo-Portal
Call venv/Scripts/activate
echo -----------------------------------------------
echo Starting the Campaign Photo Portal server...
echo -----------------------------------------------

echo Don't close this window while someone is using the portal.
echo Press Ctrl+C to stop the server.

py serve.py

pause
