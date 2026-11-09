#!/usr/bin/env python3
"""
Rastreador diario de Acoes e FIIs da B3 + planejador de aportes mensais.

Usa somente a biblioteca padrao do Python (sem pip install).
Fonte das cotacoes: endpoint publico de graficos do Yahoo Finance (atraso de ~15 min).

Comandos:
  python rastreador.py atualizar            baixa cotacoes e proventos de todos os ativos
  python rastreador.py relatorio            mostra o resumo e gera relatorios/relatorio_AAAA-MM-DD.html
  python rastreador.py diario               atualizar + relatorio (use no agendador do Windows)
  python rastreador.py aporte [--valor N]   sugere como distribuir o aporte do mes
  python rastreador.py comprar TICKER QTD PRECO   registra uma compra (atualiza preco medio)
"""
import argparse
import bisect
import html
import json
import sqlite3
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

BASE = Path(__file__).resolve().parent
CARTEIRA = BASE / "carteira.json"
DB = BASE / "dados" / "cotacoes.db"
REL = BASE / "relatorios"
YAHOO = "https://query1.finance.yahoo.com/v8/finance/chart/{}?range={}&interval=1d&events=div"
# O Yahoo nao tem historico do indice IFIX; o ETF XFIX11 replica o IFIX e serve de referencia.
BENCHMARKS = {"IBOV": "^BVSP", "XFIX11": "XFIX11.SA"}
NOME_BENCH = {"IBOV": "Ibovespa", "XFIX11": "IFIX (via ETF XFIX11)"}
WEB = BASE / "docs"  # pasta do site (GitHub Pages publica a pasta /docs)
FUSO_B3 = timezone(timedelta(hours=-3))

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass


# ----------------------------------------------------------------- formatacao
def brl(v):
    if v is None:
        return "-"
    return "R$ " + f"{v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def pct(v, sinal=True):
    if v is None:
        return "-"
    s = f"{v * 100:+.2f}%" if sinal else f"{v * 100:.2f}%"
    return s.replace(".", ",")


def num(v, casas=2):
    if v is None:
        return "-"
    return f"{v:,.{casas}f}".replace(",", "X").replace(".", ",").replace("X", ".")


# ------------------------------------------------------------------- carteira
def carregar_carteira():
    if not CARTEIRA.exists():
        sys.exit(f"Arquivo {CARTEIRA.name} nao encontrado na pasta {BASE}.")
    with open(CARTEIRA, encoding="utf-8") as f:
        cart = json.load(f)
    cfg = cart.setdefault("configuracao", {})
    cfg.setdefault("aporte_mensal_padrao", 1000)
    cfg.setdefault("alvo_classes", {"ACAO": 50, "FII": 50})
    cfg.setdefault("limite_acima_mm200", 0.25)
    cfg.setdefault("dy_minimo_fii", 0.08)
    cfg.setdefault("fator_penalidade", 0.5)
    cart.setdefault("ativos", [])
    cart.setdefault("observacao", [])
    for a in cart["ativos"]:
        a["ticker"] = a["ticker"].upper().strip()
        a["tipo"] = a.get("tipo", "ACAO").upper()
        a.setdefault("quantidade", 0)
        a.setdefault("preco_medio", 0)
        a.setdefault("peso_alvo", 1)
        a.setdefault("setor", "")
    return cart


def salvar_carteira(cart):
    with open(CARTEIRA, "w", encoding="utf-8") as f:
        json.dump(cart, f, ensure_ascii=False, indent=2)


def simbolo_yahoo(ticker):
    if ticker in BENCHMARKS:
        return BENCHMARKS[ticker]
    return ticker if ticker.startswith("^") or "." in ticker else ticker + ".SA"


def tickers_recomendados():
    """Tickers citados nas carteiras das corretoras (docs/recomendacoes.json)."""
    arq = WEB / "recomendacoes.json"
    if not arq.exists():
        return []
    try:
        rec = json.loads(arq.read_text(encoding="utf-8"))
    except ValueError:
        print("Aviso: docs/recomendacoes.json invalido; ignorado.")
        return []
    saida = []
    for c in rec.get("carteiras", []):
        saida += [i["ticker"] for i in c.get("itens", [])] + c.get("entradas", []) + [s["ticker"] for s in c.get("saidas", [])]
    for cons in rec.get("consensos", []):
        saida += [i["ticker"] for i in cons.get("itens", [])]
    return [t.upper() for t in saida]


def todos_tickers(cart):
    vistos, saida = set(), []
    for t in ([a["ticker"] for a in cart["ativos"]] + [t.upper() for t in cart["observacao"]] + list(BENCHMARKS)
              + tickers_recomendados()):
        if t not in vistos:
            vistos.add(t)
            saida.append(t)
    return saida


