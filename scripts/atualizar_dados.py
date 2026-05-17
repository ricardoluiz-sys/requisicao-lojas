#!/usr/bin/env python3
"""
atualizar_dados.py
==================
Consulta o Metabase, gera os arrays JS atualizados e injeta no HTML.

Uso local:
    MB_USER=seu@email MB_PASS=suasenha python scripts/atualizar_dados.py

Uso no GitHub Actions:
    Segredos necessários: MB_USER, MB_PASS
    (opcionais) MB_URL  → padrão: https://metabase.gocase.com.br
                MB_DB   → padrão: 3
                DIAS    → padrão: dias do mês atual até hoje
"""

import os, sys, json, re, datetime, requests
from pathlib import Path

# ── Configuração ──────────────────────────────────────────────────────────────
MB_URL  = os.getenv("MB_URL",  "https://metabase.gocase.com.br")
MB_DB   = int(os.getenv("MB_DB",   "3"))
MB_USER = os.getenv("MB_USER")
MB_PASS = os.getenv("MB_PASS")

HTML_FILE = Path(__file__).parent.parent / "requisicao-lojas.html"

# Período: 1º do mês atual até hoje
hoje  = datetime.date.today()
ini   = hoje.replace(day=1).isoformat()
fim   = (hoje + datetime.timedelta(days=1)).isoformat()   # exclusive upper bound
mes_label = hoje.strftime("%b/%Y")                        # ex: "Mai/2026"

MESES_PT = ["Jan","Fev","Mar","Abr","Mai","Jun","Jul","Ago","Set","Out","Nov","Dez"]

# ── Helpers ───────────────────────────────────────────────────────────────────
def log(msg: str):
    print(f"[{datetime.datetime.now():%H:%M:%S}] {msg}", flush=True)

def mb_session(user: str, senha: str) -> str:
    """Autentica no Metabase e retorna o session token."""
    r = requests.post(f"{MB_URL}/api/session",
                      json={"username": user, "password": senha},
                      timeout=30)
    r.raise_for_status()
    token = r.json()["id"]
    log(f"Autenticado no Metabase (token: {token[:8]}…)")
    return token

def mb_query(token: str, sql: str, limit: int = 500) -> list[dict]:
    """Executa uma query SQL nativa e retorna lista de dicts."""
    payload = {
        "database": MB_DB,
        "type": "native",
        "native": {"query": sql},
        "middleware": {"js-int-to-string?": False}
    }
    r = requests.post(
        f"{MB_URL}/api/dataset",
        headers={"X-Metabase-Session": token, "Content-Type": "application/json"},
        json=payload,
        timeout=120
    )
    r.raise_for_status()
    data = r.json()

    if "error" in data:
        raise RuntimeError(f"Metabase query error: {data['error']}")

    cols = [c["name"] for c in data["data"]["cols"]]
    rows = data["data"]["rows"][:limit]
    return [dict(zip(cols, row)) for row in rows]

