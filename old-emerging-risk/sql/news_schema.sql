-- ============================================================================
-- RISK EMERGENCE PROJECT — Schema PostgreSQL (Modulo News)
-- Versione 1.0 — Aprile 2026
-- ============================================================================

-- Estensioni necessarie
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS pg_trgm;       -- trigram similarity per fuzzy matching

-- ============================================================================
-- SCHEMA DEDICATO
-- ============================================================================
CREATE SCHEMA IF NOT EXISTS risk_emergence;
SET search_path TO risk_emergence, public;

-- ============================================================================
-- 1. TABELLA DI RIFERIMENTO: FONTI (news_sources)
-- ============================================================================
-- Normalizzazione delle testate. I valori nel campo Source del CSV possono
-- presentare varianti (es. "ft.com", "Financial Times", "FT"). Questa tabella
-- fornisce un punto di riconciliazione unico.

CREATE TABLE news_sources (
    source_id       SMALLSERIAL PRIMARY KEY,
    source_code     VARCHAR(30)  NOT NULL UNIQUE,   -- codice breve: 'FT', 'NYT', 'WSJ', 'LAT', 'USAT'
    source_name     VARCHAR(100) NOT NULL,           -- nome esteso
    source_domain   VARCHAR(100),                    -- dominio URL (es. "ft.com")
    notes           TEXT
);

INSERT INTO news_sources (source_code, source_name, source_domain) VALUES
    ('FT',   'Financial Times',    'ft.com'),
    ('NYT',  'New York Times',     'nytimes.com'),
    ('WSJ',  'Wall Street Journal', 'wsj.com'),
    ('LAT',  'Los Angeles Times',  'latimes.com'),
    ('USAT', 'USA Today',          'usatoday.com');


-- ============================================================================
-- 2. TABELLA PRINCIPALE: ARTICOLI (news_articles)
-- ============================================================================
-- Ogni riga corrisponde a un articolo ingerito dai CSV.
-- Il campo body_tsv è generato automaticamente via trigger per alimentare
-- l'indice GIN di ricerca full-text.

CREATE TABLE news_articles (
    article_id          BIGSERIAL       PRIMARY KEY,
    
    -- Campi originali dal CSV
    title               TEXT            NOT NULL,
    body                TEXT            NOT NULL,
    publication_date    DATE            NOT NULL,
    language            VARCHAR(10),
    source_raw          VARCHAR(100),           -- valore originale dal CSV, non normalizzato
    sentiment_original  DOUBLE PRECISION,       -- conservato ma non utilizzato
    is_duplicate_flag   BOOLEAN         DEFAULT FALSE,  -- flag originale dal CSV
    data_type           VARCHAR(30),
    url                 TEXT,
    
    -- Campi derivati e di controllo
    source_id           SMALLINT        REFERENCES news_sources(source_id),
    is_english          BOOLEAN         DEFAULT TRUE,
    is_duplicate_verified BOOLEAN       DEFAULT FALSE,  -- esito deduplicazione aggiuntiva
    is_excluded         BOOLEAN         DEFAULT FALSE,  -- esclusione manuale o da regola
    word_count          INTEGER,
    char_count          INTEGER,
    
    -- Full-text search
    body_tsv            TSVECTOR,
    title_tsv           TSVECTOR,
    
    -- Metadati di ingestione
    ingestion_batch     VARCHAR(50),        -- identificativo del file/batch di origine
    ingested_at         TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    
    -- Hash per deduplicazione
    content_hash        BYTEA               -- SHA-256 del body per dedup esatta
);

