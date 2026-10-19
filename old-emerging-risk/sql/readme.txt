README.TXT
Schema PostgreSQL per il modulo News
File di riferimento: news_schema.sql

1. SCOPO DEL FILE SQL
Questo script SQL crea l'infrastruttura database del modulo News del progetto
"Risk Emergence". Il file non contiene dati analitici finali, ma definisce
la struttura persistente che servirà per:

- ingestione dei CSV di news in PostgreSQL;
- normalizzazione delle fonti editoriali;
- conservazione del corpus degli articoli;
- ricerca full-text sugli articoli;
- collegamento tra articoli e imprese menzionate;
- gestione dei temi candidati di rischio;
- memorizzazione di score tematici, embedding e indicatori aggregati;
- produzione di viste di supporto per quality check e analisi.

In altre parole, news_schema.sql è il file DDL principale del modulo News:
crea schema, tabelle, indici, trigger, viste e alcuni dati iniziali.

===============================================================================
2. CHE COSA CREA ESATTAMENTE LO SCRIPT
===============================================================================

Lo script crea, nell'ordine logico, i seguenti oggetti.

2.1 Estensioni PostgreSQL
Lo script abilita due estensioni:

- uuid-ossp
  utile per generare UUID, anche se nello schema attuale non è il fulcro
  principale del modulo News.

- pg_trgm
  utile per trigram similarity e fuzzy matching. È una base utile per future
  attività di matching più flessibile su fonti, entità o testi.

2.2 Schema dedicato
Viene creato lo schema:

- risk_emergence

e viene impostato il search_path su:

- risk_emergence, public

Questo significa che, durante l'esecuzione dello script, le tabelle vengono
create nello schema risk_emergence, non in public.

2.3 Tabella di riferimento delle fonti: news_sources
Questa tabella normalizza le testate giornalistiche.
Contiene:

- source_id         chiave primaria numerica;
- source_code       codice breve della testata;
- source_name       nome esteso;
- source_domain     dominio web;
- notes             note opzionali.

Lo script la pre-popola già con cinque fonti:

- Financial Times
- New York Times
- Wall Street Journal
- Los Angeles Times
- USA Today

Questa tabella serve per mappare il valore grezzo Source dei CSV verso una
codifica coerente e interrogabile.

2.4 Tabella principale degli articoli: news_articles
È la tabella centrale del modulo News.
Ogni riga rappresenta un articolo ingerito dal corpus CSV.

Contiene tre gruppi di campi.

a) Campi originali o quasi-originali:
- title
- body
- publication_date
- language
- source_raw
- sentiment_original
- is_duplicate_flag
- data_type
- url

b) Campi derivati e di controllo:
- source_id
- is_english
- is_duplicate_verified
- is_excluded
- word_count
- char_count

c) Campi full-text e metadati:
- body_tsv
- title_tsv
- ingestion_batch
- ingested_at
- content_hash

Questa è la tabella finale su cui dovrebbero convergere gli script Python di
ingestione, dopo il passaggio intermedio nella staging table.

2.5 Indici sulla tabella news_articles
Lo script crea i principali indici necessari per lavorare bene sul corpus:

- idx_articles_pubdate
  per query temporali su publication_date;

- idx_articles_source_date
  per query per fonte + data;

- idx_articles_body_tsv
  indice GIN sul corpo dell'articolo per full-text search;

- idx_articles_title_tsv
  indice GIN sul titolo;

- idx_articles_content_hash
  per deduplicazione esatta basata su hash del body;

- idx_articles_valid
  indice parziale sugli articoli validi
  (inglesi, non duplicati, non esclusi);

- idx_articles_batch
  per debugging e reload per batch/file di ingestione.

2.6 Trigger per full-text e conteggi automatici
Lo script crea:

- la funzione news_articles_tsv_trigger()
- il trigger trg_articles_tsv

Il trigger si attiva prima di INSERT o UPDATE dei campi body e title.
Aggiorna automaticamente:

- body_tsv
- title_tsv
- word_count
- char_count

Questo significa che gli script Python non devono valorizzare manualmente
questi campi: vengono calcolati dal database.

2.7 Tabella di staging: news_articles_staging
Questa tabella è progettata per il caricamento grezzo dei CSV.

Caratteristiche principali:
- niente chiavi esterne;
- niente vincoli logici forti;
- quasi tutti i campi testuali;
- presenza di:
  - _raw_line_number
  - _source_file

Serve come area temporanea di appoggio:
prima si caricano qui i CSV, poi si validano e si trasferiscono i record
corretti in news_articles.