# ------------------------------------------------------------------ banco
def conectar():
    DB.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB)
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS precos(
            ticker TEXT, data TEXT, abertura REAL, maxima REAL, minima REAL,
            fechamento REAL, ajustado REAL, volume INTEGER, PRIMARY KEY(ticker, data));
        CREATE TABLE IF NOT EXISTS dividendos(
            ticker TEXT, data TEXT, valor REAL, PRIMARY KEY(ticker, data));
        CREATE TABLE IF NOT EXISTS patrimonio(
            data TEXT PRIMARY KEY, valor REAL, investido REAL);
        """
    )
    return con


# ------------------------------------------------------------------ coleta
def baixar(simbolo, periodo="2y"):
    url = YAHOO.format(urllib.parse.quote(simbolo), periodo)
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    for tentativa in range(3):
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                dados = json.load(r)
            break
        except urllib.error.HTTPError as e:
            if e.code in (400, 404):
                raise ValueError("ticker nao encontrado") from None
            if tentativa == 2:
                raise
        except (urllib.error.URLError, TimeoutError):
            if tentativa == 2:
                raise
        time.sleep(2 * (tentativa + 1))

    resultado = (dados.get("chart") or {}).get("result")
    if not resultado:
        raise ValueError("sem dados retornados")
    res = resultado[0]
    offset = res.get("meta", {}).get("gmtoffset", -10800)
    ts = res.get("timestamp") or []
    q = res["indicators"]["quote"][0]
    adj = (res["indicators"].get("adjclose") or [{}])[0].get("adjclose") or [None] * len(ts)

    def dia(t):
        return datetime.fromtimestamp(t + offset, tz=timezone.utc).date().isoformat()

    # O pregao da B3 vai das 10h as 17h; antes das 18h a barra de hoje e parcial e fica de fora.
    agora = datetime.now(FUSO_B3)
    hoje_parcial = agora.date().isoformat() if agora.hour < 18 else None
    barras = []
    for i, t in enumerate(ts):
        c = q["close"][i]
        if c is None or dia(t) == hoje_parcial:
            continue
        barras.append((dia(t), q["open"][i], q["high"][i], q["low"][i], c,
                       adj[i] if adj[i] is not None else c, q["volume"][i]))
    divs = [(dia(int(d["date"])), d["amount"])
            for d in (res.get("events") or {}).get("dividends", {}).values()]
    return barras, divs


def cmd_atualizar(args=None):
    cart = carregar_carteira()
    con = conectar()
    print(f"Atualizando cotacoes ({datetime.now():%d/%m/%Y %H:%M})")
    erros = 0
    for t in todos_tickers(cart):
        try:
            barras, divs = baixar(simbolo_yahoo(t))
        except Exception as e:
            erros += 1
            print(f"  {t:<8} ERRO: {e}")
            continue
        con.executemany("INSERT OR REPLACE INTO precos VALUES (?,?,?,?,?,?,?,?)",
                        [(t, *b) for b in barras])
        con.executemany("INSERT OR REPLACE INTO dividendos VALUES (?,?,?)",
                        [(t, d, v) for d, v in divs])
        if barras:
            print(f"  {t:<8} {barras[-1][0]}  {num(barras[-1][4]):>12}  ({len(divs)} proventos em 2 anos)")
        time.sleep(0.3)
    con.commit()
    registrar_patrimonio(con, cart)
    con.close()
    if erros:
        print(f"{erros} ativo(s) com erro. Confira os tickers em carteira.json.")


def registrar_patrimonio(con, cart):
    valor = investido = 0.0
    ultima = None
    for a in cart["ativos"]:
        if not a["quantidade"]:
            continue
        r = con.execute("SELECT data, fechamento FROM precos WHERE ticker=? ORDER BY data DESC LIMIT 1",
                        (a["ticker"],)).fetchone()
        if r:
            ultima = max(ultima or r[0], r[0])
            valor += a["quantidade"] * r[1]
            investido += a["quantidade"] * a["preco_medio"]
    if ultima and valor:
        con.execute("INSERT OR REPLACE INTO patrimonio VALUES (?,?,?)", (ultima, valor, investido))
        con.commit()


# ------------------------------------------------------------------ metricas
def metricas(con, ticker):
    rows = con.execute("SELECT data, fechamento, ajustado FROM precos WHERE ticker=? ORDER BY data",
                       (ticker,)).fetchall()
    if not rows:
        return None
    datas = [r[0] for r in rows]
    ult_data = date.fromisoformat(datas[-1])
    preco, ajust = rows[-1][1], rows[-1][2]

    def linha_em(dias):
        alvo = (ult_data - timedelta(days=dias)).isoformat()
        i = bisect.bisect_right(datas, alvo) - 1
        return rows[i] if i >= 0 else None

    def variacao(dias, col=1):
        r = linha_em(dias)
        return (rows[-1][col] / r[col] - 1) if r and r[col] else None

    janela = [r[1] for r in rows[-252:]]
    mm200 = statistics.fmean(r[1] for r in rows[-200:]) if len(rows) >= 200 else None
    inicio12 = (ult_data - timedelta(days=365)).isoformat()
    prov12 = con.execute("SELECT COALESCE(SUM(valor),0), COUNT(*) FROM dividendos WHERE ticker=? AND data>?",
                         (ticker, inicio12)).fetchone()
    return {
        "data": datas[-1],
        "preco": preco,
        "var_dia": (preco / rows[-2][1] - 1) if len(rows) > 1 else None,
        "var_30d": variacao(30),
        "var_12m": variacao(365),
        "ret_total_12m": variacao(365, col=2),
        "max52": max(janela),
        "min52": min(janela),
        "mm200": mm200,
        "dist_mm200": (preco / mm200 - 1) if mm200 else None,
        "prov_12m": prov12[0],
        "n_prov_12m": prov12[1],
        "dy_12m": (prov12[0] / preco) if preco else None,
        "serie": [r[1] for r in rows[-90:]],
    }


def alertas_ativo(m, tipo, cfg):
    al = []
    if m is None:
        return ["sem cotacao"]
    if m["dist_mm200"] is not None and m["dist_mm200"] > cfg["limite_acima_mm200"]:
        al.append(f"{pct(m['dist_mm200'])} acima da media de 200 dias (esticado)")
    if m["preco"] <= m["min52"] * 1.03:
        al.append("perto da minima de 52 semanas")
    if m["preco"] >= m["max52"] * 0.98:
        al.append("perto da maxima de 52 semanas")
    if tipo == "FII" and m["dy_12m"] is not None and m["dy_12m"] < cfg["dy_minimo_fii"]:
        al.append(f"DY 12m {pct(m['dy_12m'], False)} abaixo do minimo configurado")
    if m["var_dia"] is not None and abs(m["var_dia"]) >= 0.05:
        al.append(f"variacao forte no dia ({pct(m['var_dia'])})")
    return al


# ------------------------------------------------------------------ relatorio
def sparkline(serie, w=110, h=28):
    if len(serie) < 2:
        return ""
    lo, hi = min(serie), max(serie)
    amp = (hi - lo) or 1
    pts = " ".join(f"{i * w / (len(serie) - 1):.1f},{h - 2 - (v - lo) / amp * (h - 4):.1f}"
                   for i, v in enumerate(serie))
    cor = "var(--pos)" if serie[-1] >= serie[0] else "var(--neg)"
    return (f'<svg width="{w}" height="{h}" viewBox="0 0 {w} {h}" aria-hidden="true">'
            f'<polyline points="{pts}" fill="none" stroke="{cor}" stroke-width="1.5"/></svg>')


def classe_cor(v):
    if v is None:
        return ""
    return "pos" if v > 0 else "neg" if v < 0 else ""


def cmd_relatorio(args=None):
    cart = carregar_carteira()
    cfg = cart["configuracao"]
    con = conectar()
    linhas, alertas = [], []
    total = investido = 0.0
    por_classe = {}
    for a in cart["ativos"]:
        m = metricas(con, a["ticker"])
        valor = a["quantidade"] * m["preco"] if m else 0
        total += valor
        investido += a["quantidade"] * a["preco_medio"]
        por_classe[a["tipo"]] = por_classe.get(a["tipo"], 0) + valor
        for txt in alertas_ativo(m, a["tipo"], cfg):
            alertas.append((a["ticker"], txt))
        linhas.append((a, m, valor))

    bench = {t: metricas(con, t) for t in BENCHMARKS}
    obs = [(t.upper(), metricas(con, t.upper())) for t in cart["observacao"]]
    hist = con.execute("SELECT data, valor FROM patrimonio ORDER BY data").fetchall()
    con.close()

    hoje = date.today().isoformat()
    # ---- console
    print(f"\n=== Carteira em {hoje} ===")
    print(f"{'Ativo':<8}{'Tipo':<6}{'Preco':>11}{'Dia':>9}{'30d':>9}{'12m':>9}{'DY12m':>8}{'Qtd':>7}{'Valor':>15}")
    for a, m, valor in linhas:
        if not m:
            print(f"{a['ticker']:<8}{a['tipo']:<6}  sem dados - rode 'atualizar'")
            continue
        print(f"{a['ticker']:<8}{a['tipo']:<6}{num(m['preco']):>11}{pct(m['var_dia']):>9}{pct(m['var_30d']):>9}"
              f"{pct(m['var_12m']):>9}{pct(m['dy_12m'], False):>8}{a['quantidade']:>7}{brl(valor):>15}")
    if total:
        print(f"\nPatrimonio: {brl(total)}   Investido: {brl(investido)}   "
              f"Resultado: {brl(total - investido)} ({pct(total / investido - 1) if investido else '-'})")
    for t, m in bench.items():
        if m:
            print(f"{NOME_BENCH.get(t, t)}: {num(m['preco'])}  dia {pct(m['var_dia'])}  12m {pct(m['var_12m'])}")
    if alertas:
        print("\nAlertas:")
        for t, txt in alertas:
            print(f"  - {t}: {txt}")

    # ---- HTML
    REL.mkdir(exist_ok=True)
    arq = REL / f"relatorio_{hoje}.html"
    arq.write_text(gerar_html(hoje, cart, linhas, total, investido, por_classe, bench, obs, alertas, hist),
                   encoding="utf-8")
    print(f"\nRelatorio salvo em: {arq}")


def gerar_html(hoje, cart, linhas, total, investido, por_classe, bench, obs, alertas, hist):
    e = html.escape
    cfg = cart["configuracao"]

    def linha_ativo(t, tipo, setor, m, qtd=None, valor=None, pm=None):
        if not m:
            return f"<tr><td>{e(t)}</td><td colspan='11'>sem dados</td></tr>"
        res = (m["preco"] / pm - 1) if pm else None
        return (
            f"<tr><td><b>{e(t)}</b><div class='muted'>{e(tipo)} {e(setor)}</div></td>"
            f"<td class='n'>{num(m['preco'])}</td>"
            f"<td class='n {classe_cor(m['var_dia'])}'>{pct(m['var_dia'])}</td>"
            f"<td class='n {classe_cor(m['var_30d'])}'>{pct(m['var_30d'])}</td>"
            f"<td class='n {classe_cor(m['var_12m'])}'>{pct(m['var_12m'])}</td>"
            f"<td class='n {classe_cor(m['ret_total_12m'])}'>{pct(m['ret_total_12m'])}</td>"
            f"<td class='n'>{pct(m['dy_12m'], False)}</td>"
            f"<td class='n'>{num(m['min52'])} / {num(m['max52'])}</td>"
            f"<td class='n'>{pct(m['dist_mm200'])}</td>"
            f"<td>{sparkline(m['serie'])}</td>"
            + (f"<td class='n'>{qtd}</td><td class='n'>{brl(valor)}<div class='muted {classe_cor(res)}'>{pct(res)}</div></td>"
               if qtd is not None else "")
            + "</tr>"
        )

    cab = ("<tr><th>Ativo</th><th>Preco</th><th>Dia</th><th>30d</th><th>12m</th><th>12m c/ prov.</th>"
           "<th>DY 12m</th><th>Min/Max 52s</th><th>vs MM200</th><th>90 dias</th>")
    corpo = "".join(linha_ativo(a["ticker"], a["tipo"], a["setor"], m, a["quantidade"], v, a["preco_medio"])
                    for a, m, v in linhas)
    corpo_obs = "".join(linha_ativo(t, "", "", m) for t, m in obs)

    alvo = cfg["alvo_classes"]
    soma_alvo = sum(alvo.values()) or 1
    aloc = "".join(
        f"<tr><td>{e(c)}</td><td class='n'>{brl(por_classe.get(c, 0))}</td>"
        f"<td class='n'>{pct(por_classe.get(c, 0) / total if total else 0, False)}</td>"
        f"<td class='n'>{pct(alvo.get(c, 0) / soma_alvo, False)}</td></tr>"
        for c in sorted(set(alvo) | set(por_classe)))
    cards = "".join(
        f"<div class='card'><div class='muted'>{e(NOME_BENCH.get(t, t))}</div><div class='big'>{num(m['preco'])}</div>"
        f"<div class='{classe_cor(m['var_dia'])}'>dia {pct(m['var_dia'])}</div>"
        f"<div class='{classe_cor(m['var_12m'])}'>12m {pct(m['var_12m'])}</div></div>"
        for t, m in bench.items() if m)
    resultado = total - investido
    cards = (f"<div class='card'><div class='muted'>Patrimonio</div><div class='big'>{brl(total)}</div>"
             f"<div class='{classe_cor(resultado)}'>{brl(resultado)} ({pct(resultado / investido) if investido else '-'})</div>"
             f"<div class='muted'>investido {brl(investido)}</div></div>") + cards
    lista_alertas = "".join(f"<li><b>{e(t)}</b>: {e(x)}</li>" for t, x in alertas) or "<li>Nenhum alerta hoje.</li>"
    hist_svg = sparkline([v for _, v in hist], w=600, h=80) if len(hist) > 1 else \
        "<p class='muted'>O grafico do patrimonio aparece depois de alguns dias de execucao.</p>"

    return f"""<!DOCTYPE html>