-- Commenti sulle colonne
COMMENT ON COLUMN news_articles.source_raw IS 'Valore originale del campo Source nel CSV, prima della normalizzazione';
COMMENT ON COLUMN news_articles.sentiment_original IS 'Sentiment pre-calcolato nei CSV originali. Non utilizzato nelle analisi correnti';
COMMENT ON COLUMN news_articles.is_duplicate_flag IS 'Flag IsDuplicate dal CSV originale';
COMMENT ON COLUMN news_articles.is_duplicate_verified IS 'Risultato della deduplicazione aggiuntiva (MinHash/SimHash)';
COMMENT ON COLUMN news_articles.is_excluded IS 'Flag di esclusione: articoli non pertinenti, corrotti o problematici';
COMMENT ON COLUMN news_articles.content_hash IS 'SHA-256 del campo body per identificazione duplicati esatti';
COMMENT ON COLUMN news_articles.ingestion_batch IS 'Nome del file CSV o identificativo del batch di caricamento';


-- ============================================================================
-- 3. INDICI SU news_articles
-- ============================================================================

-- Indice primario per query temporali (il più frequente)
CREATE INDEX idx_articles_pubdate ON news_articles (publication_date);

-- Indice composito: fonte + data (per coverage intensity per testata)
CREATE INDEX idx_articles_source_date ON news_articles (source_id, publication_date);

-- Indice GIN per ricerca full-text sul body
CREATE INDEX idx_articles_body_tsv ON news_articles USING GIN (body_tsv);

-- Indice GIN per ricerca full-text sul titolo
CREATE INDEX idx_articles_title_tsv ON news_articles USING GIN (title_tsv);

-- Indice per deduplicazione esatta
CREATE INDEX idx_articles_content_hash ON news_articles (content_hash);

-- Indice parziale: solo articoli validi (non duplicati, non esclusi, inglese)
-- Questo è l'indice "di lavoro" per tutte le analisi
CREATE INDEX idx_articles_valid ON news_articles (publication_date, source_id)
    WHERE is_english = TRUE
      AND is_duplicate_flag = FALSE
      AND is_duplicate_verified = FALSE
      AND is_excluded = FALSE;

-- Indice per batch di ingestione (utile in fase di debug/reload)
CREATE INDEX idx_articles_batch ON news_articles (ingestion_batch);


-- ============================================================================
-- 4. TRIGGER: AGGIORNAMENTO AUTOMATICO TSVECTOR
-- ============================================================================
-- I campi body_tsv e title_tsv vengono aggiornati automaticamente a ogni
-- INSERT o UPDATE, usando la configurazione linguistica 'english'.

CREATE OR REPLACE FUNCTION news_articles_tsv_trigger()
RETURNS TRIGGER AS $$
BEGIN
    NEW.body_tsv  := to_tsvector('english', COALESCE(NEW.body, ''));
    NEW.title_tsv := to_tsvector('english', COALESCE(NEW.title, ''));
    NEW.word_count := array_length(string_to_array(trim(COALESCE(NEW.body, '')), ' '), 1);
    NEW.char_count := length(COALESCE(NEW.body, ''));
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_articles_tsv
    BEFORE INSERT OR UPDATE OF body, title
    ON news_articles
    FOR EACH ROW
    EXECUTE FUNCTION news_articles_tsv_trigger();


-- ============================================================================
-- 5. STAGING TABLE PER INGESTIONE CSV
-- ============================================================================
-- Tabella senza vincoli per il caricamento massivo via COPY.
-- I dati vengono validati e trasferiti in news_articles in un secondo momento.

CREATE TABLE news_articles_staging (
    title               TEXT,
    body                TEXT,
    publication_date    TEXT,        -- TEXT per gestire formati data eterogenei
    language            TEXT,
    source              TEXT,
    sentiment           TEXT,        -- TEXT per catturare valori anomali
    is_duplicate        TEXT,        -- TEXT: "True"/"False" come stringa
    data_type           TEXT,
    url                 TEXT,
    _raw_line_number    BIGINT,     -- numero riga nel CSV, per tracciabilità
    _source_file        TEXT        -- nome del file CSV di origine
);

COMMENT ON TABLE news_articles_staging IS 'Tabella di staging per ingestione massiva via COPY. Nessun vincolo, tutti i campi TEXT. Dopo validazione, i dati vengono trasferiti in news_articles.';