2.8 Tabella anagrafica imprese: firm_universe
Contiene il perimetro imprese (es. S&P 500) e i relativi metadati:
- CIK
- ticker
- nome impresa
- sector
- industry
- SIC
- date di ingresso/uscita dal campione
- flag is_active

Serve per il collegamento tra news e imprese menzionate e, più avanti, anche
per il modulo filing SEC.

2.9 Tabella delle menzioni impresa: news_firm_mentions
Tabella many-to-many tra:
- news_articles
- firm_universe

Ogni riga rappresenta una relazione articolo-impresa con eventuali attributi:
- mention_context
- mention_count
- confidence

Questa tabella sarà popolata da pipeline di NER e entity linking.

2.10 Tabella dei temi candidati: candidate_themes
Contiene i temi di rischio osservati dal progetto.
Lo script la pre-popolà con cinque temi:
- cyber_risk
- climate_risk
- pandemic_risk
- supply_chain_risk
- ai_risk

Ogni tema ha:
- nome tecnico
- etichetta
- descrizione
- seed words
- flag attivo/inattivo
- metadata temporali

2.11 Tabella degli score tematici: news_theme_scores
Contiene, per ogni coppia articolo-tema, uno score continuo di rilevanza.
Campi principali:
- article_id
- theme_id
- score
- method
- model_version
- computed_at

Permette di conservare più metodi di tagging/classificazione sullo stesso
articolo.

2.12 Tabella degli embedding: news_embeddings
Contiene embedding documentali associati agli articoli.
Chiave primaria composta:
- article_id
- model_name

Il campo embedding è definito come REAL[].
Questo implica che, nello stato attuale, il database conserva il vettore ma
le operazioni vettoriali avanzate saranno gestite soprattutto lato Python,
salvo futura adozione di pgvector.

2.13 Tabella degli indicatori aggregati: news_indicators
Contiene serie temporali aggregate per tema e periodo.
Campi tipici:
- article_count
- article_count_norm
- burst_state
- burst_intensity
- novelty_score
- semantic_drift
- n_firms_mentioned
- n_sectors_mentioned
- herfindahl_sector

Questa tabella sarà popolata nelle fasi successive del progetto, non durante
l'ingestione iniziale dei CSV.

2.14 Viste di utilità
Lo script crea tre viste principali.

a) v_valid_articles
Restituisce solo articoli considerati validi:
- inglesi
- non duplicati
- non esclusi
e aggiunge informazioni della fonte.

b) v_coverage_by_source
Restituisce conteggi mensili per testata, utile per quality check e controllo
della copertura temporale del corpus.

c) v_theme_coverage
Restituisce la coverage intensity mensile per tema, normalizzata rispetto
al volume totale di articoli validi.

===============================================================================
3. COME SI USA IL FILE SQL
===============================================================================

Il file news_schema.sql va eseguito una volta per creare la struttura del
database, o nuovamente solo in ambiente di test dopo aver prima eliminato o
ricreato lo schema/database.

Non è un file da "importare" come CSV: è uno script SQL da eseguire contro
un database PostgreSQL.

Esistono tre modalità pratiche d'uso.

3.1 Uso da pgAdmin
Procedura consigliata se lavori in ambiente Windows desktop.

Passi:
1. apri pgAdmin;
2. connettiti al server PostgreSQL corretto;
3. seleziona il database su cui vuoi creare lo schema;
4. apri il Query Tool;
5. carica il file news_schema.sql;
6. esegui lo script.

Attenzione:
- lo script crea lo schema risk_emergence nel database corrente;
- quindi devi essere connesso al database giusto prima di eseguirlo.

3.2 Uso da psql
Da terminale, puoi eseguire:

psql -U postgres -d research -f news_schema.sql

oppure, se il database target ha un nome diverso, sostituisci research con
il database corretto.

Su Windows potresti usare una forma tipo:

psql -U postgres -d research -f "C:\percorso\news_schema.sql"

3.3 Uso da IDE o script Python
In alternativa puoi eseguire il contenuto del file anche da Python, leggendo
lo script e passandolo a un cursore psycopg2. Questa modalità è possibile,
ma per un DDL iniziale è in genere più semplice usare pgAdmin o psql.

===============================================================================
4. PREREQUISITI PRIMA DELL'ESECUZIONE
===============================================================================

Prima di eseguire il file assicurati che:

1. PostgreSQL sia installato e funzionante.
2. Tu abbia i permessi per:
   - creare estensioni
   - creare schema
   - creare tabelle
   - creare trigger
   - creare viste
3. Il database target esista già.
4. Le estensioni uuid-ossp e pg_trgm siano disponibili nella tua installazione.
5. Il ruolo che esegue lo script abbia privilegi sufficienti.

