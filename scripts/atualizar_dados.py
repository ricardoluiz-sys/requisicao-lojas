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

AN_CACHE_FILE = Path(__file__).parent.parent / 'an_cache.js'

# Períodos
hoje  = datetime.date.today()
fim   = (hoje + datetime.timedelta(days=1)).isoformat()   # exclusive upper bound
mes_label = hoje.strftime("%b/%Y")
# Consumo: sempre Jan→hoje (para AN.skus ter histórico completo)
ini   = datetime.date(hoje.year, 1, 1).isoformat()
# Desempenho: mês atual; com fallback para mês anterior se for início de mês
_primeiro_mes      = hoje.replace(day=1)
_primeiro_anterior = (_primeiro_mes - datetime.timedelta(days=1)).replace(day=1)
# Nos primeiros 7 dias do mês, incluir o mês anterior para ter histórico suficiente
ini_dp = _primeiro_anterior.isoformat() if hoje.day <= 7 else _primeiro_mes.isoformat()
ini_dp_fallback = _primeiro_anterior.isoformat()  # fallback se ini_dp retornar < 5 linhas

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
SQL_DESEMPENHO_LOJAS = """
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
    o.delivered_at,
    (o.created_at AT TIME ZONE 'America/Sao_Paulo')::date dia,
    fc.loja
  FROM orders o JOIN fac fc ON fc.cid=o.company_id
  WHERE o.deleted_at IS NULL
    AND o.created_at >= '{ini_dp}' AND o.created_at < '{fim}'
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
  SELECT b.dia, b.loja, b.created_at,
    CASE WHEN b.aasm_state='canceled' THEN 'cancelado'
         WHEN b.aasm_state='waiting' THEN 'aguardando'
         WHEN b.aasm_state='delivered' OR rp.t1 IS NOT NULL THEN 'pronto'
         ELSE 'em_producao' END status,
    -- Via log de produção (tempo interno)
    CASE WHEN rp.t1 IS NOT NULL THEN
      GREATEST(0, EXTRACT(EPOCH FROM(rp.t1 - COALESCE(fp.t0, b.created_at)))/60.0)
    END pm,
    -- Via delivered_at (experiência do cliente)
    CASE WHEN b.delivered_at IS NOT NULL THEN
      GREATEST(0, EXTRACT(EPOCH FROM(b.delivered_at - b.created_at))/60.0)
    END pm_del
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
  ROUND(PERCENTILE_CONT(0.5) WITHIN GROUP(ORDER BY pm)::numeric, 1) mediana_total,
  -- Métricas via delivered_at (experiência do cliente)
  COUNT(*) FILTER(WHERE pm_del IS NOT NULL AND pm_del<=30) d30_del,
  COUNT(*) FILTER(WHERE pm_del IS NOT NULL AND pm_del<=120) d120_del,
  COUNT(*) FILTER(WHERE pm_del IS NOT NULL AND pm_del>120) acima120_del,
  COUNT(*) FILTER(WHERE pm_del IS NOT NULL AND pm_del BETWEEN 0 AND 15) f0_15_del,
  COUNT(*) FILTER(WHERE pm_del IS NOT NULL AND pm_del>15 AND pm_del<=30) f15_30_del,
  COUNT(*) FILTER(WHERE pm_del IS NOT NULL AND pm_del>30 AND pm_del<=60) f30_60_del,
  COUNT(*) FILTER(WHERE pm_del IS NOT NULL AND pm_del>60 AND pm_del<=120) f60_120_del,
  COUNT(*) FILTER(WHERE pm_del IS NOT NULL AND pm_del>120 AND pm_del<=240) f120_240_del,
  COUNT(*) FILTER(WHERE pm_del IS NOT NULL AND pm_del>240) f240_mais_del,
  ROUND(PERCENTILE_CONT(0.5) WITHIN GROUP(ORDER BY pm_del)
    FILTER(WHERE pm_del IS NOT NULL AND pm_del>=2 AND pm_del<1440)::numeric, 1) mediana_del
FROM calc
GROUP BY dia, loja
ORDER BY dia, loja
"""