-- ============================================================================
-- 6. ANAGRAFICA IMPRESE (firm_universe)
-- ============================================================================
-- Tabella di riferimento per le imprese S&P 500 con validità temporale.
-- Utilizzata per il linking entità news → impresa e per i filing SEC.

CREATE TABLE firm_universe (
    firm_id             SERIAL          PRIMARY KEY,
    cik                 VARCHAR(20)     NOT NULL,       -- Central Index Key SEC
    ticker              VARCHAR(10),
    company_name        VARCHAR(200)    NOT NULL,
    sector              VARCHAR(100),                   -- GICS sector
    industry            VARCHAR(100),                   -- GICS industry
    sic_code            VARCHAR(10),                    -- SIC code
    sp500_entry_date    DATE,                           -- data ingresso S&P 500
    sp500_exit_date     DATE,                           -- data uscita (NULL se ancora membro)
    is_active           BOOLEAN         DEFAULT TRUE,
    
    CONSTRAINT uq_firm_cik_period UNIQUE (cik, sp500_entry_date)
);

CREATE INDEX idx_firm_ticker ON firm_universe (ticker);
CREATE INDEX idx_firm_cik ON firm_universe (cik);
CREATE INDEX idx_firm_sector ON firm_universe (sector);
CREATE INDEX idx_firm_active ON firm_universe (is_active) WHERE is_active = TRUE;

COMMENT ON TABLE firm_universe IS 'Anagrafica S&P 500 con validità temporale. Una impresa può avere più righe se è uscita e rientrata nell indice.';


-- ============================================================================
-- 7. MENZIONI IMPRESA NEGLI ARTICOLI (news_firm_mentions)
-- ============================================================================
-- Tabella di collegamento many-to-many tra articoli e imprese.
-- Popolata dal NER (spaCy) applicato nella Fase 1.

CREATE TABLE news_firm_mentions (
    mention_id      BIGSERIAL       PRIMARY KEY,
    article_id      BIGINT          NOT NULL REFERENCES news_articles(article_id),
    firm_id         INTEGER         NOT NULL REFERENCES firm_universe(firm_id),
    mention_context VARCHAR(20)     NOT NULL DEFAULT 'body',   -- 'title', 'body', 'both'
    mention_count   SMALLINT        DEFAULT 1,                 -- occorrenze nel documento
    confidence      REAL,                                       -- score NER [0, 1]
    
    CONSTRAINT uq_article_firm UNIQUE (article_id, firm_id)
);

CREATE INDEX idx_mentions_article ON news_firm_mentions (article_id);
CREATE INDEX idx_mentions_firm ON news_firm_mentions (firm_id);
CREATE INDEX idx_mentions_firm_article ON news_firm_mentions (firm_id, article_id);

COMMENT ON TABLE news_firm_mentions IS 'Collegamento articolo-impresa estratto via NER. Una riga per coppia articolo-impresa.';


-- ============================================================================
-- 8. DEFINIZIONE DEI TEMI CANDIDATI (candidate_themes)
-- ============================================================================
-- I temi identificati tramite burst detection e topic modeling.
-- Ogni tema ha un set di seed words e una descrizione.

CREATE TABLE candidate_themes (
    theme_id        SERIAL          PRIMARY KEY,
    theme_name      VARCHAR(100)    NOT NULL UNIQUE,
    theme_label     VARCHAR(200),                       -- etichetta leggibile
    description     TEXT,
    seed_words      TEXT[],                              -- parole chiave iniziali
    is_active       BOOLEAN         DEFAULT TRUE,
    created_at      TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    notes           TEXT
);

COMMENT ON TABLE candidate_themes IS 'Registro dei temi candidati per l analisi di emergenza. Include sia temi storici (cyber, climate) sia il tema AI risk.';

