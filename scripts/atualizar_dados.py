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

HTML_FILE = Path(__file__).parent.parent / "index.html"

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
                      timeout=60)
    r.raise_for_status()
    token = r.json()["id"]
    log(f"Autenticado no Metabase (token: {token[:8]}…)")
    return token


# Sessão HTTP global (reutiliza conexão TCP para reduzir latência)
_http_session: requests.Session | None = None

def get_http_session() -> requests.Session:
    global _http_session
    if _http_session is None:
        _http_session = requests.Session()
        _http_session.headers.update({"Connection": "keep-alive"})
        adapter = requests.adapters.HTTPAdapter(
            max_retries=0,
            pool_connections=1,
            pool_maxsize=4,
        )
        _http_session.mount("https://", adapter)
    return _http_session

def mb_query_csv(token: str, sql: str) -> list[dict]:
    """Executa SQL via endpoint CSV — sem limite de 2000 linhas do /api/dataset."""
    import csv, io
    payload = {
        "database": MB_DB,
        "type": "native",
        "native": {"query": sql},
        "middleware": {"js-int-to-string?": False},
    }
    sess = get_http_session()
    sess.headers["X-Metabase-Session"] = token
    r = sess.post(f"{MB_URL}/api/dataset/csv", json=payload, timeout=120)
    r.raise_for_status()
    reader = csv.DictReader(io.StringIO(r.text))
    return [row for row in reader]


def mb_query(token: str, sql: str, limit: int = 500, retries: int = 3) -> list[dict]:
    """Executa uma query SQL nativa com retry automático."""
    import time
    payload = {
        "database": MB_DB,
        "type": "native",
        "native": {"query": sql},
        "middleware": {"js-int-to-string?": False},
    }
    if limit > 2000:
        payload["constraints"] = {"max-results": limit}  # supera o limite padrão Metabase
    headers = {
        "X-Metabase-Session": token,
        "Content-Type": "application/json",
        "Connection": "keep-alive",
    }
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            if attempt > 1:
                wait = 15 * attempt
                log(f"  Tentativa {attempt}/{retries} — aguardando {wait}s...")
                time.sleep(wait)
            sess = get_http_session()
            sess.headers["X-Metabase-Session"] = token
            r = sess.post(
                f"{MB_URL}/api/dataset",
                json=payload,
                timeout=300,
            )
            r.raise_for_status()
            data = r.json()
            if "error" in data:
                raise RuntimeError(f"Metabase query error: {data['error']}")
            cols = [c["name"] for c in data["data"]["cols"]]
            rows = data["data"]["rows"][:limit]
            return [dict(zip(cols, row)) for row in rows]
        except (requests.exceptions.ConnectionError,
                requests.exceptions.ReadTimeout,
                requests.exceptions.ChunkedEncodingError) as e:
            last_err = e
            log(f"  ⚠ Conexão interrompida (tentativa {attempt}/{retries}): {type(e).__name__}")
            if attempt == retries:
                raise RuntimeError(f"Query falhou após {retries} tentativas: {last_err}") from e
    raise RuntimeError("Unreachable")


