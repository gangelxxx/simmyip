@echo off
chcp 65001 >nul
echo Сборка MyIP Tray...

:: В папку (быстрее запускается: не распаковывает бандл во временную папку
:: при каждом старте).
pyinstaller --noconfirm --windowed --onedir --name MyIP_Tray tray_app.py

:: Раскомментируйте строку ниже и закомментируйте верхнюю,
:: если хотите сборку одним файлом (удобно переносить, но стартует медленнее):
:: pyinstaller --noconfirm --windowed --onefile --name MyIP_Tray tray_app.py

echo.
echo Готово! EXE находится в папке dist\MyIP_Tray.exe (или dist\MyIP_Tray\)
pause