-- Pre-popolamento con temi candidati noti
INSERT INTO candidate_themes (theme_name, theme_label, seed_words) VALUES
    ('cyber_risk',       'Cybersecurity Risk',       ARRAY['cybersecurity', 'data breach', 'hacking', 'cyber attack', 'ransomware']),
    ('climate_risk',     'Climate / Environmental Risk', ARRAY['climate change', 'carbon', 'emissions', 'ESG', 'environmental']),
    ('pandemic_risk',    'Pandemic Risk',            ARRAY['pandemic', 'COVID', 'epidemic', 'contagion', 'quarantine']),
    ('supply_chain_risk','Supply Chain Risk',         ARRAY['supply chain', 'disruption', 'shortage', 'logistics', 'semiconductor shortage']),
    ('ai_risk',          'AI-Related Risk',          ARRAY['artificial intelligence', 'machine learning', 'AI regulation', 'algorithmic', 'deepfake', 'generative AI']);


-- ============================================================================
-- 9. TAGGING DEI TEMI SUGLI ARTICOLI (news_theme_scores)
-- ============================================================================
-- Score continuo per ogni coppia articolo-tema.
-- Prodotto dal tagger (supervised o weakly supervised) nella Fase 2.

CREATE TABLE news_theme_scores (
    score_id        BIGSERIAL       PRIMARY KEY,
    article_id      BIGINT          NOT NULL REFERENCES news_articles(article_id),
    theme_id        INTEGER         NOT NULL REFERENCES candidate_themes(theme_id),
    score           REAL            NOT NULL,               -- score continuo [0, 1]
    method          VARCHAR(30)     NOT NULL,               -- 'supervised', 'weakly_supervised', 'keyword'
    model_version   VARCHAR(50),                            -- versione del modello/tagger
    computed_at     TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    
    CONSTRAINT uq_article_theme_method UNIQUE (article_id, theme_id, method)
);

CREATE INDEX idx_theme_scores_article ON news_theme_scores (article_id);
CREATE INDEX idx_theme_scores_theme ON news_theme_scores (theme_id);
CREATE INDEX idx_theme_scores_theme_score ON news_theme_scores (theme_id, score DESC);

COMMENT ON TABLE news_theme_scores IS 'Score continuo di rilevanza per ogni coppia articolo-tema. Più metodi possono coesistere per lo stesso articolo-tema.';


-- ============================================================================
-- 10. EMBEDDING DOCUMENTALI (news_embeddings)
-- ============================================================================
-- Embedding vettoriali a livello di articolo, calcolati con sentence-transformers.
-- Se pgvector è installato, il campo può essere convertito in tipo VECTOR.
-- Altrimenti si usa REAL[] con operazioni in Python.

CREATE TABLE news_embeddings (
    article_id      BIGINT          NOT NULL REFERENCES news_articles(article_id),
    model_name      VARCHAR(50)     NOT NULL,               -- es. 'all-MiniLM-L6-v2'
    embedding       REAL[]          NOT NULL,               -- vettore di embedding
    computed_at     TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    
    PRIMARY KEY (article_id, model_name)
);

COMMENT ON TABLE news_embeddings IS 'Embedding vettoriali per calcolo di novelty e semantic drift. Chiave: articolo + modello. Se pgvector è disponibile, convertire REAL[] in VECTOR(dim).';


-- ============================================================================
-- 11. SERIE TEMPORALI AGGREGATE (news_indicators)
-- ============================================================================
-- Tabella materializzata con gli indicatori aggregati per tema e periodo.
-- Popolata da query di aggregazione nelle Fasi 2-3.

CREATE TABLE news_indicators (
    indicator_id        BIGSERIAL   PRIMARY KEY,
    theme_id            INTEGER     NOT NULL REFERENCES candidate_themes(theme_id),
    period_start        DATE        NOT NULL,               -- inizio periodo (mese)
    period_type         VARCHAR(10) NOT NULL DEFAULT 'month', -- 'month', 'quarter', 'week'
    
    -- Indicatori di coverage
    article_count       INTEGER,                -- conteggio articoli grezzi
    article_count_norm  REAL,                   -- normalizzato per volume totale
    
    -- Burst detection
    burst_state         SMALLINT,               -- stato di burst (Kleinberg): 0 = baseline, 1+ = burst
    burst_intensity     REAL,                   -- intensità del burst
    
    -- Novelty e semantic drift
    novelty_score       REAL,                   -- distanza coseno vs. baseline
    semantic_drift      REAL,                   -- drift cumulato
    
    -- Co-mention
    n_firms_mentioned   INTEGER,                -- numero imprese co-menzionate
    n_sectors_mentioned INTEGER,                -- numero settori coinvolti
    herfindahl_sector   REAL,                   -- concentrazione settoriale (HHI)
    
    -- Metadati di calcolo
    computed_at         TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    computation_notes   TEXT,
    
    CONSTRAINT uq_theme_period UNIQUE (theme_id, period_start, period_type)
);