# ── SQLs ──────────────────────────────────────────────────────────────────────
SQL_DESEMPENHO_LOJAS = f"""
WITH fac AS (
  SELECT c.id cid,
    CASE f.name
      WHEN 'Shopping Iguatemi (BR)'      THEN 'Iguatemi'
      WHEN 'ParkShopping Brasília (BR)'  THEN 'PKS'
      ELSE 'Anália Franco' END loja
  FROM companies c JOIN factories f ON f.id=c.factory_id
  WHERE f.operation='totem' AND f.id IN(3,6,7)
),
base AS (
  SELECT o.id oid, o.aasm_state, o.created_at,
    (o.created_at AT TIME ZONE 'America/Sao_Paulo')::date dia,
    fc.loja
  FROM orders o JOIN fac fc ON fc.cid=o.company_id
  WHERE o.deleted_at IS NULL
    AND o.created_at >= '{ini}' AND o.created_at < '{fim}'
),
fp AS (
  SELECT DISTINCT ON(li.order_id) li.order_id,
    MIN(lipl.created_at) OVER(PARTITION BY li.order_id) t0
  FROM line_items li
  JOIN line_item_production_logs lipl ON lipl.line_item_id=li.id
  WHERE lipl.operation IN('production','surface') AND lipl.success=TRUE
    AND li.deleted_at IS NULL
    AND li.order_id IN (SELECT oid FROM base)
  ORDER BY li.order_id, lipl.created_at
),
rp AS (
  SELECT li.order_id, MAX(lipl.created_at) t1
  FROM line_items li
  JOIN line_item_production_logs lipl ON lipl.line_item_id=li.id
  WHERE lipl.operation IN('closing','picking','packing') AND lipl.success=TRUE
    AND li.deleted_at IS NULL
    AND li.order_id IN (SELECT oid FROM base)
  GROUP BY li.order_id
),
calc AS (
  SELECT b.dia, b.loja,
    CASE WHEN b.aasm_state='canceled' THEN 'cancelado'
         WHEN b.aasm_state='waiting' THEN 'aguardando'
         WHEN b.aasm_state='delivered' OR rp.t1 IS NOT NULL THEN 'pronto'
         ELSE 'em_producao' END status,
    CASE WHEN rp.t1 IS NOT NULL THEN
      GREATEST(0, EXTRACT(EPOCH FROM(rp.t1 - COALESCE(fp.t0, b.created_at)))/60.0)
    END pm
  FROM base b
  LEFT JOIN fp ON fp.order_id=b.oid
  LEFT JOIN rp ON rp.order_id=b.oid
)
SELECT dia::text, loja,
  COUNT(*) pedidos,
  COUNT(*) FILTER(WHERE status='pronto') prontos,
  COUNT(*) FILTER(WHERE status='em_producao') em_producao,
  COUNT(*) FILTER(WHERE status='cancelado') cancelados,
  COUNT(*) FILTER(WHERE status='aguardando') aguardando,
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
    FILTER(WHERE pm IS NOT NULL AND pm>=2 AND pm<1440)::numeric, 1) mediana_op,
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

SQL_MATERIAIS = """
SELECT m.id, mc.name AS categoria, m.reference
FROM materials m
JOIN material_categories mc ON mc.id = m.material_category_id
WHERE mc.name <> 'Ebook'
"""

SQL_CONSUMO_MES = """
SELECT
  b.factory_id,
  li.material_id,
  EXTRACT(MONTH FROM o.created_at AT TIME ZONE 'America/Sao_Paulo')::int AS mes,
  COUNT(li.id)                     AS qtd,
  ROUND(SUM(li.price)::numeric, 2) AS val
FROM orders o
JOIN batches  b  ON b.id  = o.batch_id  AND b.factory_id IN (3,6,7)
JOIN line_items li ON li.order_id = o.id AND li.deleted_at IS NULL AND li.price > 0
WHERE o.deleted_at IS NULL
  AND o.created_at >= '{ano}-{mes:02d}-01'
  AND o.created_at <  '{prox_mes}'
GROUP BY b.factory_id, li.material_id, mes
ORDER BY b.factory_id, val DESC
LIMIT 500
"""


SQL_VOL_MENSAL = """
SELECT
  b.factory_id,
  EXTRACT(MONTH FROM o.created_at AT TIME ZONE 'America/Sao_Paulo') AS mes,
  COUNT(li.id)                     AS volume,
  ROUND(SUM(li.price)::numeric, 2) AS receita
FROM orders o
JOIN batches  b  ON b.id  = o.batch_id       AND b.factory_id IN (3,6,7)
JOIN line_items li ON li.order_id = o.id     AND li.deleted_at IS NULL
                                             AND li.price > 0
JOIN materials m   ON m.id = li.material_id
JOIN material_categories mc ON mc.id = m.material_category_id
                           AND mc.name <> 'Ebook'
WHERE o.deleted_at IS NULL
  AND o.created_at >= '2026-01-01'
  AND o.created_at <  '{fim}'
