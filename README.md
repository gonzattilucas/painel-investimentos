# Rastreador de Ações e FIIs (B3)

Programa em Python (sem bibliotecas extras) que:

1. **Baixa todo dia** as cotações e os proventos das suas ações e FIIs, além do Ibovespa e do IFIX (via ETF XFIX11), e guarda o histórico em `dados/cotacoes.db` (SQLite).
2. **Gera um relatório** (`relatorios/relatorio_AAAA-MM-DD.html`) com preço, variação no dia, em 30 dias e em 12 meses, retorno com proventos, DY dos últimos 12 meses, mínima e máxima de 52 semanas, distância da média de 200 dias, gráfico de 90 dias, patrimônio e alertas.
3. **Sugere o aporte do mês** (`relatorios/aporte_AAAA-MM.md`): distribui o valor entre os ativos que estão mais abaixo do peso-alvo **que você definiu**. Ativos "esticados" e FIIs com DY baixo ficam com prioridade menor.

## Uso

```bash
py rastreador.py atualizar
```
```bash
py rastreador.py relatorio
```
```bash
py rastreador.py aporte --valor 1500
```
```bash
py rastreador.py comprar ITUB4 10 40.53
```

- `diario` = `atualizar` + `relatorio` (é o que o agendador roda).
- `comprar TICKER QTD PRECO` atualiza a quantidade e o preço médio em `carteira.json`. Para registrar uma venda, use quantidade negativa.

## Painel web (relatório semanal)

O painel online fica em https://claude.ai/artifact/LbMLiD38LDH98kLpbKdt1p. Ele é privado: só você vê, até compartilhar pela própria página. Tem cinco abas: Relatório semanal, Carteira, Aporte do mês, Recomendações e Método.

- O mesmo site também é publicado no GitHub Pages (repositório `painel-investimentos`, pasta `docs/`), para abrir no celular.
- `py rastreador.py exportar` gera `docs/dados.json` (o `diario` já faz isso).
- `docs/recomendacoes.json` guarda as carteiras das corretoras, cada uma marcada com horizonte `longo` (comprar para manter) ou `curto` (carteira tática semanal, visando venda). A aba Corretoras calcula as compras e vendas em comum a partir dele.
- **Atualização automática:** a tarefa agendada "Atualizar Painel de Investimentos" do Claude roda segunda, quarta e sexta às 10h. Ela pesquisa as carteiras das corretoras, atualiza `recomendacoes.json`, roda o `diario`, envia ao GitHub (`publicar_site.ps1`) e republica o painel no claude.ai. O app do Claude precisa estar aberto; se estiver fechado no horário, a tarefa roda quando ele abrir.
- Primeira publicação no GitHub, depois do login (`gh auth login`): `powershell -ExecutionPolicy Bypass -File criar_site_github.ps1`.

## Configuração: `carteira.json`

| Campo | O que é |
|---|---|
| `alvo_classes` | % do patrimônio em cada classe, ex.: `{"ACAO": 50, "FII": 50}` |
| `ativos[].peso_alvo` | peso **relativo dentro da classe** (2 = o dobro de um ativo com 1) |
| `ativos[].quantidade` / `preco_medio` | sua posição atual |
| `limite_acima_mm200` | 0.25 = ativo 25% acima da média de 200 dias é considerado esticado |
| `dy_minimo_fii` | 0.08 = FII com DY 12m abaixo de 8% recebe menos prioridade |
| `fator_penalidade` | 0.5 = metade da prioridade; 0 = não compra o ativo penalizado |
| `observacao` | tickers que você só quer acompanhar |

A carteira que vem no arquivo é **um exemplo didático** montado com os ativos mais citados nas carteiras públicas de novembro de 2026 (veja `ANALISE_NOV2026.md`). Substitua pelos seus ativos.

## Rodar sozinho todo dia útil

Rode uma vez no PowerShell, dentro desta pasta:

```bash
powershell -ExecutionPolicy Bypass -File agendar.ps1
```

Isso cria a tarefa "RastreadorInvestimentos" no Agendador de Tarefas, que roda de segunda a sexta às 18:30. O log fica em `dados/log.txt`. Para remover a tarefa:

```bash
powershell -Command "Unregister-ScheduledTask -TaskName RastreadorInvestimentos -Confirm:$false"
```

## Limitações

- As cotações vêm do endpoint público de gráficos do Yahoo Finance, que não é uma API oficial. Pode ter atraso, falhas ou mudar sem aviso.
- O DY é calculado com os proventos que o Yahoo registra. Em ações, JCP e bonificações podem aparecer diferentes do que a empresa informou. Confira no RI ou no Status Invest.
- O programa não calcula P/L, P/VP nem vacância. Consulte esses indicadores no Status Invest, no Investidor10 ou no Funds Explorer.
- A sugestão de aporte é um cálculo mecânico de rebalanceamento. Não é recomendação de investimento.
