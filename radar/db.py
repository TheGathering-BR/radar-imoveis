"""Conexão e schema do banco SQLite."""
import sqlite3

from radar.config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS bairros (
  id            INTEGER PRIMARY KEY,
  cidade        TEXT NOT NULL DEFAULT 'sao_paulo',
  codigo        TEXT NOT NULL,          -- cd_distrito_municipal (GeoSampa)
  nome          TEXT NOT NULL,
  sigla         TEXT,
  regiao        TEXT,
  geometria     TEXT NOT NULL,          -- GeoJSON WGS84, pronto para o Leaflet
  UNIQUE (cidade, codigo)
);

-- Suporte à geocodificação: centroide de cada quadra fiscal, com o bairro
-- já atribuído por point-in-polygon (feito uma única vez na carga).
CREATE TABLE IF NOT EXISTS quadras (
  cidade    TEXT NOT NULL DEFAULT 'sao_paulo',
  setor     TEXT NOT NULL,              -- 3 primeiros dígitos do SQL
  quadra    TEXT NOT NULL,              -- dígitos 4-6 do SQL
  lat       REAL NOT NULL,
  lon       REAL NOT NULL,
  bairro_id INTEGER REFERENCES bairros(id),
  PRIMARY KEY (cidade, setor, quadra)
);

CREATE TABLE IF NOT EXISTS transacoes (
  id                  INTEGER PRIMARY KEY,
  cidade              TEXT NOT NULL DEFAULT 'sao_paulo',
  sql_cadastro        TEXT,
  logradouro          TEXT,
  numero              TEXT,
  complemento         TEXT,
  cep                 TEXT,
  bairro_iptu         TEXT,             -- texto livre do cadastro IPTU
  natureza            TEXT,
  valor_transacao     REAL,
  data_transacao      TEXT,             -- ISO YYYY-MM-DD
  mes_pagamento       TEXT,             -- YYYY-MM (aba de origem da guia)
  valor_venal_ref     REAL,
  proporcao_pct       REAL,
  base_calculo        REAL,
  valor_financiado    REAL,
  area_terreno_m2     REAL,
  area_construida_m2  REAL,
  fracao_ideal        REAL,
  uso_iptu            TEXT,
  descricao_uso       TEXT,
  padrao_iptu         TEXT,
  descricao_padrao    TEXT,
  ano_construcao      INTEGER,
  lat                 REAL,
  lon                 REAL,
  geocode_metodo      TEXT,             -- 'quadra_fiscal' | NULL
  bairro_id           INTEGER REFERENCES bairros(id),
  preco_m2            REAL,
  classe              TEXT,             -- 'apartamento' | 'casa' | NULL (não residencial)
  elegivel_mediana    INTEGER NOT NULL DEFAULT 0,
  fonte_arquivo       TEXT NOT NULL,    -- ex.: 'itbi_2026.xlsx'
  fonte_aba           TEXT NOT NULL,    -- ex.: 'MAI-2026'
  hash_registro       TEXT NOT NULL UNIQUE
);
CREATE INDEX IF NOT EXISTS idx_trans_bairro_mes
  ON transacoes (bairro_id, mes_pagamento);
CREATE INDEX IF NOT EXISTS idx_trans_fonte
  ON transacoes (fonte_arquivo, fonte_aba);

-- 'todos' = todo o residencial (apartamento + casa). Usos não residenciais
-- (garagens, lojas, escritórios) ficam fora de qualquer classe.
CREATE TABLE IF NOT EXISTS agregados_itbi (
  bairro_id        INTEGER NOT NULL REFERENCES bairros(id),
  mes              TEXT NOT NULL,       -- YYYY-MM (mês da DATA DE TRANSAÇÃO)
  classe           TEXT NOT NULL DEFAULT 'todos',  -- todos | apartamento | casa
  mediana_preco_m2 REAL,
  n_amostras       INTEGER NOT NULL,
  var_3m           REAL,
  var_6m           REAL,
  var_12m          REAL,
  var_24m          REAL,
  PRIMARY KEY (bairro_id, mes, classe)
);

-- ===== Módulo 2: anúncios ativos =====