GROUP BY b.factory_id, mes
ORDER BY b.factory_id, mes
"""

# ── Geradores de JS ───────────────────────────────────────────────────────────
def gerar_dp_raw(rows: list[dict]) -> str:
    """Gera o array JS let DP_RAW=[...]."""
    campos = ["dia","loja","pedidos","prontos","em_producao","cancelados","aguardando",
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
    Inclui todos os meses de Janeiro até o mês atual automaticamente.
    Retorna (an_js, dist_js).
    """
    # Incluir todos os meses de Jan até o mês atual (não só os com dados)
    mes_atual = hoje.month
    meses_disponiveis = list(range(1, mes_atual + 1))
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


def gerar_an_month_dates(hoje: datetime.date) -> str:
    """Gera AN_MONTH_DATES com todos os meses até o atual."""
    import calendar
    meses_disponiveis = list(range(1, hoje.month + 1))
    linhas = []
    for m in meses_disponiveis:
        start = f"01/{m:02d}"
        if m < hoje.month:
            # Mês completo
            ultimo_dia = calendar.monthrange(hoje.year, m)[1]
            end = f"{ultimo_dia:02d}/{m:02d}"
        else:
            # Mês atual: até hoje
            end = hoje.strftime("%d/%m")
        linhas.append(f"  {{start:'{start}', end:'{end}'}}")
    return "const AN_MONTH_DATES = [\n" + ",\n".join(linhas) + "\n];"

# ── Injeção no HTML ───────────────────────────────────────────────────────────

def gerar_skus_mes(rows: list[dict], hoje) -> str:
    """Gera const AN_SKUS_MES = [...] com os dados do mês atual."""
    import json
    linhas = []
    for r in rows:
        fid = int(r.get('factory_id', 0))
        cat = str(r.get('categoria', ''))
        var = str(r.get('variacao_cor', ''))
        mod = str(r.get('modelo', '') or '')
        qtd = int(r.get('qtd_mes', 0) or 0)
        val = float(r.get('val_mes', 0) or 0)
        cat_esc = cat.replace('"', '\\"')
        var_esc = var.replace('"', '\\"')
        mod_esc = mod.replace('"', '\\"')
        linhas.append(f'[{fid},"{cat_esc}","{var_esc}","{mod_esc}",{qtd},{round(val,2)}]')
    mes = hoje.month
    return f'const AN_SKUS_MES = {{mes:{mes},rows:[\n' + ',\n'.join(linhas) + '\n]}};'


def gerar_vol_mes(rows: list[dict], hoje) -> str:
    """Gera const AN_VOL_MES = {{mes:N, 3:[vol,rev], 6:[vol,rev], 7:[vol,rev]}}."""
    data = {3: [0, 0.0], 6: [0, 0.0], 7: [0, 0.0]}
    for r in rows:
        fid = int(r.get('factory_id', 0))
        if fid in data:
            data[fid] = [int(r.get('volume', 0) or 0), float(r.get('receita', 0) or 0)]
    mes = hoje.month
    parts = ', '.join(f'{fid}:{data[fid]}' for fid in [3, 6, 7])
    return f'const AN_VOL_MES = {{mes:{mes}, {parts}}};'

def substituir_bloco_dados(html: str, dp_raw: str, dp_opr: str, an: str, an_dist: str,
                           an_month_dates: str = "") -> str:
    """Substitui o bloco <script id='dados-metabase'> por completo.
    Se an/an_dist são None, preserva os dados de AN existentes no HTML."""
    START = '<script id="dados-metabase">'
    END   = '</script>'
    i_s = html.find(START)
    if i_s < 0:
        raise ValueError("Bloco <script id=\"dados-metabase\"> não encontrado no HTML!")
    i_e = html.find(END, i_s) + len(END)

    # Se AN não disponível, extrair do HTML existente
    an_final = an if an else _extrair_bloco_entre(html, '/* @@AN_START@@ */', '/* @@AN_END@@ */')
    an_dist_final = an_dist if an_dist else _extrair_bloco_entre(html, '/* @@AN_DIST_START@@ */', '/* @@AN_DIST_END@@ */')

    novo = (
        f'{START}\n'
        f'/* === DADOS METABASE — atualizado {datetime.date.today().strftime("%d/%m/%Y")} === */\n'
        f'{dp_raw}\n'
        f'{dp_opr}\n'
        + (f'{an_final}\n' if an_final else '')
        + (f'{an_dist_final}\n' if an_dist_final else '')
        + (f'{an_month_dates}\n' if an_month_dates else '')
                + f'{END}'
    )
    return html[:i_s] + novo + html[i_e:]


