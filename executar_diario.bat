@echo off
REM Executa a coleta diaria e gera o relatorio. Usado pelo Agendador de Tarefas.
chcp 65001 >nul
cd /d "%~dp0"
if not exist dados mkdir dados
echo ===== %date% %time% ===== >> dados\log.txt
where py >nul 2>&1 && (py rastreador.py diario >> dados\log.txt 2>&1) || (python rastreador.py diario >> dados\log.txt 2>&1)
