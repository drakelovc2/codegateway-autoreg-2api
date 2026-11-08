@echo off
chcp 65001 >nul 2>&1
title <НАЗВАНИЕ ПРОЕКТА>
setlocal enabledelayedexpansion

REM ============================================================
REM  Универсальный Windows-лаунчер.
REM  Сам создаёт venv, ставит зависимости, копирует пример конфига.
REM  Пользователю не нужно дописывать код — только заполнить конфиг.
REM  Флаги: --reinstall  (пересоздать venv)
REM         --skip-install (не ставить зависимости, просто запустить)
REM ============================================================

cd /d "%~dp0"

set REINSTALL=0
set SKIP_INSTALL=0
for %%a in (%*) do (
    if /I "%%a"=="--reinstall" set REINSTALL=1
    if /I "%%a"=="--skip-install" set SKIP_INSTALL=1
)

echo ================================
echo   <НАЗВАНИЕ ПРОЕКТА>
echo ================================
echo.

REM ---------- 1. Найти Python ----------
set PYTHON_BIN=
for %%P in (python python3 py) do (
    if not defined PYTHON_BIN (
        %%P --version >nul 2>&1
        if not errorlevel 1 set PYTHON_BIN=%%P
    )
)
if not defined PYTHON_BIN (
    echo [ERROR] Python не найден.
    echo Установи Python 3.10+ : https://www.python.org/downloads/
    echo ВАЖНО: при установке отметь "Add Python to PATH".
    pause
    exit /b 1
)
echo [*] Python:
%PYTHON_BIN% --version

REM ---------- 2. venv ----------
if "%REINSTALL%"=="1" (
    if exist venv (
        echo [*] --reinstall: удаляю старый venv...
        rmdir /s /q venv
    )
)
set VENV_PY=venv\Scripts\python.exe
if not exist "%VENV_PY%" (
    echo [*] Создаю виртуальное окружение...
    %PYTHON_BIN% -m venv venv
    if errorlevel 1 (
        echo [ERROR] Не удалось создать venv. Переустанови Python с полными компонентами.
        pause
        exit /b 1
    )
)

REM ---------- 3. Зависимости ----------
if "%SKIP_INSTALL%"=="1" goto SKIP_DEPS
"%VENV_PY%" -c "import requests" >nul 2>&1
if errorlevel 1 (
    echo [*] Ставлю зависимости (1-3 минуты)...
    "%VENV_PY%" -m pip install --upgrade pip --quiet
    if exist requirements.txt (
        "%VENV_PY%" -m pip install -r requirements.txt
    ) else (
        echo [!] requirements.txt не найден — пропускаю установку.
    )
    if errorlevel 1 (
        echo.
        echo [ERROR] Установка не удалась. Проверь интернет и запусти: start.bat --reinstall
        pause
        exit /b 1
    )
    echo [*] Зависимости установлены.
)
:SKIP_DEPS

REM ---------- 4. Конфиг из примера ----------
if not exist config.json (
    if exist config.example.json (
        copy /y config.example.json config.json >nul
        echo [!] Создан config.json из примера — ЗАПОЛНИ ЕГО перед первым запуском.
    )
)
if not exist config.toml (
    if exist config.example.toml (
        copy /y config.example.toml config.toml >nul
        echo [!] Создан config.toml из примера — ЗАПОЛНИ ЕГО перед первым запуском.
    )
)
if not exist .env (
    if exist .env.example (
        copy /y .env.example .env >nul
        echo [!] Создан .env из примера — ЗАПОЛНИ ЕГО перед первым запуском.
    )
)

REM ---------- 5. Запуск ----------
echo.
echo [*] Запускаю...
echo.
REM ЗАМЕНИ строку ниже на реальную точку входа проекта:
"%VENV_PY%" main.py

echo.
echo [*] Процесс завершён.
pause