# ── SQLs ──────────────────────────────────────────────────────────────────────
SQL_DESEMPENHO_LOJAS = f"""
WITH base AS (
  SELECT o.id oid, o.aasm_state, o.created_at,
    (o.created_at AT TIME ZONE 'America/Sao_Paulo')::date dia,
    CASE f.name
      WHEN 'Shopping Iguatemi (BR)'      THEN 'Iguatemi'
      WHEN 'ParkShopping Brasília (BR)'  THEN 'PKS'
      ELSE 'Anália Franco' END loja
  FROM orders o
  JOIN companies c ON c.id=o.company_id
  JOIN factories f ON f.id=c.factory_id
  WHERE f.operation='totem' AND f.id IN(3,6,7) AND o.deleted_at IS NULL
    AND o.created_at >= '{ini}' AND o.created_at < '{fim}'
),
fp AS (
  SELECT DISTINCT ON(li.order_id) li.order_id,
    MIN(lipl.created_at) OVER(PARTITION BY li.order_id) t0
  FROM line_items li JOIN base b ON b.oid=li.order_id
  JOIN line_item_production_logs lipl ON lipl.line_item_id=li.id
  WHERE lipl.operation IN('production','surface') AND lipl.success=TRUE AND li.deleted_at IS NULL
  ORDER BY li.order_id, lipl.created_at
),
rp AS (
  SELECT li.order_id, MAX(lipl.created_at) t1
  FROM line_items li JOIN base b ON b.oid=li.order_id
  JOIN line_item_production_logs lipl ON lipl.line_item_id=li.id
  WHERE lipl.operation IN('closing','picking','packing') AND lipl.success=TRUE AND li.deleted_at IS NULL
  GROUP BY li.order_id
),
calc AS (
  SELECT b.dia, b.loja,
    CASE WHEN b.aasm_state='canceled' THEN 'cancelado'
         WHEN b.aasm_state='delivered' OR rp.t1 IS NOT NULL THEN 'pronto'
         ELSE 'em_producao' END status,
    CASE WHEN rp.t1 IS NOT NULL THEN
      GREATEST(0, EXTRACT(EPOCH FROM(rp.t1 - COALESCE(fp.t0, b.created_at)))/60.0)
    END pm
  FROM base b LEFT JOIN fp ON fp.order_id=b.oid LEFT JOIN rp ON rp.order_id=b.oid
)
SELECT dia::text, loja,
  COUNT(*) pedidos,
  COUNT(*) FILTER(WHERE status='pronto') prontos,
  COUNT(*) FILTER(WHERE status='em_producao') em_producao,
  COUNT(*) FILTER(WHERE status='cancelado') cancelados,
  COUNT(*) FILTER(WHERE pm IS NOT NULL AND pm<=30) d30,
  COUNT(*) FILTER(WHERE pm IS NOT NULL AND pm<=120) d120,
  COUNT(*) FILTER(WHERE pm IS NOT NULL AND pm>120) acima120,
  COUNT(*) FILTER(WHERE pm IS NOT NULL AND pm BETWEEN 0 AND 15) f0_15,
  COUNT(*) FILTER(WHERE pm IS NOT NULL AND pm>15 AND pm<=30) f15_30,
  COUNT(*) FILTER(WHERE pm IS NOT NULL AND pm>30 AND pm<=60) f30_60,
  COUNT(*) FILTER(WHERE pm IS NOT NULL AND pm>60 AND pm<=120) f60_120,
  COUNT(*) FILTER(WHERE pm IS NOT NULL AND pm>120 AND pm<=240) f120_240,
  COUNT(*) FILTER(WHERE pm IS NOT NULL AND pm>240) f240_mais,
  ROUND(PERCENTILE_CONT(0.5) WITHIN GROUP(ORDER BY pm)
    FILTER(WHERE pm IS NOT NULL AND pm>=2)::numeric, 1) mediana_op,
  ROUND(PERCENTILE_CONT(0.5) WITHIN GROUP(ORDER BY pm)::numeric, 1) mediana_total
FROM calc
GROUP BY dia, loja
ORDER BY dia, loja
"""