CREATE TABLE IF NOT EXISTS anuncios (
  id                INTEGER PRIMARY KEY,
  cidade            TEXT NOT NULL DEFAULT 'sao_paulo',
  portal            TEXT NOT NULL,      -- 'vivareal'
  portal_id         TEXT NOT NULL,
  url               TEXT,
  tipo              TEXT,               -- apartamento, casa, cobertura...
  categoria         TEXT,               -- USED | DEVELOPMENT
  preco             REAL,
  condominio        REAL,
  iptu_mensal       REAL,
  area_m2           REAL,
  quartos           INTEGER,
  banheiros         INTEGER,
  suites            INTEGER,
  vagas             INTEGER,
  endereco          TEXT,
  bairro_texto      TEXT,               -- como o anúncio nomeia o bairro
  lat               REAL,
  lon               REAL,
  bairro_id         INTEGER REFERENCES bairros(id),
  preco_m2          REAL,
  classe            TEXT,               -- apartamento | casa | casa_vila | cobertura
  fingerprint       TEXT,               -- dedup entre portais (mesmo imóvel)
  elegivel_mediana  INTEGER NOT NULL DEFAULT 0,
  primeira_captura  TEXT NOT NULL,
  ultima_captura    TEXT NOT NULL,
  UNIQUE (portal, portal_id)
);
CREATE INDEX IF NOT EXISTS idx_anuncios_bairro ON anuncios (bairro_id);
CREATE INDEX IF NOT EXISTS idx_anuncios_fp ON anuncios (fingerprint);

-- Histórico de preço: um registro por anúncio por coleta em que apareceu.
CREATE TABLE IF NOT EXISTS anuncio_capturas (
  anuncio_id   INTEGER NOT NULL REFERENCES anuncios(id),
  capturado_em TEXT NOT NULL,           -- ISO datetime da coleta
  preco        REAL,
  PRIMARY KEY (anuncio_id, capturado_em)
);

-- Mediana do preço/m² PEDIDO por bairro/mês (série separada da ITBI,
-- que é preço FECHADO; a diferença vira o "desconto típico" no dashboard).
CREATE TABLE IF NOT EXISTS agregados_anuncios (
  bairro_id        INTEGER NOT NULL REFERENCES bairros(id),
  mes              TEXT NOT NULL,       -- YYYY-MM da captura
  classe           TEXT NOT NULL DEFAULT 'todos',
  mediana_preco_m2 REAL,
  n_amostras       INTEGER NOT NULL,
  PRIMARY KEY (bairro_id, mes, classe)
);

-- Controle de ingestão incremental: uma aba só é reprocessada se o
-- conteúdo dela mudou desde a última carga.
CREATE TABLE IF NOT EXISTS ingestoes (
  fonte          TEXT NOT NULL,         -- 'itbi'
  arquivo        TEXT NOT NULL,
  aba            TEXT NOT NULL,
  hash_conteudo  TEXT NOT NULL,
  n_registros    INTEGER NOT NULL,
  processado_em  TEXT NOT NULL,
  PRIMARY KEY (fonte, arquivo, aba)
);
"""


def get_conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _migrar(conn: sqlite3.Connection) -> None:
    """Migrações idempotentes para bancos criados antes da coluna `classe`."""
    for tabela in ("transacoes", "anuncios"):
        cols = {r[1] for r in conn.execute(f"PRAGMA table_info({tabela})")}
        if "classe" not in cols:
            conn.execute(f"ALTER TABLE {tabela} ADD COLUMN classe TEXT")
    cols = {r[1] for r in conn.execute("PRAGMA table_info(anuncios)")}
    if cols and "fingerprint" not in cols:
        conn.execute("ALTER TABLE anuncios ADD COLUMN fingerprint TEXT")
    # Agregados são derivados: se ainda não têm a dimensão classe, recria.
    for tabela in ("agregados_itbi", "agregados_anuncios"):
        cols = {r[1] for r in conn.execute(f"PRAGMA table_info({tabela})")}
        if cols and "classe" not in cols:
            conn.execute(f"DROP TABLE {tabela}")


def init_schema(conn: sqlite3.Connection) -> None:
    _migrar(conn)
    conn.executescript(SCHEMA)
    conn.commit()
