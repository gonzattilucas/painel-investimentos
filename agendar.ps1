# Cria a tarefa "RastreadorInvestimentos" no Agendador de Tarefas do Windows:
# roda de segunda a sexta as 18:30 (apos o fechamento da B3). Execute uma vez:
#   powershell -ExecutionPolicy Bypass -File agendar.ps1
# Para remover:  Unregister-ScheduledTask -TaskName RastreadorInvestimentos -Confirm:$false

$bat = Join-Path $PSScriptRoot "executar_diario.bat"
$acao = New-ScheduledTaskAction -Execute "cmd.exe" -Argument "/c `"$bat`"" -WorkingDirectory $PSScriptRoot
$gatilho = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At "18:30"
$opcoes = New-ScheduledTaskSettingsSet -StartWhenAvailable -DontStopIfGoingOnBatteries -AllowStartIfOnBatteries
Register-ScheduledTask -TaskName "RastreadorInvestimentos" -Action $acao -Trigger $gatilho -Settings $opcoes `
    -Description "Atualiza cotacoes de acoes e FIIs e gera relatorio diario" -Force | Out-Null
Write-Host "Tarefa criada. Ela roda dias uteis as 18:30 (e assim que o PC ligar, se estiver desligado nesse horario)."