SQL_DESEMPENHO_OPR = f"""
WITH base AS (
  SELECT o.id oid, o.aasm_state, o.created_at,
    (o.created_at AT TIME ZONE 'America/Sao_Paulo')::date dia,
    CASE f.name
      WHEN 'Shopping Iguatemi (BR)'      THEN 'Iguatemi'
      WHEN 'ParkShopping Brasília (BR)'  THEN 'PKS'
      ELSE 'Anália Franco' END loja
  FROM orders o
  JOIN companies c ON c.id=o.company_id
  JOIN factories f ON f.id=c.factory_id
  WHERE f.operation='totem' AND f.id IN(3,6,7) AND o.deleted_at IS NULL
    AND o.created_at >= '{ini}' AND o.created_at < '{fim}'
),
fp AS (
  SELECT DISTINCT ON(li.order_id) li.order_id,
    MIN(lipl.created_at) OVER(PARTITION BY li.order_id) t0,
    lipl.user_id uid
  FROM line_items li JOIN base b ON b.oid=li.order_id
  JOIN line_item_production_logs lipl ON lipl.line_item_id=li.id
  WHERE lipl.operation IN('production','surface') AND lipl.success=TRUE AND li.deleted_at IS NULL
  ORDER BY li.order_id, lipl.created_at
),
rp AS (
  SELECT li.order_id, MAX(lipl.created_at) t1
  FROM line_items li JOIN base b ON b.oid=li.order_id
  JOIN line_item_production_logs lipl ON lipl.line_item_id=li.id
  WHERE lipl.operation IN('closing','picking','packing') AND lipl.success=TRUE AND li.deleted_at IS NULL
  GROUP BY li.order_id
)
SELECT b.dia::text, b.loja,
  COALESCE(fp.uid::text, '0') uid,
  COALESCE(u.name, 'Sistema') nome,
  COUNT(*) pedidos,
  COUNT(*) FILTER(WHERE b.aasm_state='delivered' OR rp.t1 IS NOT NULL) prontos,
  COUNT(*) FILTER(WHERE rp.t1 IS NULL AND b.aasm_state NOT IN('canceled','delivered')) em_producao,
  COUNT(*) FILTER(WHERE rp.t1 IS NOT NULL AND
    GREATEST(0,EXTRACT(EPOCH FROM(rp.t1-COALESCE(fp.t0,b.created_at)))/60.0)<=30) d30,
  COUNT(*) FILTER(WHERE rp.t1 IS NOT NULL AND
    GREATEST(0,EXTRACT(EPOCH FROM(rp.t1-COALESCE(fp.t0,b.created_at)))/60.0)<=120) d120,
  COUNT(*) FILTER(WHERE rp.t1 IS NOT NULL AND
    GREATEST(0,EXTRACT(EPOCH FROM(rp.t1-COALESCE(fp.t0,b.created_at)))/60.0)>120) acima120,
  ROUND(PERCENTILE_CONT(0.5) WITHIN GROUP(
    ORDER BY GREATEST(0,EXTRACT(EPOCH FROM(rp.t1-COALESCE(fp.t0,b.created_at)))/60.0)
  )::numeric, 1) mediana
FROM base b
LEFT JOIN fp ON fp.order_id=b.oid
LEFT JOIN rp ON rp.order_id=b.oid
LEFT JOIN users u ON u.id=fp.uid
GROUP BY b.dia, b.loja, fp.uid, u.name
ORDER BY b.dia, b.loja, pedidos DESC
"""

SQL_CONSUMO = """
SELECT
  b.factory_id,
  mc.name AS categoria,
  SPLIT_PART(m.reference, ' / ', 1) AS variacao_cor,
  SPLIT_PART(m.reference, ' / ', 2) AS modelo,
  COUNT(li.id) AS qtd_total,
  ROUND(SUM(li.price)::numeric, 2) AS val_total
FROM line_items li
JOIN orders o ON o.id = li.order_id AND o.deleted_at IS NULL
JOIN batches b ON b.id = o.batch_id AND b.factory_id IN (3,6,7)
JOIN materials m ON m.id = li.material_id
JOIN material_categories mc ON mc.id = m.material_category_id
WHERE li.created_at >= '2026-01-01'
  AND li.deleted_at IS NULL
  AND mc.name != 'Ebook'
  AND li.price > 0
GROUP BY b.factory_id, mc.name, variacao_cor, modelo
ORDER BY b.factory_id, val_total DESC
"""

SQL_VOL_MENSAL = f"""
SELECT
  b.factory_id,
  EXTRACT(MONTH FROM li.created_at AT TIME ZONE 'America/Sao_Paulo') AS mes,
  COUNT(li.id) AS volume,
  ROUND(SUM(li.price)::numeric, 2) AS receita
FROM line_items li
JOIN orders o ON o.id = li.order_id AND o.deleted_at IS NULL
JOIN batches b ON b.id = o.batch_id AND b.factory_id IN (3,6,7)
JOIN material_categories mc ON mc.id = (
  SELECT material_category_id FROM materials WHERE id = li.material_id LIMIT 1
)
WHERE li.created_at >= '2026-01-01'
  AND li.deleted_at IS NULL
  AND mc.name != 'Ebook'
  AND li.price > 0
GROUP BY b.factory_id, mes
ORDER BY b.factory_id, mes
"""

# ── Geradores de JS ───────────────────────────────────────────────────────────
def gerar_dp_raw(rows: list[dict]) -> str:
    """Gera o array JS let DP_RAW=[...]."""
    campos = ["dia","loja","pedidos","prontos","em_producao","cancelados",
              "d30","d120","acima120","f0_15","f15_30","f30_60",
              "f60_120","f120_240","f240_mais","mediana_op","mediana_total"]
    linhas = []
    for r in rows:
        vals = []
        for c in campos:
            v = r.get(c)
            if v is None:
                vals.append("null")
            elif c in ("dia", "loja"):
                vals.append(f"'{v}'")
            else:
                vals.append(str(v) if v is not None else "0")
        linhas.append("{" + ",".join(f"{c}:{v}" for c,v in zip(campos, vals)) + "}")
    return "let DP_RAW=[\n" + ",\n".join(linhas) + "\n];"