CREATE INDEX idx_indicators_theme_period ON news_indicators (theme_id, period_start);

COMMENT ON TABLE news_indicators IS 'Indicatori news-based aggregati per tema e periodo. Popolata nelle Fasi 2-3 da query di aggregazione.';


-- ============================================================================
-- 12. VISTE DI UTILITÀ
-- ============================================================================

-- Vista: articoli validi (filtro standard per tutte le analisi)
CREATE VIEW v_valid_articles AS
SELECT a.*, s.source_code, s.source_name
FROM news_articles a
LEFT JOIN news_sources s ON a.source_id = s.source_id
WHERE a.is_english = TRUE
  AND a.is_duplicate_flag = FALSE
  AND a.is_duplicate_verified = FALSE
  AND a.is_excluded = FALSE;

COMMENT ON VIEW v_valid_articles IS 'Articoli validi: inglese, non duplicati, non esclusi. Filtro standard per tutte le analisi.';

-- Vista: copertura temporale per fonte (quality check)
CREATE VIEW v_coverage_by_source AS
SELECT 
    s.source_code,
    s.source_name,
    date_trunc('month', a.publication_date)::DATE AS month,
    COUNT(*) AS total_articles,
    COUNT(*) FILTER (WHERE a.is_duplicate_flag = FALSE 
                       AND a.is_duplicate_verified = FALSE 
                       AND a.is_excluded = FALSE) AS valid_articles
FROM news_articles a
LEFT JOIN news_sources s ON a.source_id = s.source_id
GROUP BY s.source_code, s.source_name, date_trunc('month', a.publication_date)
ORDER BY s.source_code, month;

COMMENT ON VIEW v_coverage_by_source IS 'Conteggio mensile articoli per testata. Utile per verificare uniformità della copertura temporale.';

-- Vista: coverage intensity per tema (input per burst detection)
CREATE VIEW v_theme_coverage AS
SELECT 
    t.theme_name,
    date_trunc('month', a.publication_date)::DATE AS month,
    COUNT(*) AS theme_articles,
    total.total AS total_articles,
    ROUND(COUNT(*)::NUMERIC / NULLIF(total.total, 0), 6) AS coverage_ratio
FROM news_theme_scores ts
JOIN news_articles a ON ts.article_id = a.article_id
JOIN candidate_themes t ON ts.theme_id = t.theme_id
JOIN LATERAL (
    SELECT COUNT(*) AS total 
    FROM v_valid_articles va 
    WHERE date_trunc('month', va.publication_date) = date_trunc('month', a.publication_date)
) total ON TRUE
WHERE ts.score >= 0.5      -- soglia configurabile
  AND a.is_english = TRUE
  AND a.is_duplicate_flag = FALSE
  AND a.is_excluded = FALSE
GROUP BY t.theme_name, date_trunc('month', a.publication_date), total.total
ORDER BY t.theme_name, month;

COMMENT ON VIEW v_theme_coverage IS 'Coverage intensity mensile per tema, normalizzata per volume totale. Soglia score configurabile.';


-- ============================================================================
-- 13. GRANT E PERMESSI (da personalizzare)
-- ============================================================================
-- GRANT USAGE ON SCHEMA risk_emergence TO researcher;
-- GRANT SELECT ON ALL TABLES IN SCHEMA risk_emergence TO researcher;
-- GRANT INSERT, UPDATE ON news_articles, news_articles_staging TO etl_user;