<html lang="pt-BR"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Carteira {hoje}</title>
<style>
:root{{--bg:#f7f7f5;--fg:#1d1d1b;--muted:#6b6b66;--card:#fff;--line:#e3e3de;--pos:#1a7f37;--neg:#c62828}}
@media (prefers-color-scheme:dark){{:root{{--bg:#161615;--fg:#ececea;--muted:#9a9a94;--card:#21211f;--line:#33332f;--pos:#4cc36b;--neg:#ff6b6b}}}}
body{{background:var(--bg);color:var(--fg);font:14px/1.45 system-ui,Segoe UI,sans-serif;margin:0;padding:24px 16px}}
main{{max-width:1200px;margin:auto}} h1{{font-size:22px;margin:0 0 4px}} h2{{font-size:16px;margin:28px 0 8px}}
.muted{{color:var(--muted);font-size:12px}} .pos{{color:var(--pos)}} .neg{{color:var(--neg)}}
.cards{{display:flex;gap:12px;flex-wrap:wrap}} .card{{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 16px;min-width:180px}}
.big{{font-size:20px;font-weight:600;font-variant-numeric:tabular-nums}}
.wrap{{overflow-x:auto;background:var(--card);border:1px solid var(--line);border-radius:10px}}
table{{border-collapse:collapse;width:100%}} th,td{{padding:7px 10px;border-bottom:1px solid var(--line);white-space:nowrap;text-align:left}}
th{{font-size:12px;color:var(--muted);font-weight:500}} .n{{text-align:right;font-variant-numeric:tabular-nums}}
li{{margin:3px 0}}
</style></head><body><main>
<h1>Carteira de Acoes e FIIs</h1>
<div class="muted">Atualizado em {hoje} - cotacoes do Yahoo Finance (podem ter atraso). DY calculado pelos proventos dos ultimos 12 meses; confira no RI/Status Invest.</div>
<h2>Resumo</h2><div class="cards">{cards}</div>
<h2>Evolucao do patrimonio</h2><div>{hist_svg}</div>
<h2>Alocacao por classe</h2><div class="wrap"><table><tr><th>Classe</th><th>Valor</th><th>Atual</th><th>Alvo</th></tr>{aloc}</table></div>
<h2>Meus ativos</h2><div class="wrap"><table>{cab}<th>Qtd</th><th>Valor / resultado</th></tr>{corpo}</table></div>
<h2>Alertas</h2><ul>{lista_alertas}</ul>
<h2>Lista de observacao</h2><div class="wrap"><table>{cab}</tr>{corpo_obs or "<tr><td>Vazia</td></tr>"}</table></div>
</main></body></html>"""


# ------------------------------------------------------------------ aporte
def calcular_aporte(cart, con, valor_aporte, avisar=True):
    cfg = cart["configuracao"]
    alvo_cls = cfg["alvo_classes"]
    soma_cls = sum(alvo_cls.values()) or 1
    pesos_cls = {}
    for a in cart["ativos"]:
        pesos_cls[a["tipo"]] = pesos_cls.get(a["tipo"], 0) + a["peso_alvo"]

    itens = []
    for a in cart["ativos"]:
        m = metricas(con, a["ticker"])
        if not m:
            if avisar:
                print(f"Aviso: {a['ticker']} sem cotacao, ignorado. Rode 'atualizar'.")
            continue
        if a["peso_alvo"] <= 0 or a["tipo"] not in alvo_cls:
            continue
        motivos, fator = [], 1.0
        if m["dist_mm200"] is not None and m["dist_mm200"] > cfg["limite_acima_mm200"]:
            fator *= cfg["fator_penalidade"]
            motivos.append(f"esticado ({pct(m['dist_mm200'])} vs MM200)")
        if a["tipo"] == "FII" and m["dy_12m"] is not None and m["dy_12m"] < cfg["dy_minimo_fii"]:
            fator *= cfg["fator_penalidade"]
            motivos.append(f"DY 12m baixo ({pct(m['dy_12m'], False)})")
        itens.append({"a": a, "m": m, "preco": m["preco"], "atual": a["quantidade"] * m["preco"],
                      "fator": fator, "motivos": motivos, "compra": 0})
    if not itens:
        return [], 0.0, valor_aporte, valor_aporte

    total_atual = sum(i["atual"] for i in itens)
    total_final = total_atual + valor_aporte
    for i in itens:
        a = i["a"]
        i["alvo_pct"] = alvo_cls[a["tipo"]] / soma_cls * a["peso_alvo"] / pesos_cls[a["tipo"]]
        i["falta"] = max(0.0, total_final * i["alvo_pct"] - i["atual"])

    # Fase 1: compra cota a cota no ativo proporcionalmente mais abaixo do alvo (ponderado pelo
    # fator de valuation), so enquanto a cota "cabe" na falta (falta >= meia cota), evitando passar do alvo.
    # Fase 2: a sobra vai para quem ultrapassar menos o alvo.
    caixa = valor_aporte
    while True:
        cand = [i for i in itens if i["fator"] > 0 and i["preco"] <= caixa and i["falta"] >= i["preco"] / 2]
        if not cand:
            break
        i = max(cand, key=lambda x: x["falta"] * x["fator"] / (total_final * x["alvo_pct"]))
        i["compra"] += 1
        i["falta"] -= i["preco"]
        caixa -= i["preco"]
    while True:
        cand = [i for i in itens if i["fator"] > 0 and i["preco"] <= caixa and i["falta"] > 0]
        if not cand:
            break
        i = max(cand, key=lambda x: x["falta"] * x["fator"] - x["preco"])
        i["compra"] += 1
        i["falta"] -= i["preco"]
        caixa -= i["preco"]
    return itens, total_atual, total_final, caixa


def cmd_aporte(args):
    cart = carregar_carteira()
    valor_aporte = args.valor if args.valor is not None else cart["configuracao"]["aporte_mensal_padrao"]
    con = conectar()
    itens, total_atual, total_final, caixa = calcular_aporte(cart, con, valor_aporte)
    con.close()
    if not itens:
        sys.exit("Nenhum ativo com cotacao e peso_alvo > 0.")

    mes = date.today().strftime("%Y-%m")
    saida = [f"# Sugestao de aporte - {mes}", "",
             f"Aporte: **{brl(valor_aporte)}** | Patrimonio antes: {brl(total_atual)} | depois: {brl(total_final - caixa)}", "",
             "| Ativo | Tipo | Preco | Comprar | Total | Peso atual | Peso alvo | Peso depois | Observacao |",
             "|---|---|---:|---:|---:|---:|---:|---:|---|"]
    print(f"\nAporte de {brl(valor_aporte)} - patrimonio atual {brl(total_atual)}\n")
    print(f"{'Ativo':<8}{'Preco':>10}{'Comprar':>9}{'Total':>14}{'Atual':>9}{'Alvo':>9}{'Depois':>9}  Obs")
    base = total_final - caixa
    for i in sorted(itens, key=lambda x: -x["compra"] * x["preco"]):
        t = i["a"]["ticker"]
        gasto = i["compra"] * i["preco"]
        p_atual = i["atual"] / total_atual if total_atual else 0
        p_depois = (i["atual"] + gasto) / base if base else 0
        obs = "; ".join(i["motivos"])
        print(f"{t:<8}{num(i['preco']):>10}{i['compra']:>9}{brl(gasto):>14}{pct(p_atual, False):>9}"
              f"{pct(i['alvo_pct'], False):>9}{pct(p_depois, False):>9}  {obs}")
        saida.append(f"| {t} | {i['a']['tipo']} | {num(i['preco'])} | {i['compra']} | {brl(gasto)} | "
                     f"{pct(p_atual, False)} | {pct(i['alvo_pct'], False)} | {pct(p_depois, False)} | {obs} |")
    print(f"\nSobra sem alocar: {brl(caixa)} (guarde para o proximo mes ou na reserva)")
    saida += ["", f"Sobra sem alocar: {brl(caixa)}", "",
              "Metodo: rebalanceamento por aporte - o dinheiro vai para os ativos mais abaixo do peso-alvo; "
              "ativos esticados (muito acima da media de 200 dias) ou FIIs com DY abaixo do minimo recebem prioridade menor. "
              "Isto e um calculo mecanico baseado nos alvos que VOCE definiu em carteira.json, nao uma recomendacao de investimento."]
    REL.mkdir(exist_ok=True)
    arq = REL / f"aporte_{mes}.md"
    arq.write_text("\n".join(saida), encoding="utf-8")
    print(f"Plano salvo em: {arq}")
    print("Depois de comprar, registre com: python rastreador.py comprar TICKER QTD PRECO")


# ------------------------------------------------------------------ comprar
def cmd_comprar(args):
    cart = carregar_carteira()
    t = args.ticker.upper()
    ativo = next((a for a in cart["ativos"] if a["ticker"] == t), None)
    if ativo is None:
        tipo = "FII" if t.endswith("11") else "ACAO"
        print(f"{t} nao estava na carteira; adicionado como {tipo} com peso_alvo 1 (ajuste em carteira.json).")
        ativo = {"ticker": t, "tipo": tipo, "setor": "", "quantidade": 0, "preco_medio": 0, "peso_alvo": 1}
        cart["ativos"].append(ativo)
    q_ant, pm_ant = ativo["quantidade"], ativo["preco_medio"]
    q_nova = q_ant + args.quantidade
    if q_nova < 0:
        sys.exit("Quantidade final ficaria negativa.")
    if args.quantidade > 0:
        ativo["preco_medio"] = round((q_ant * pm_ant + args.quantidade * args.preco) / q_nova, 4)
    ativo["quantidade"] = q_nova
    salvar_carteira(cart)
    print(f"{t}: {q_ant} -> {q_nova} cotas, preco medio {num(pm_ant)} -> {num(ativo['preco_medio'])}")


# ------------------------------------------------------------------ exportar (painel web)
def _semana_iso(d):
    y, w, _ = date.fromisoformat(d).isocalendar()
    return (y, w)


def cmd_exportar(args=None):
    """Gera web/dados.json, lido pelo painel web (relatorio semanal + carteira + aporte)."""
    cart = carregar_carteira()
    cfg = cart["configuracao"]
    con = conectar()
    ref = con.execute("SELECT MAX(data) FROM precos WHERE ticker='IBOV'").fetchone()[0]
    if not ref:
        sys.exit("Sem dados. Rode 'atualizar' primeiro.")
    ref_d = date.fromisoformat(ref)
    inicio_sem = (ref_d - timedelta(days=ref_d.weekday())).isoformat()

    def historico(t):
        return con.execute("SELECT data, fechamento, ajustado FROM precos WHERE ticker=? AND data<=? ORDER BY data",
                           (t, ref)).fetchall()

    def fech_semanais(rows):
        d = {}
        for r in rows:
            d[_semana_iso(r[0])] = r  # fica a ultima linha de cada semana
        return d

    fs_ibov = fech_semanais(historico("IBOV"))
    semanas_ref = sorted(fs_ibov)[-27:]  # 27 fechamentos semanais -> 26 retornos

    def retornos_semanais(rows, col=2):
        fs = fech_semanais(rows)
        out = []
        for k_ant, k in zip(semanas_ref, semanas_ref[1:]):
            a, b = fs.get(k_ant), fs.get(k)
            out.append(round(b[col] / a[col] - 1, 6) if a and b and a[col] else None)
        return out

    def fech_antes_semana(rows):
        antes = [r for r in rows if r[0] < inicio_sem]
        return antes[-1][1] if antes else None

    alvo_cls = cfg["alvo_classes"]
    soma_cls = sum(alvo_cls.values()) or 1
    pesos_cls = {}
    for a in cart["ativos"]:
        pesos_cls[a["tipo"]] = pesos_cls.get(a["tipo"], 0) + a["peso_alvo"]

    def alvo_pct(a):
        if a["tipo"] not in alvo_cls or not pesos_cls.get(a["tipo"]):
            return 0
        return alvo_cls[a["tipo"]] / soma_cls * a["peso_alvo"] / pesos_cls[a["tipo"]]

    inicio_prov = (ref_d - timedelta(days=35)).isoformat()

    def item(t, a=None, tipo=None):
        m = metricas(con, t)
        if not m:
            return None
        rows = historico(t)
        tipo = a["tipo"] if a else (tipo or ("FII" if t.endswith("11") else "ACAO"))
        ant = fech_antes_semana(rows)
        provs = con.execute("SELECT data, valor FROM dividendos WHERE ticker=? AND data>=? ORDER BY data DESC",
                            (t, inicio_prov)).fetchall()
        d = {
            "ticker": t, "tipo": tipo, "data": m["data"], "preco": m["preco"], "fech_semana_ant": ant,
            "var_dia": m["var_dia"], "var_semana": (m["preco"] / ant - 1) if ant else None,
            "var_30d": m["var_30d"], "var_12m": m["var_12m"], "ret_total_12m": m["ret_total_12m"],
            "dy_12m": m["dy_12m"], "prov_12m": m["prov_12m"], "min52": m["min52"], "max52": m["max52"],
            "dist_mm200": m["dist_mm200"],
            "serie": [[r[0], round(r[1], 4)] for r in rows[-126:]],
            "ret_semanais": retornos_semanais(rows),
            "proventos_recentes": [{"data": dt, "valor": v, "yield": v / m["preco"] if m["preco"] else None}
                                   for dt, v in provs],
            "alertas": alertas_ativo(m, tipo, cfg),
        }
        if a:
            q, pm = a["quantidade"], a["preco_medio"]
            d.update({"setor": a["setor"], "quantidade": q, "preco_medio": pm, "peso_alvo": alvo_pct(a),
                      "valor": q * m["preco"], "investido": q * pm,
                      "resultado": (m["preco"] / pm - 1) if pm else None})
        return d

    ativos = [x for x in (item(a["ticker"], a) for a in cart["ativos"]) if x]
    obs = [x for x in (item(t.upper()) for t in cart["observacao"]) if x]
    bench = {}
    for t, nome in NOME_BENCH.items():
        b = item(t, tipo="INDICE")
        if b:
            b["nome"] = nome
            b["alertas"] = []
            bench[t] = b

    modo = "posicoes" if any(x["quantidade"] > 0 for x in ativos) else "pesos_alvo"
    total = sum(x["valor"] for x in ativos)
    peso = {x["ticker"]: (x["valor"] / total if modo == "posicoes" else x["peso_alvo"]) for x in ativos}
    for x in ativos:
        x["peso_atual"] = x["valor"] / total if total else 0

    cart_ret = []
    for k in range(len(semanas_ref) - 1):
        acc = den = 0.0
        for x in ativos:
            r, w = x["ret_semanais"][k], peso[x["ticker"]]
            if r is not None and w:
                acc += w * r
                den += w
        cart_ret.append(round(acc / den, 6) if den else None)

    semanas = []
    for i, k in enumerate(semanas_ref[1:]):
        semanas.append({
            "inicio": date.fromisocalendar(k[0], k[1], 1).isoformat(),
            "fim": fs_ibov[k][0],
            "carteira": cart_ret[i],
            "ibov": bench["IBOV"]["ret_semanais"][i] if "IBOV" in bench else None,
            "ifix": bench["XFIX11"]["ret_semanais"][i] if "XFIX11" in bench else None,
        })
    for x in ativos + obs + list(bench.values()):
        x["ret_semana_total"] = x["ret_semanais"][-1] if x["ret_semanais"] else None
        del x["ret_semanais"]

    # cotacoes resumidas das acoes/FIIs recomendados pelas corretoras que nao estao na carteira
    ja = {x["ticker"] for x in ativos + obs}
    cotacoes = {}
    for t in dict.fromkeys(tickers_recomendados()):
        if t in ja:
            continue
        x = item(t)
        if x:
            cotacoes[t] = {k: x[k] for k in ("tipo", "data", "preco", "var_dia", "var_semana", "var_30d", "var_12m",
                                              "dy_12m", "dist_mm200", "min52", "max52", "alertas")}

    pat_ant = sum(x["quantidade"] * x["fech_semana_ant"] for x in ativos if x["fech_semana_ant"])
    itens, total_atual, total_final, caixa = calcular_aporte(cart, con, cfg["aporte_mensal_padrao"], avisar=False)
    base = total_final - caixa
    aporte = {
        "valor": cfg["aporte_mensal_padrao"], "patrimonio_antes": total_atual, "patrimonio_depois": base,
        "sobra": caixa,
        "itens": [{
            "ticker": i["a"]["ticker"], "tipo": i["a"]["tipo"], "preco": i["preco"], "comprar": i["compra"],
            "total": i["compra"] * i["preco"],
            "peso_atual": i["atual"] / total_atual if total_atual else 0,
            "peso_alvo": i["alvo_pct"],
            "peso_depois": (i["atual"] + i["compra"] * i["preco"]) / base if base else 0,
            "obs": "; ".join(i["motivos"]),
        } for i in sorted(itens, key=lambda x: -x["compra"] * x["preco"])],
    }
    hist = con.execute("SELECT data, valor, investido FROM patrimonio ORDER BY data").fetchall()
    con.close()

    saida = {
        "gerado_em": datetime.now().isoformat(timespec="minutes"),
        "data_ref": ref,
        "semana": {"inicio": inicio_sem, "fim": ref},
        "modo_carteira": modo,
        "config": {"alvo_classes": alvo_cls, "aporte_mensal_padrao": cfg["aporte_mensal_padrao"],
                   "limite_acima_mm200": cfg["limite_acima_mm200"], "dy_minimo_fii": cfg["dy_minimo_fii"]},
        "resumo": {"patrimonio": total, "investido": sum(x["investido"] for x in ativos),
                   "patrimonio_semana_ant": pat_ant, "ret_semana_carteira": cart_ret[-1] if cart_ret else None},
        "ativos": ativos,
        "observacao": obs,
        "cotacoes": cotacoes,
        "benchmarks": bench,
        "semanas": semanas,
        "aporte": aporte,
        "patrimonio_hist": [{"data": d, "valor": v, "investido": i} for d, v, i in hist],
    }
    WEB.mkdir(exist_ok=True)
    arq = WEB / "dados.json"
    arq.write_text(json.dumps(saida, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"Dados do painel salvos em: {arq} ({arq.stat().st_size // 1024} KB)")


def main():
    p = argparse.ArgumentParser(description="Rastreador de Acoes e FIIs da B3")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("atualizar", help="baixa cotacoes e proventos")
    sub.add_parser("relatorio", help="mostra resumo e gera HTML")
    sub.add_parser("diario", help="atualizar + relatorio + exportar")
    sub.add_parser("exportar", help="gera web/dados.json para o painel web")
    ap = sub.add_parser("aporte", help="sugere a distribuicao do aporte mensal")
    ap.add_argument("--valor", type=float, help="valor do aporte (padrao: carteira.json)")
    cp = sub.add_parser("comprar", help="registra compra (QTD negativa = venda)")
    cp.add_argument("ticker")
    cp.add_argument("quantidade", type=int)
    cp.add_argument("preco", type=float)
    args = p.parse_args()
    if args.cmd == "atualizar":
        cmd_atualizar(args)
    elif args.cmd == "relatorio":
        cmd_relatorio(args)
    elif args.cmd == "diario":
        cmd_atualizar(args)
        cmd_relatorio(args)
        cmd_exportar(args)
    elif args.cmd == "exportar":
        cmd_exportar(args)
    elif args.cmd == "aporte":
        cmd_aporte(args)
    elif args.cmd == "comprar":
        cmd_comprar(args)


if __name__ == "__main__":
    main()