def gerar_dp_opr_daily(rows: list[dict]) -> str:
    """Gera o array JS const DP_OPR_DAILY=[...]."""
    campos = ["dia","loja","uid","nome","pedidos","prontos","em_producao",
              "d30","d120","acima120","mediana"]
    linhas = []
    for r in rows:
        vals = []
        for c in campos:
            v = r.get(c)
            if v is None:
                vals.append("null")
            elif c in ("dia","loja","nome"):
                s = str(v).replace("'", "\\'")
                vals.append(f"'{s}'")
            elif c == "uid":
                vals.append(str(v) if v else "0")
            else:
                vals.append(str(v) if v is not None else "0")
        linhas.append("{" + ",".join(f"{c}:{v}" for c,v in zip(campos, vals)) + "}")
    return "const DP_OPR_DAILY=[\n" + ",\n".join(linhas) + "\n];"

def gerar_an(skus_rows: list[dict], vol_rows: list[dict]) -> tuple[str, str]:
    """
    Gera const AN={...} e const AN_MONTHLY_DIST={...}.
    Retorna (an_js, dist_js).
    """
    # Determinar meses disponíveis (1-indexed)
    meses_disponiveis = sorted({int(r["mes"]) for r in vol_rows})
    meses_labels = [MESES_PT[m-1] for m in meses_disponiveis]

    # Construir vol e rev por fábrica por mês
    vol: dict[int, list] = {3:[], 6:[], 7:[]}
    rev: dict[int, list] = {3:[], 6:[], 7:[]}
    for fid in [3,6,7]:
        for m in meses_disponiveis:
            row = next((r for r in vol_rows if int(r["factory_id"])==fid and int(r["mes"])==m), None)
            vol[fid].append(int(row["volume"]) if row else 0)
            rev[fid].append(float(row["receita"]) if row else 0.0)

    # Construir skus: top 200 F3 + 100 F6 + 100 F7
    skus_f3 = [r for r in skus_rows if int(r["factory_id"])==3][:200]
    skus_f6 = [r for r in skus_rows if int(r["factory_id"])==6][:100]
    skus_f7 = [r for r in skus_rows if int(r["factory_id"])==7][:100]
    all_skus = skus_f3 + skus_f6 + skus_f7

    sku_lines = []
    for r in all_skus:
        fid = int(r["factory_id"])
        cat = str(r["categoria"]).replace("'", "\\'")
        cor = str(r["variacao_cor"]).replace("'", "\\'")
        mod = str(r["modelo"]).replace("'", "\\'") if r["modelo"] else ""
        qtd = int(r["qtd_total"])
        val = round(float(r["val_total"]), 2)
        sku_lines.append(f"[{fid},'{cat}','{cor}','{mod}',{qtd},{val}]")

    vol_js = "vol:{" + ",".join(f"{f}:{json.dumps(vol[f])}" for f in [3,6,7]) + "}"
    rev_js = "rev:{" + ",".join(f"{f}:{json.dumps(rev[f])}" for f in [3,6,7]) + "}"

    an_js = (
        "const AN = {\n"
        f"  months:{json.dumps(meses_labels)},\n"
        f"  {vol_js},\n"
        f"  {rev_js},\n"
        "  skus:[\n"
        + ",\n".join(sku_lines) + "\n"
        "]\n};"
    )

    # AN_MONTHLY_DIST
    totais = {f: sum(vol[f]) for f in [3,6,7]}
    dist_linhas = []
    for f in [3,6,7]:
        t = totais[f] or 1
        vals = ",".join(f"{v}/{t}" for v in vol[f])
        dist_linhas.append(f"  {f}:[{vals}]")
    dist_js = "const AN_MONTHLY_DIST = {\n" + ",\n".join(dist_linhas) + "\n};"

    return an_js, dist_js