def _extrair_bloco_entre(html: str, inicio: str, fim: str) -> str:
    """Extrai o conteúdo entre dois marcadores no HTML."""
    i_s = html.find(inicio)
    i_e = html.find(fim, i_s)
    if i_s < 0 or i_e < 0:
        return ""
    return html[i_s:i_e + len(fim)]



def injetar_no_html(html: str,
                    dp_raw_js: str,
                    dp_opr_daily_js: str,
                    an_js: str,
                    an_dist_js: str,
                    hoje: datetime.date) -> str:
    # 1. Substituir bloco de dados
    an_month_dates_js = gerar_an_month_dates(hoje)
    html = substituir_bloco_dados(html, dp_raw_js, dp_opr_daily_js, an_js, an_dist_js, an_month_dates_js)
    log("  ↳ Bloco de dados substituído")

    # 2. Atualizar datas nos badges
    hoje_str = hoje.strftime("%d/%m/%Y")
    hoje_dm  = hoje.strftime("%d/%m")
    mes_nome = MESES_PT[hoje.month - 1]
    n_sub = 0

    def sub(pat, repl, s, **kw):
        return re.subn(pat, repl, s, **kw)

    html, k = re.subn(r'(?<=id="dp-last-update">)[^<]+(?=</div>)',
                      f'01/05 – {hoje_str} · atualizado {hoje_dm}', html); n_sub += k
    html, k = re.subn(r'(?<=id="an-filter-summary-range">)[^<]+(?=</div>)',
                      f'01/01/2026 – {hoje_str}', html); n_sub += k
    html, k = re.subn(r'(?<=id="dp-filter-label">)[^<]+(?=</div>)',
                      f'01/05 – {hoje_str}', html); n_sub += k
    html, k = re.subn(r'Atualizado em \d{2}/\d{2}/\d{4}',
                      f'Atualizado em {hoje_str}', html); n_sub += k
    html, k = re.subn(r'(?<=>)\d{2}/\d{2}/\d{4}(?=</div>)',
                      hoje_str, html); n_sub += k
    html, k = re.subn(r'01/01(/\d{4})? [–-] \d{2}/\d{2}/\d{4}',
                      f'01/01/2026 – {hoje_str}', html); n_sub += k
    html, k = re.subn(r'(01/\d{2}/\d{4} [–-] )\d{2}/\d{2}/\d{4}',
                      lambda m: m.group(1) + hoje_str, html); n_sub += k

    log(f"  ↳ {n_sub} datas atualizadas para {hoje_str}")

    # 3. Atualizar AN_MONTH_DATES — end do mês atual
    # Meses: Jan=0, Fev=1, ..., Dez=11
    mes_idx = hoje.month - 1
    html, k = re.subn(
        r"(\{start:'01/" + f"{hoje.month:02d}" + r"',\s*end:')\d{2}/" + f"{hoje.month:02d}" + r"('\}[^,\n]*)",
        lambda m: m.group(1) + hoje_dm + "'} /* ← dados até " + hoje_dm + " */",
        html
    )
    if k:
        log(f"  ↳ AN_MONTH_DATES mês {hoje.month} atualizado para {hoje_dm}")

    # Atualizar texto 'dados até DD/MM' no anUpdateSummary
    html = re.sub(r'dados até \d{2}/\d{2} —', f'dados até {hoje_dm} —', html)

    # ── Top Consumo: badge ATUALIZADO e botão Mai ─────────────────────────
    html, k = re.subn(
        r'(?<=>)\d{2}/\d{2}/\d{4}(?=</div>)',
        hoje_str, html)
    html = re.sub(
        r'title="01/05[–-]\d{2}/\d{2}/\d{4}"',
        f'title="01/05–{hoje_str}"', html)

    # ── Desempenho: inputs de data e botões de período ────────────────────
    mes_ini = hoje.replace(day=1)
    mes_ini_iso = mes_ini.strftime('%Y-%m-%d')
    hoje_iso    = hoje.strftime('%Y-%m-%d')
    sem1_fim    = hoje.replace(day=7).strftime('%Y-%m-%d')
    sem2_ini    = hoje.replace(day=8).strftime('%Y-%m-%d')
    sem2_fim    = hoje.replace(day=14).strftime('%Y-%m-%d')

    # Input date fim (value e max)
    html = re.sub(
        r'(id="dp-data-fim" value=")2026-\d{2}-\d{2}(" min="2026-\d{2}-\d{2}" max=")2026-\d{2}-\d{2}"',
        lambda m: m.group(1) + hoje_iso + m.group(2) + hoje_iso + '"', html)

    # Input date ini max
    html = re.sub(
        r'(id="dp-data-ini"[^>]*max=")\d{4}-\d{2}-\d{2}"',
        lambda m: m.group(1) + hoje_iso + '"', html)

    # Botões período no Desempenho
    html = re.sub(r"dpSetPeriod\('\d{4}-\d{2}-01','\d{4}-\d{2}-07'\)",
                  f"dpSetPeriod('{mes_ini_iso}','{sem1_fim}')", html)
    html = re.sub(r"dpSetPeriod\('\d{4}-\d{2}-08','\d{4}-\d{2}-14'\)",
                  f"dpSetPeriod('{sem2_ini}','{sem2_fim}')", html)
    html = re.sub(r"dpSetPeriod\('\d{4}-\d{2}-01','\d{4}-\d{2}-\d{2}'\)",
                  f"dpSetPeriod('{mes_ini_iso}','{hoje_iso}')", html)

    log(f"  ↳ Datas TC e DP atualizadas para {hoje_str}")
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


    # ── Consumo: 2 queries leves + join em Python ────────────────────────
    # Query 1: catálogo de materiais (rápida, tabela pequena)
    log("Consultando catálogo de materiais...")
    try:
        mat_rows = mb_query_csv(token, SQL_MATERIAIS)
        mat_map = {}
        for r in mat_rows:
            ref = (r.get('reference') or '').split(' / ')
            try:
                mat_map[int(r['id'])] = {
                    'categoria': r['categoria'],
                    'variacao_cor': ref[0].strip() if ref else '',
                    'modelo': ref[1].strip() if len(ref) > 1 else '',
                }
            except (ValueError, KeyError):
                pass
        log(f"  ↳ {len(mat_map)} materiais")
    except Exception as e:
        log(f"  ⚠ Catálogo falhou: {e}")
        mat_map = {}

    # Query 2: consumo por material_id por mês (sem JOIN material_categories)
    log("Consultando Consumo por SKU (mês a mês)...")
    consumo_acc = {}  # (factory_id, material_id) → {qtd, val}
    ano = hoje.year
    meses = list(range(1, hoje.month + 1))
    vol_rows_list = []  # para montar AN.vol e AN.rev

    for mes in meses:
        prox = f"{ano}-{mes+1:02d}-01" if mes < 12 else f"{ano+1}-01-01"
        if mes == hoje.month:
            prox = fim
        sql_m = SQL_CONSUMO_MES.format(ano=ano, mes=mes, prox_mes=prox)
        try:
            rows_m = mb_query(token, sql_m, limit=500, retries=2)
            for r in rows_m:
                key = (int(r['factory_id']), int(r['material_id']))
                if key not in consumo_acc:
                    consumo_acc[key] = {'qtd': 0, 'val': 0.0}
                consumo_acc[key]['qtd'] += int(r['qtd'] or 0)
                consumo_acc[key]['val'] += float(r['val'] or 0)
            # Agregar volume por fábrica para vol_rows
            for fid in [3, 6, 7]:
                fid_rows = [r for r in rows_m if int(r['factory_id']) == fid]
                if fid_rows:
                    vol_rows_list.append({
                        'factory_id': fid,
                        'mes': mes,
                        'volume': sum(int(r['qtd'] or 0) for r in fid_rows),
                        'receita': round(sum(float(r['val'] or 0) for r in fid_rows), 2),
                    })
            log(f"  ↳ Mês {mes:02d}: {len(rows_m)} linhas")
        except Exception as e:
            log(f"  ⚠ Mês {mes:02d} falhou: {e}")

    # Montar skus_rows (join consumo_acc × mat_map)
    skus_rows = []
    for (fid, mid), totais in consumo_acc.items():
        mat = mat_map.get(mid)
        if mat:
            skus_rows.append({
                'factory_id': fid,
                'categoria':    mat['categoria'],
                'variacao_cor': mat['variacao_cor'],
                'modelo':       mat['modelo'],
                'qtd_total':    totais['qtd'],
                'val_total':    round(totais['val'], 2),
            })
    # Debug: verificar correspondência de IDs
    if consumo_acc and not skus_rows:
        sample_ids = list(consumo_acc.keys())[:5]
        mat_keys = sorted(mat_map.keys())[:5] if mat_map else []
        log(f"  DEBUG consumo_acc sample: {sample_ids}")
        log(f"  DEBUG mat_map primeiras chaves: {mat_keys}")
        log(f"  DEBUG mat_map total: {len(mat_map)}")
        # Tentar encontrar um match manual
        for (fid, mid), _ in list(consumo_acc.items())[:3]:
            log(f"  DEBUG mid={mid} type={type(mid).__name__} in mat_map={mid in mat_map}")
    log(f"  ↳ {len(skus_rows)} SKUs após merge")

    vol_rows = vol_rows_list if vol_rows_list else None
    log(f"  ↳ Volume mensal: {len(vol_rows_list)} entradas")


    # Gerar JS
    log("Gerando blocos JS...")
    dp_raw_js       = gerar_dp_raw(dp_raw_rows)
    dp_opr_daily_js = gerar_dp_opr_daily(dp_opr_rows)

    # Consumo: usar dados do loop mês-a-mês
    if vol_rows is not None and skus_rows:
        log(f"  ↳ Atualizando AN: {len(skus_rows)} SKUs, {len(vol_rows)} entradas mensais")
        an_js, an_dist_js = gerar_an(skus_rows, vol_rows)
        an_month_dates_js = gerar_an_month_dates(hoje)
    else:
        log("  ↳ AN: mantendo dados existentes no HTML")
        an_js, an_dist_js, an_month_dates_js = None, None, None
    
    # Injetar no HTML
    log("Injetando no HTML...")
    html = HTML_FILE.read_text(encoding="utf-8")
    html = injetar_no_html(html, dp_raw_js, dp_opr_daily_js, an_js, an_dist_js, hoje)
    HTML_FILE.write_text(html, encoding="utf-8")

    # Verificar quais blocos mudaram
    with open(HTML_FILE, 'r', encoding='utf-8') as fh:
        html_depois = fh.read()

    import subprocess
    result = subprocess.run(['git', 'diff', '--stat', 'requisicao-lojas.html'],
                           capture_output=True, text=True, cwd=HTML_FILE.parent)
    log(f"Git diff: {result.stdout.strip() or 'sem mudanças detectadas'}")

    # Confirmar datas no novo HTML
    import re as _re
    datas = _re.findall(r"dia:'(2026-\d{2}-\d{2})'", html_depois)
    if datas:
        log(f"Dados Desempenho: {min(datas)} a {max(datas)} ({len(datas)} entradas)")
    else:
        log("⚠ Nenhum dado de desempenho encontrado no HTML após atualização")

    skus = len(_re.findall(r'\[\d+,"', html_depois))
    log(f"SKUs: {skus} encontrados no HTML")

    log(f"✅ {HTML_FILE.name} atualizado com dados de {ini} a {hoje.strftime('%d/%m/%Y')}")

if __name__ == "__main__":
    main()