Se non hai privilegi per CREATE EXTENSION, lo script può fallire all'inizio.
In tal caso dovrai:
- far creare le estensioni da un amministratore;
- oppure commentare temporaneamente quelle istruzioni, sapendo però che alcune
  funzionalità previste dal progetto potrebbero non essere disponibili.

===============================================================================
5. COSA NON FA IL FILE SQL
===============================================================================

È importante capire anche cosa NON fa.

Questo script:
- non carica automaticamente i CSV delle news;
- non trasferisce i record dalla staging alla tabella finale;
- non esegue deduplicazione near-duplicate;
- non calcola embedding;
- non esegue NER;
- non popola automaticamente news_firm_mentions;
- non popola news_theme_scores salvo i temi iniziali;
- non popola news_indicators;
- non implementa pipeline ETL complete.

Queste attività devono essere svolte dagli script Python di ingestione e dalle
pipeline successive del progetto.

===============================================================================
6. RELAZIONE CON GLI SCRIPT PYTHON DI INGESTIONE
===============================================================================

Nel workflow corretto, il ruolo del file SQL è questo:

Fase A
- esegui news_schema.sql una sola volta per predisporre il database.

Fase B
- lo script Python step 1 carica i CSV grezzi in news_articles_staging.

Fase C
- lo script Python step 2 controlla qualità e anomalie dei dati in staging.

Fase D
- lo script Python step 3 trasferisce i record corretti in news_articles.

Quindi il file SQL è la base strutturale su cui lavorano gli script Python.

===============================================================================
7. COME VERIFICARE CHE L'ESECUZIONE SIA RIUSCITA
===============================================================================

Dopo aver eseguito news_schema.sql, puoi lanciare alcune query di controllo.

7.1 Verifica schema
SELECT schema_name
FROM information_schema.schemata
WHERE schema_name = 'risk_emergence';

7.2 Verifica tabelle principali
SELECT table_name
FROM information_schema.tables
WHERE table_schema = 'risk_emergence'
ORDER BY table_name;

7.3 Verifica fonti pre-popolate
SELECT *
FROM risk_emergence.news_sources;

7.4 Verifica temi pre-popolati
SELECT theme_name, theme_label
FROM risk_emergence.candidate_themes
ORDER BY theme_name;

7.5 Verifica viste
SELECT table_name
FROM information_schema.views
WHERE table_schema = 'risk_emergence'
ORDER BY table_name;

===============================================================================
8. AVVERTENZE IMPORTANTI
===============================================================================

1. Lo script non contiene DROP TABLE o DROP SCHEMA.
   Quindi, se lo esegui due volte senza modifiche, potresti ricevere errori
   perché molte tabelle esistono già.

2. Alcuni INSERT iniziali non sono protetti da meccanismi di idempotenza.
   Se ricrei manualmente le tabelle e riesegui lo script, va bene.
   Se invece provi a rilanciare solo parti dello script su oggetti già esistenti,
   potresti trovare errori o duplicazioni.

3. Il trigger su news_articles aggiorna automaticamente body_tsv, title_tsv,
   word_count e char_count. Gli script Python non devono cercare di popolare
   questi campi manualmente.

4. Il campo content_hash esiste ma il suo popolamento dipende dalla pipeline ETL.
   Lo script SQL definisce la colonna e l'indice, ma non decide da solo quando
   e come calcolare l'hash dei testi caricati.

5. La staging table è intenzionalmente permissiva. Non interpretare l'assenza
   di vincoli come un errore di progettazione: serve proprio a catturare dati
   grezzi e potenzialmente sporchi prima della validazione.

===============================================================================
9. USO MINIMO CONSIGLIATO NEL PROGETTO
===============================================================================

Uso minimo consigliato:

1. crea o scegli il database target;
2. esegui news_schema.sql;
3. verifica che news_sources e candidate_themes siano popolati;
4. testa uno script Python di caricamento verso news_articles_staging;
5. valida i record in staging;
6. trasferisci i record corretti in news_articles;
7. usa v_coverage_by_source per controllare la copertura del corpus.

===============================================================================
10. RIASSUNTO FINALE
===============================================================================

news_schema.sql è il file fondativo del modulo News del progetto.
Serve a costruire l'intera struttura PostgreSQL necessaria per:

- ingestione dei dati grezzi;
- memorizzazione degli articoli;
- full-text search;
- collegamento con imprese e temi;
- memorizzazione di embedding e indicatori;
- supporto alle analisi successive.

Va eseguito una volta all'inizio del progetto sul database corretto.
Dopo la sua esecuzione, il database è pronto per essere usato dagli script
Python di ingestione e dalle pipeline analitiche delle fasi successive.