# ── Injeção no HTML ───────────────────────────────────────────────────────────
def substituir_bloco(html: str, start_pat: str, end_pat: str, novo: str) -> str:
    """
    Substitui tudo entre start_pat (inclusive) e a próxima linha que casa end_pat (inclusive).
    """
    lines = html.split("\n")
    start_idx = next((i for i,l in enumerate(lines) if re.match(start_pat, l)), None)
    if start_idx is None:
        raise ValueError(f"Padrão de início não encontrado: {start_pat!r}")
    end_idx = next((i for i in range(start_idx+1, len(lines)) if re.match(end_pat, lines[i])), None)
    if end_idx is None:
        raise ValueError(f"Padrão de fim não encontrado: {end_pat!r}")
    new_lines = lines[:start_idx] + [novo] + lines[end_idx+1:]
    return "\n".join(new_lines)

def injetar_no_html(html: str,
                    dp_raw_js: str,
                    dp_opr_daily_js: str,
                    an_js: str,
                    an_dist_js: str,
                    hoje: datetime.date) -> str:
    # 1. DP_RAW
    html = substituir_bloco(html,
        r'^let DP_RAW=\[',
        r'^\];',
        dp_raw_js)
    log("  ↳ DP_RAW substituído")

    # 2. DP_OPR_DAILY
    html = substituir_bloco(html,
        r'^const DP_OPR_DAILY=\[',
        r'^\];',
        dp_opr_daily_js)
    log("  ↳ DP_OPR_DAILY substituído")

    # 3. AN object
    html = substituir_bloco(html,
        r'^const AN = \{',
        r'^\};',
        an_js)
    log("  ↳ AN substituído")

    # 4. AN_MONTHLY_DIST
    html = substituir_bloco(html,
        r'^const AN_MONTHLY_DIST = \{',
        r'^\};',
        an_dist_js)
    log("  ↳ AN_MONTHLY_DIST substituído")

    # 5. Atualizar badge de data no comentário do bloco de desempenho
    mes_nome = MESES_PT[hoje.month - 1]
    dia_str  = hoje.strftime("%d/%m/%Y")
    html = re.sub(
        r'DESEMPENHO DE PRODUÇÃO[^*]*Mai 2026',
        f'DESEMPENHO DE PRODUÇÃO — Metabase — {mes_nome} {hoje.year}',
        html
    )
    # 6. Badge "atualizado" no dashboard
    html = re.sub(
        r'atualizado \d{2}/\d{2}',
        f'atualizado {dia_str[:5]}',
        html
    )

    return html

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    if not MB_USER or not MB_PASS:
        print("ERRO: defina as variáveis de ambiente MB_USER e MB_PASS", file=sys.stderr)
        sys.exit(1)

    if not HTML_FILE.exists():
        print(f"ERRO: arquivo não encontrado: {HTML_FILE}", file=sys.stderr)
        sys.exit(1)

    log(f"Período: {ini} → {fim}")
    log(f"HTML: {HTML_FILE}")

    # Autenticar
    token = mb_session(MB_USER, MB_PASS)

    # Queries
    log("Consultando Desempenho por loja...")
    dp_raw_rows = mb_query(token, SQL_DESEMPENHO_LOJAS, limit=500)
    log(f"  ↳ {len(dp_raw_rows)} linhas")

    log("Consultando Desempenho por operador...")
    dp_opr_rows = mb_query(token, SQL_DESEMPENHO_OPR, limit=500)
    log(f"  ↳ {len(dp_opr_rows)} linhas")

    log("Consultando Consumo por SKU (Jan–hoje)...")
    skus_rows = mb_query(token, SQL_CONSUMO, limit=500)
    log(f"  ↳ {len(skus_rows)} linhas")

    log("Consultando Volume mensal por fábrica...")
    vol_rows = mb_query(token, SQL_VOL_MENSAL, limit=60)
    log(f"  ↳ {len(vol_rows)} linhas")

    # Gerar JS
    log("Gerando blocos JS...")
    dp_raw_js      = gerar_dp_raw(dp_raw_rows)
    dp_opr_daily_js = gerar_dp_opr_daily(dp_opr_rows)
    an_js, an_dist_js = gerar_an(skus_rows, vol_rows)

    # Injetar no HTML
    log("Injetando no HTML...")
    html = HTML_FILE.read_text(encoding="utf-8")
    html = injetar_no_html(html, dp_raw_js, dp_opr_daily_js, an_js, an_dist_js, hoje)
    HTML_FILE.write_text(html, encoding="utf-8")

    log(f"✅ {HTML_FILE.name} atualizado com dados de {ini} a {hoje.strftime('%d/%m/%Y')}")

if __name__ == "__main__":
    main()
