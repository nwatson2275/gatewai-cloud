@echo off
title Gemini Project Vault Launcher
echo -------------------------------------------------------
echo 🚀 STARTING YOUR AI PROJECT VAULT
echo -------------------------------------------------------

:: 1. Move to the correct drive and folder
cd /d "E:\Gemini memory project - Dev VI"

:: 2. Launch the Streamlit server in a separate minimized window
:: This allows the batch file to continue to the next line
start /min cmd /c "python -m streamlit run app.py"

echo ⏳ Waiting 5 seconds for the server to warm up...
timeout /t 5 /nobreak >nul

:: 3. Automatically launch your browser to the local address
echo 🌐 Opening browser...


echo ✅ App is now active!
echo ⚠️  DO NOT close the minimized terminal window while using the app.
echo -------------------------------------------------------
pause