SQL_DESEMPENHO_OPR = """
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
    AND o.created_at >= '{ini_dp}' AND o.created_at < '{fim}'
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
  AND m.id > {min_id}
ORDER BY m.id
LIMIT 2000
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
  AND o.created_at >= '{ini}'
  AND o.created_at <  '{fim}'
GROUP BY b.factory_id, li.material_id, mes
ORDER BY b.factory_id, val DESC
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
              "f60_120","f120_240","f240_mais","mediana_op","mediana_total",
              "d30_del","d120_del","acima120_del",
              "f0_15_del","f15_30_del","f30_60_del","f60_120_del","f120_240_del","f240_mais_del",
              "mediana_del"]
    linhas = []
    for r in rows:
        vals = []
        for c in campos:
            v = r.get(c)
            if v is None:
                vals.append("null")
            elif c == "dia":
                vals.append(f"'{str(v)[:10]}'")
            elif c == "loja":
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
    # Incluir apenas meses COMPLETOS no AN para evitar distorção
    # O mês atual só entra se tiver dados reais de volume
    n_meses_completos = mes_atual - 1 if not vol_rows else mes_atual
    meses_disponiveis = list(range(1, n_meses_completos + 1)) or [1]
    meses_labels = [MESES_PT[m-1] for m in meses_disponiveis]

    # Construir vol e rev por fábrica por mês
    vol: dict[int, list] = {3:[], 6:[], 7:[]}
    rev: dict[int, list] = {3:[], 6:[], 7:[]}
    for fid in [3,6,7]:
        for m in meses_disponiveis:
            row = next((r for r in vol_rows if int(r["factory_id"])==fid and int(r["mes"])==m), None)
            vol[fid].append(int(row["volume"]) if row else 0)
            rev[fid].append(float(row["receita"]) if row else 0.0)

    # Se vol_rows vazio, estimar volume e receita a partir dos skus_rows
    if not vol_rows:
        n = len(meses_disponiveis)
        n_completos = n  # meses_disponiveis já excluiu o mês atual incompleto
        for fid in [3,6,7]:
            total_qtd = sum(int(r["qtd_total"]) for r in skus_rows if int(r["factory_id"])==fid)
            total_val = sum(float(r["val_total"]) for r in skus_rows if int(r["factory_id"])==fid)
            base_vol = max(1, total_qtd // n_completos)
            base_rev = round(total_val / n_completos, 2)
            vol[fid] = [base_vol] * n_completos
            rev[fid] = [base_rev] * n_completos
        log(f"  ↳ vol_rows vazio — vol+receita estimados de {len(skus_rows)} SKUs ({n_completos} meses + 0 atual)")

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
        "/* @@AN_START@@ */\n"
        "const AN = {\n"
        f"  months:{json.dumps(meses_labels)},\n"
        f"  meses:{json.dumps(meses_labels)},\n"
        f"  {vol_js},\n"
        f"  {rev_js},\n"
        "  skus:[\n"
        + ",\n".join(sku_lines) + "\n"
        "]\n};"
        "\n/* @@AN_END@@ */"
    )

    # AN_MONTHLY_DIST
    totais = {f: sum(vol[f]) for f in [3,6,7]}
    dist_linhas = []
    for f in [3,6,7]:
        t = totais[f] or 1
        vals = ",".join(f"{v}/{t}" for v in vol[f])
        dist_linhas.append(f"  {f}:[{vals}]")
    dist_js = ("/* @@AN_DIST_START@@ */\n"
               "const AN_MONTHLY_DIST = {\n" + ",\n".join(dist_linhas) + "\n};"
               "\n/* @@AN_DIST_END@@ */")

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
    def _extrair_an_existente(h):
        """Extrai const AN e AN_MONTHLY_DIST do HTML existente."""
        via_marker = _extrair_bloco_entre(h, '/* @@AN_START@@ */', '/* @@AN_END@@ */')
        if via_marker:
            return via_marker
        s = h.find('const AN = ')
        if s < 0:
            return None
        e = h.find('\nconst AN_MONTHLY_DIST', s)
        if e < 0:
            e = h.find('\nconst AN_MONTH', s)
        return h[s:e].strip() if e > s else None

    def _extrair_dist_existente(h):
        via_marker = _extrair_bloco_entre(h, '/* @@AN_DIST_START@@ */', '/* @@AN_DIST_END@@ */')
        if via_marker:
            return via_marker
        s = h.find('const AN_MONTHLY_DIST = ')
        if s < 0:
            return None
        e = h.find('\nconst AN_MONTH_DATES', s)
        if e < 0:
            e = h.find('\n// ', s)
        return h[s:e].strip() if e > s else None

    # Preservar DP_RAW/DP_OPR existentes quando novo for None
    dp_raw_final = dp_raw
    if dp_raw_final is None:
        s_dp = html.find('let DP_RAW=[')
        e_dp = html.find('\nconst DP_OPR_DAILY=', s_dp) if s_dp >= 0 else -1
        dp_raw_final = html[s_dp:e_dp].strip() if s_dp >= 0 and e_dp > s_dp else None
        log(f"  ↳ DP_RAW: {'preservado do HTML existente' if dp_raw_final else 'nao encontrado'}")

    dp_opr_final = dp_opr
    if dp_opr_final is None:
        s_op = html.find('const DP_OPR_DAILY=[')
        e_op = html.find('\nconst AN', s_op) if s_op >= 0 else -1
        dp_opr_final = html[s_op:e_op].strip() if s_op >= 0 and e_op > s_op else None

    # Prioridade fallback: 1) novo dado, 2) an_cache.js, 3) HTML existente
    an_final = an or None
    log(f"  substituir_bloco_dados: an={'OK' if an else 'None'}, an_dist={'OK' if an_dist else 'None'}")
    if not an_final:
        try:
            cache_txt = AN_CACHE_FILE.read_text(encoding="utf-8")
            an_final = cache_txt.split("\nconst AN_MONTHLY_DIST")[0].strip()
            log(f"  ↳ AN: lido de an_cache.js ({len(an_final)} chars)")
        except Exception as e_cache:
            log(f"  ↳ an_cache.js não encontrado: {e_cache}")
            an_final = _extrair_an_existente(html)
            if an_final:
                log(f"  ↳ AN: extraído do HTML ({len(an_final)} chars)")
            else:
                log("  ⚠ AN não encontrado em nenhuma fonte!")

    an_dist_final = an_dist or None
    if not an_dist_final:
        try:
            cache_txt = AN_CACHE_FILE.read_text(encoding="utf-8")
            if "\nconst AN_MONTHLY_DIST" in cache_txt:
                an_dist_final = "const AN_MONTHLY_DIST" + cache_txt.split("\nconst AN_MONTHLY_DIST")[1].strip()
        except Exception:
            an_dist_final = _extrair_dist_existente(html)

    if not an_final:
        log("  ⚠ AN não disponível e não encontrado no HTML existente!")
    if not an_dist_final:
        log("  ⚠ AN_MONTHLY_DIST não disponível e não encontrado no HTML existente!")

    log(f"  Escrevendo: an_final={'OK '+str(len(an_final))+'chars' if an_final else 'VAZIO'}, an_dist={'OK' if an_dist_final else 'VAZIO'}")
    novo = (
        f'{START}\n'
        f'/* === DADOS METABASE — atualizado {datetime.date.today().strftime("%d/%m/%Y")} === */\n'
        f'{dp_raw_final}\n'
        + (f'{dp_opr_final}\n' if dp_opr_final else '')
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
    dp_raw_rows = mb_query(token, SQL_DESEMPENHO_LOJAS.format(ini_dp=ini_dp, fim=fim), limit=500)
    log(f"  ↳ {len(dp_raw_rows)} linhas")
    if len(dp_raw_rows) < 5:
        log(f"  ↳ Poucos dados ({len(dp_raw_rows)}) — ampliando para mês anterior ({ini_dp_fallback[:7]})...")
        dp_raw_rows = mb_query(token, SQL_DESEMPENHO_LOJAS.format(ini_dp=ini_dp_fallback, fim=fim), limit=500)
        log(f"  ↳ {len(dp_raw_rows)} linhas (período ampliado)")

    log("Consultando Desempenho por operador...")
    # Usar o mesmo período que retornou dados para desempenho loja
    _ini_dp_usado = ini_dp_fallback if (not dp_raw_rows or
        (dp_raw_rows and not dp_raw_rows[0].get("dia","").startswith(ini_dp[:7]))
    ) else ini_dp
    dp_opr_rows = mb_query(token, SQL_DESEMPENHO_OPR.format(ini_dp=_ini_dp_usado, fim=fim), limit=500)
    log(f"  ↳ {len(dp_opr_rows)} linhas")


    # ── Consumo: 2 queries leves + join em Python ────────────────────────
    # Query 1: catálogo de materiais (rápida, tabela pequena)
    log("Consultando catálogo de materiais (paginado)...")
    mat_map = {}
    try:
        min_id = 0
        while True:
            page = mb_query(token, SQL_MATERIAIS.format(min_id=min_id), limit=2000, retries=2)
            if not page:
                break
            for r in page:
                ref = (r.get('reference') or '').split(' / ')
                try:
                    mat_map[int(r['id'])] = {
                        'categoria': r['categoria'],
                        'variacao_cor': ref[0].strip() if ref else '',
                        'modelo': ref[1].strip() if len(ref) > 1 else '',
                    }
                except (ValueError, KeyError):
                    pass
            min_id = max(int(r['id']) for r in page)
            if len(page) < 2000:
                break  # última página
        log(f"  ↳ {len(mat_map)} materiais carregados")
    except Exception as e:
        log(f"  ⚠ Catálogo falhou: {e}")
        mat_map = {}

    # ── Query 2: consumo mês a mês — cada mês com try/except isolado ───────
    log("Consultando consumo mensal...")
    consumo_acc = {}   # {(factory_id, material_id, mes): (qtd, val)}
    vol_acc     = {}   # {(factory_id, mes): (volume, receita)}

    primeiro_mes = datetime.date(hoje.year, 1, 1)
    mes_atual    = primeiro_mes
    meses_ok, meses_err = 0, 0

    while mes_atual <= hoje:
        mes_ini_iso = mes_atual.isoformat()
        mes_fim     = (mes_atual.replace(day=28) + datetime.timedelta(days=4)).replace(day=1)
        mes_fim_iso = min(mes_fim, hoje + datetime.timedelta(days=1)).isoformat()
        try:
            rows_mes = mb_query(token, SQL_CONSUMO_MES.format(
                ini=mes_ini_iso, fim=mes_fim_iso), limit=5000, retries=2)
            for r in rows_mes:
                fid = r.get('factory_id')
                mid = r.get('material_id')
                mes = r.get('mes')
                qtd = r.get('qtd', 0) or 0
                if fid and mid and mes:
                    v = float(r.get('val') or 0)
                    k = (int(fid), int(mid), int(mes))
                    prev = consumo_acc.get(k, (0, 0.0))
                    consumo_acc[k] = (prev[0] + int(qtd), prev[1] + v)
            meses_ok += 1
        except Exception as e:
            log(f"  ⚠ Consumo {mes_ini_iso[:7]} falhou: {e}")
            meses_err += 1
        mes_atual = mes_fim

    log(f"  ↳ {len(consumo_acc)} entradas de consumo ({meses_ok} meses OK, {meses_err} erros)")

    try:
        rows_vol = mb_query(token, SQL_VOL_MENSAL.format(fim=fim), limit=100, retries=2)
        for r in rows_vol:
            fid = r.get('factory_id')
            mes = r.get('mes')
            if fid and mes:
                vol_acc[(int(fid), int(mes))] = (int(r.get('volume', 0) or 0),
                                                  float(r.get('receita', 0) or 0))
        log(f"  ↳ {len(vol_acc)} entradas de volume")
    except Exception as e:
        log(f"  ⚠ Volume mensal falhou: {e}")

    # ── Gerar JS ─────────────────────────────────────────────────────────────
    if dp_raw_rows:
        dp_raw_js       = gerar_dp_raw(dp_raw_rows)
        dp_opr_daily_js = gerar_dp_opr_daily(dp_opr_rows)
        log(f"  ↳ DP_RAW gerado: {len(dp_raw_rows)} linhas")
    else:
        dp_raw_js       = None   # preservar existente
        dp_opr_daily_js = None
        log("  ↳ DP_RAW vazio — preservando dados existentes no HTML")

    an_js      = None
    an_dist_js = None
    MIN_SKUS = 50  # não substituir AN se tiver menos que isso
    if consumo_acc and mat_map:
        try:
            # Montar skus_rows: join consumo_acc × mat_map
            from collections import defaultdict
            skus_agg = defaultdict(lambda: {'qtd_total': 0, 'val_total': 0.0,
                                             'factory_id': 0, 'categoria': '',
                                             'variacao_cor': '', 'modelo': ''})
            log(f"  ↳ consumo_acc={len(consumo_acc)} entradas, mat_map={len(mat_map)} materiais")
            for (fid, mid, mes), (qtd, val) in consumo_acc.items():
                if mid not in mat_map:
                    continue
                key = (fid, mid)
                skus_agg[key]['factory_id']   = fid
                skus_agg[key]['categoria']    = mat_map[mid].get('categoria', '')
                skus_agg[key]['variacao_cor'] = mat_map[mid].get('variacao_cor', '')
                skus_agg[key]['modelo']       = mat_map[mid].get('modelo', '')
                skus_agg[key]['qtd_total']   += qtd
                skus_agg[key]['val_total']   += val

            skus_rows = sorted(skus_agg.values(),
                               key=lambda r: -r['qtd_total'])
            vol_rows = [{'factory_id': k[0], 'mes': k[1], 'volume': v[0], 'receita': v[1]}
                        for k, v in vol_acc.items()]
            log(f"  ↳ skus_rows={len(skus_rows)}, vol_rows={len(vol_rows)}")
            if len(skus_rows) < MIN_SKUS:
                log(f"  ⚠ Apenas {len(skus_rows)} SKUs — abaixo do mínimo ({MIN_SKUS}). AN não atualizado.")
                an_js, an_dist_js = None, None
            else:
                an_js, an_dist_js = gerar_an(skus_rows, vol_rows)
                log(f"  ↳ AN gerado com sucesso — {len(skus_rows)} SKUs")
            # Salvar cache persistente — nunca será zerado pelo workflow
            try:
                AN_CACHE_FILE.write_text(an_js + "\n" + an_dist_js, encoding="utf-8")
                log(f"  ↳ an_cache.js salvo ({AN_CACHE_FILE})")
            except Exception as e_cache:
                log(f"  ⚠ an_cache.js não salvo: {e_cache}")
        except Exception as e:
            log(f"  ⚠ gerar_an falhou: {e}")
            an_js, an_dist_js = None, None
    else:
        log("  ⚠ Sem dados de consumo — AN preservado do HTML existente")
        an_js, an_dist_js = None, None

    # ── Atualizar HTML ───────────────────────────────────────────────────────
    # Só abortar se não há NADA novo para salvar
    if not dp_raw_rows and an_js is None:
        log("⚠ DP_RAW e AN ambos vazios — abortando para não zerar o HTML")
        sys.exit(0)
    if not dp_raw_rows:
        log("⚠ DP_RAW vazio — atualizando só o AN (preservando Desempenho)")

    log("Atualizando HTML...")
    with open(HTML_FILE, encoding='utf-8') as f:
        html = f.read()

    html = injetar_no_html(html, dp_raw_js, dp_opr_daily_js, an_js, an_dist_js, hoje)

    with open(HTML_FILE, 'w', encoding='utf-8') as f:
        f.write(html)

    log(f"✅ HTML atualizado: {HTML_FILE}")
    log(f"   Desempenho: {len(dp_raw_rows)} linhas ({ini} → {hoje.isoformat()})")
    if an_js:
        log(f"   Consumo AN: atualizado")
    else:
        log(f"   Consumo AN: preservado (dados insuficientes)")


if __name__ == '__main__':
    main()
