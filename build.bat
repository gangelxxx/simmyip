@echo off
chcp 65001 >nul
echo Сборка MyIP Tray...

:: Одним файлом (удобно переносить)
pyinstaller --noconfirm --windowed --onefile --name MyIP_Tray tray_app.py

:: Раскомментируйте строку ниже и закомментируйте верхнюю,
:: если хотите сборку в папку (быстрее запускается):
:: pyinstaller --noconfirm --windowed --onedir --name MyIP_Tray tray_app.py

echo.
echo Готово! EXE находится в папке dist\MyIP_Tray.exe (или dist\MyIP_Tray\)
pause
