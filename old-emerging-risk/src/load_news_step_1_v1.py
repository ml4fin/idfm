# Importa il modulo standard per leggere file CSV.
import csv
# Importa Path, utile per gestire percorsi file in modo robusto.
from pathlib import Path
# Importa tipi usati nelle annotazioni delle funzioni.
from typing import List, Optional
# Importa psycopg2, libreria per connettersi a PostgreSQL.
import psycopg2
# Importa execute_batch, utile per inserire molte righe in modo più efficiente.
from psycopg2.extras import execute_batch

# Aumenta il limite massimo consentito per un singolo campo CSV.
# Il valore di default del modulo csv è spesso 131072 caratteri.
# Nel nostro caso il campo Body può essere molto più lungo.
csv.field_size_limit(10_000_000)

# ============================================================================
# CONFIGURAZIONE DEBUG / SVILUPPO
# ============================================================================

# Flag puramente informativo: indica che lo script è pensato per essere eseguito da IDE.
RUN_FROM_IDE = True

# Percorso del file CSV da caricare nella staging table.
CSV_FILE_PATH = r"C:/corpus/news-2/FTXX_2014_2023.csv"

# Se True, la staging table viene svuotata prima di caricare il nuovo file.
TRUNCATE_STAGING_BEFORE_LOAD = True

# Percorso del file manifest che contiene l'elenco dei CSV da caricare.
MANIFEST_FILE_PATH = r"./ingestion_manifest.csv"

# Se True, la staging viene svuotata una sola volta prima di caricare tutti i file.
TRUNCATE_STAGING_BEFORE_BATCH = True

# Parametri di connessione al database PostgreSQL.
DB_CONFIG = {
    # Indirizzo del server PostgreSQL.
    "host"     : "localhost",
    # Porta del server PostgreSQL.
    "port"     : 5432,
    # Nome del database a cui connettersi.
    "dbname"   : "research",
    # Utente PostgreSQL.
    "user"     : "postgres",
    # Password dell'utente PostgreSQL.
    "password" : "Unibo1234",
}

# Nome dello schema PostgreSQL che contiene le tabelle del progetto.
SCHEMA_NAME = "risk_emergence"

# Nome della tabella di staging in cui caricare i dati grezzi.
STAGING_TABLE = "news_articles_staging"

# Lista delle colonne attese nel file CSV, nell'ordine esatto.
EXPECTED_COLUMNS = [
    "Title",
    "Body",
    "Date",
    "Language",
    "Source",
    "Sentiment",
    "IsDuplicate",
    "dataType",
    "Url",
]

# Numero massimo di righe da inserire in un singolo batch.
BATCH_SIZE = 2000

# Separatore usato nel CSV.
CSV_DELIMITER = "|"

# Encoding usato per leggere il CSV.
CSV_ENCODING = "utf-8"


# ============================================================================
# CONNESSIONE DATABASE
# ============================================================================

def get_connection():
    """
    Crea una connessione al database PostgreSQL.

    In questa prima versione le credenziali sono ancora definite nel file
    per facilitare il debug. In una versione successiva le sposteremo in
    variabili d'ambiente o in un file .env.
    """

    # Crea e restituisce una connessione PostgreSQL usando i parametri definiti in DB_CONFIG.
    return psycopg2.connect(
        # Host del database.
        host=DB_CONFIG["host"],
        # Porta del database.
        port=DB_CONFIG["port"],
        # Nome del database.
        dbname=DB_CONFIG["dbname"],
        # Nome utente PostgreSQL.
        user=DB_CONFIG["user"],
        # Password PostgreSQL.
        password=DB_CONFIG["password"],
    )


# ============================================================================
# VERIFICHE INIZIALI
# ============================================================================

def check_schema_and_tables(conn) -> None:
    """
    Verifica che lo schema e le tabelle minime richieste esistano.
    """

    # Query che cerca le tre tabelle essenziali nello schema configurato.
    query = """
    SELECT table_schema, table_name
    FROM information_schema.tables
    WHERE table_schema = %s
      AND table_name IN ('news_articles_staging', 'news_articles', 'news_sources')
    ORDER BY table_name;
    """

    # Apre un cursore per eseguire la query.
    with conn.cursor() as cur:
        # Esegue la query passando SCHEMA_NAME come parametro sicuro.
        cur.execute(query, (SCHEMA_NAME,))

        # Recupera tutte le righe restituite dalla query.
        rows = cur.fetchall()

    # Converte le righe trovate in un insieme di tuple (schema, tabella).
    found = {(r[0], r[1]) for r in rows}

    # Definisce l'insieme delle tabelle che devono esistere.
    required = {
        (SCHEMA_NAME, "news_articles_staging"),
        (SCHEMA_NAME, "news_articles"),
        (SCHEMA_NAME, "news_sources"),
    }

    # Calcola quali tabelle richieste mancano.
    missing = required - found

    # Se manca almeno una tabella, interrompe l'esecuzione con un errore.
    if missing:
        raise RuntimeError(f"Tabelle mancanti nello schema {SCHEMA_NAME}: {missing}")

    # Messaggio di conferma se tutte le tabelle sono presenti.
    print("Verifica schema OK: tabelle richieste presenti.")

# ============================================================================

def get_staging_columns(conn) -> List[str]:
    """
    Legge e stampa le colonne della staging table.
    Utile per verificare che il DDL caricato sia quello atteso.
    """

    # Query che legge le colonne della staging table nell'ordine definito nel database.
    query = """
    SELECT column_name
    FROM information_schema.columns
    WHERE table_schema = %s
      AND table_name = %s
    ORDER BY ordinal_position;
    """

    # Apre un cursore per interrogare il catalogo del database.
    with conn.cursor() as cur:
        # Esegue la query passando schema e nome tabella come parametri.
        cur.execute(query, (SCHEMA_NAME, STAGING_TABLE))

        # Recupera tutte le colonne trovate.
        rows = cur.fetchall()

    # Estrae il nome della colonna da ciascuna riga del risultato.
    cols = [r[0] for r in rows]

    # Stampa un'intestazione leggibile.
    print("Colonne staging trovate:")

    # Stampa ogni colonna trovata nella staging table.
    for col in cols:
        print(f"  - {col}")

    # Restituisce la lista delle colonne, utile per eventuali controlli successivi.
    return cols

# ============================================================================

def validate_csv_header(header: List[str]) -> None:
    """
    Verifica che l'header del CSV coincida con quello atteso.
    """

    # Confronta l'header letto dal CSV con la lista delle colonne attese.
    if header != EXPECTED_COLUMNS:
        # Se l'header è diverso, interrompe l'esecuzione mostrando atteso e trovato.
        raise ValueError(
            "Header CSV inatteso.\n"
            f"Atteso:  {EXPECTED_COLUMNS}\n"
            f"Trovato: {header}"
        )

    # Messaggio di conferma se l'header è corretto.
    print("Header CSV OK.")


# ============================================================================
# OPERAZIONI SULLA STAGING TABLE
# ============================================================================

def truncate_staging(conn) -> None:
    """
    Svuota la staging table prima del caricamento.
    """

    # Costruisce il comando SQL per svuotare completamente la staging table.
    sql = f"TRUNCATE TABLE {SCHEMA_NAME}.{STAGING_TABLE};"

    # Apre un cursore per eseguire il comando SQL.
    with conn.cursor() as cur:
        # Esegue il TRUNCATE sulla staging table.
        cur.execute(sql)

    # Conferma definitivamente l'operazione sul database.
    conn.commit()

    # Stampa un messaggio di conferma.
    print("Staging table svuotata.")

# ============================================================================

def insert_batch_into_staging(conn, batch_rows: List[tuple]) -> None:
    """
    Inserisce un batch di righe nella staging table.
    """

    # Query parametrica di inserimento nella staging table.
    sql = f"""
    INSERT INTO {SCHEMA_NAME}.{STAGING_TABLE}
    (
        title,
        body,
        publication_date,
        language,
        source,
        sentiment,
        is_duplicate,
        data_type,
        url,
        _raw_line_number,
        _source_file
    )
    VALUES
    (
        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
    )
    """

    # Apre un cursore per eseguire l'inserimento batch.
    with conn.cursor() as cur:
        # Inserisce tutte le righe del batch usando execute_batch.
        execute_batch(cur, sql, batch_rows, page_size=BATCH_SIZE)

# ============================================================================
# CARICAMENTO CSV
# ============================================================================

def read_ingestion_manifest(manifest_path: str) -> List[dict]:
    """
    Legge il file manifest che contiene l'elenco dei CSV da caricare.

    Il manifest deve avere almeno queste colonne:
    enabled,file_path,max_rows,description

    enabled:
        1 per caricare il file, 0 per ignorarlo.

    file_path:
        percorso completo del CSV.

    max_rows:
        numero massimo di righe da caricare per quel file.
        Se vuoto, carica tutto il file.

    description:
        descrizione libera del file.
    """

    # Converte il percorso del manifest in un oggetto Path.
    manifest_file = Path(manifest_path)

    # Verifica che il file manifest esista.
    if not manifest_file.exists():
        raise FileNotFoundError(f"Manifest file non trovato: {manifest_file}")

    # Lista che conterrà i file da caricare.
    files_to_load = []

    # Apre il manifest in lettura.
    with open(manifest_file, "r", encoding="utf-8", newline="") as f:

        # Usa DictReader per leggere il manifest come dizionario per colonna.
        reader = csv.DictReader(f)

        # Definisce le colonne minime richieste.
        required_columns = {"enabled", "file_path", "max_rows", "description"}

        # Verifica che il manifest abbia tutte le colonne richieste.
        if reader.fieldnames is None or not required_columns.issubset(set(reader.fieldnames)):
            raise ValueError(
                "Manifest non valido.\n"
                f"Colonne richieste: {required_columns}\n"
                f"Colonne trovate: {reader.fieldnames}"
            )

        # Itera sulle righe del manifest.
        for manifest_line, row in enumerate(reader, start=2):

            # Legge il flag enabled e rimuove spazi inutili.
            enabled = row["enabled"].strip()

            # Se enabled non è 1, il file viene ignorato.
            if enabled != "1":
                print(f"[MANIFEST] Riga {manifest_line}: file disabilitato, salto.")
                continue

            # Legge il percorso del CSV.
            file_path = row["file_path"].strip()

            # Legge la descrizione del file.
            description = row["description"].strip()

            # Legge max_rows come testo.
            max_rows_raw = row["max_rows"].strip()

            # Se max_rows è vuoto, significa caricare tutto il file.
            if max_rows_raw == "":
                max_rows = None

            # Altrimenti converte max_rows in intero.
            else:
                max_rows = int(max_rows_raw)

            # Aggiunge il file alla lista dei file da caricare.
            files_to_load.append(
                {
                    "file_path": file_path,
                    "max_rows": max_rows,
                    "description": description,
                }
            )

    # Stampa quanti file risultano abilitati nel manifest.
    print(f"File abilitati nel manifest: {len(files_to_load)}")

    # Restituisce la lista dei file da caricare.
    return files_to_load

# ============================================================================

def load_manifest_to_staging(
    manifest_path: str,
    truncate_before_batch: bool = True,
) -> None:
    """
    Carica nella staging table tutti i CSV abilitati nel manifest.

    La staging viene svuotata una sola volta all'inizio del batch.
    Ogni file viene poi caricato in append.
    """

    # Legge dal manifest l'elenco dei file da caricare.
    files_to_load = read_ingestion_manifest(manifest_path)

    # Se il manifest non contiene file abilitati, interrompe l'esecuzione.
    if not files_to_load:
        print("Nessun file abilitato nel manifest. Nulla da caricare.")
        return

    # Lista che raccoglierà i report dei singoli file.
    reports = []

    # Flag che indica se il prossimo file è il primo del batch.
    first_file = True

    # Itera su tutti i file abilitati nel manifest.
    for item in files_to_load:

        # Estrae il percorso del CSV.
        file_path = item["file_path"]

        # Estrae il limite righe specifico per questo file.
        max_rows = item["max_rows"]

        # Estrae la descrizione del file.
        description = item["description"]

        # Stampa una separazione visiva tra i caricamenti.
        print("")
        print("=" * 80)
        print(f"Inizio caricamento file: {file_path}")
        print(f"Descrizione: {description}")
        print(f"max_rows: {max_rows}")
        print("=" * 80)

        # La staging viene svuotata solo prima del primo file del batch.
        truncate_this_file = truncate_before_batch and first_file

        # Carica il singolo CSV nella staging table.
        report = load_csv_to_staging(
            csv_path=file_path,
            truncate_before_load=truncate_this_file,
            max_rows=max_rows,
        )

        # Salva il report del file appena caricato.
        reports.append(report)

        # Dopo il primo file, i successivi vengono caricati in append.
        first_file = False

    # Calcola il totale delle righe inserite.
    total_inserted = sum(r["inserted_rows"] for r in reports)

    # Calcola il totale delle righe saltate.
    total_skipped = sum(r["skipped_rows"] for r in reports)

    # Stampa il report finale complessivo.
    print("")
    print("=" * 80)
    print("REPORT FINALE INGESTION BATCH")
    print("=" * 80)

    # Stampa il dettaglio per ciascun file.
    for r in reports:
        print(
            f"{r['file']}: "
            f"inserite={r['inserted_rows']}, "
            f"saltate={r['skipped_rows']}"
        )

    # Stampa i totali finali.
    print("-" * 80)
    print(f"Totale righe inserite: {total_inserted}")
    print(f"Totale righe saltate: {total_skipped}")
    
# ============================================================================

def load_csv_to_staging(
    csv_path: str,
    truncate_before_load: bool = True,
    max_rows: Optional[int] = None,
) -> dict:
    """
    Carica un CSV pipe-delimited nella staging table.

    Parametri
    ---------
    csv_path:
        Percorso del file CSV.

    truncate_before_load:
        Se True, svuota la staging table prima del caricamento.

    max_rows:
        Numero massimo di righe dati da caricare.
        Utile in debug: ad esempio max_rows=1000.
        Se None, carica tutto il file.
    """

    # Converte il percorso del file in un oggetto Path.
    csv_file = Path(csv_path)

    # Verifica che il file CSV esista davvero.
    if not csv_file.exists():
        # Se il file non esiste, interrompe l'esecuzione con un errore esplicito.
        raise FileNotFoundError(f"File non trovato: {csv_file}")

    # Estrae solo il nome del file, senza il percorso completo.
    source_file_name = csv_file.name

    # Contatore delle righe effettivamente inserite nella staging table.
    inserted_rows = 0

    # Contatore delle righe saltate per problemi strutturali.
    skipped_rows = 0

    # Lista temporanea che contiene le righe da inserire nel batch corrente.
    batch = []

    # Apre una connessione al database usando un context manager.
    with get_connection() as conn:
        # Conferma che la connessione è stata aperta correttamente.
        print("Connessione al database riuscita.")

        # Verifica che schema e tabelle richieste siano presenti.
        check_schema_and_tables(conn)

        # Stampa le colonne della staging table, utile per debug.
        get_staging_columns(conn)

        # Se richiesto, svuota la staging table prima del caricamento.
        if truncate_before_load:
            truncate_staging(conn)

        # Apre il file CSV in lettura testuale con l'encoding configurato.
        with open(csv_file, "r", encoding=CSV_ENCODING, newline="") as f:
            # Crea un reader CSV con delimitatore pipe e quote standard.
            reader = csv.reader(f, delimiter=CSV_DELIMITER, quotechar='"')

            # Prova a leggere la prima riga, che dovrebbe contenere l'header.
            try:
                # Legge l'header del CSV.
                header = next(reader)

            # Gestisce il caso in cui il file sia completamente vuoto.
            except StopIteration:
                raise ValueError("Il file CSV è vuoto.")

            # Verifica che l'header del CSV sia quello atteso.
            validate_csv_header(header)

            # Itera sulle righe dati del CSV, partendo da 2 perché la riga 1 è l'header.
            for raw_idx, row in enumerate(reader, start=2):

                # Se max_rows è impostato, interrompe il caricamento al limite richiesto.
                if max_rows is not None and (inserted_rows + len(batch)) >= max_rows:
                    print(f"Raggiunto limite debug max_rows={max_rows}.")
                    break

                # Verifica che la riga abbia esattamente 9 colonne, come previsto.
                if len(row) != 9:
                    # Incrementa il contatore delle righe saltate.
                    skipped_rows += 1

                    # Stampa un warning con il numero di riga e il numero di colonne trovato.
                    print(
                        f"[WARNING] Riga {raw_idx}: numero colonne inatteso "
                        f"({len(row)} invece di 9). Riga saltata."
                    )

                    # Salta la riga corrente e passa alla successiva.
                    continue

                # Scompone la riga CSV nei nove campi attesi.
                title, body, date_, language, source, sentiment, is_duplicate, data_type, url = row

                # Aggiunge la riga corrente al batch da inserire in staging.
                batch.append(
                    (
                        # Titolo dell'articolo.
                        title,
                        # Corpo testuale dell'articolo.
                        body,
                        # Data di pubblicazione, ancora come testo grezzo.
                        date_,
                        # Lingua dichiarata dal CSV.
                        language,
                        # Fonte grezza indicata dal CSV.
                        source,
                        # Sentiment grezzo, ancora come testo.
                        sentiment,
                        # Flag duplicato grezzo, ancora come testo.
                        is_duplicate,
                        # Tipo dato grezzo.
                        data_type,
                        # URL dell'articolo.
                        url,
                        # Numero di riga originale nel file CSV.
                        raw_idx,
                        # Nome del file sorgente.
                        source_file_name,
                    )
                )

                # Se il batch ha raggiunto la dimensione configurata, viene scritto su database.
                if len(batch) >= BATCH_SIZE:
                    # Inserisce il batch corrente nella staging table.
                    insert_batch_into_staging(conn, batch)
                    # Conferma l'inserimento del batch sul database.
                    conn.commit()
                    # Aggiorna il contatore delle righe inserite.
                    inserted_rows += len(batch)
                    # Stampa lo stato di avanzamento.
                    print(f"Inserite in staging {inserted_rows} righe...")
                    # Svuota il batch per iniziare ad accumulare nuove righe.
                    batch = []

            # Dopo il ciclo, se rimangono righe nel batch, le inserisce.
            if batch:
                # Inserisce l'ultimo batch, anche se più piccolo di BATCH_SIZE.
                insert_batch_into_staging(conn, batch)
                # Conferma l'inserimento dell'ultimo batch.
                conn.commit()
                # Aggiorna il contatore finale delle righe inserite.
                inserted_rows += len(batch)

        # Stampa una riga vuota per leggibilità.
        print("")
        # Messaggio finale di completamento.
        print("Caricamento staging completato.")
        # Stampa il numero totale di righe inserite.
        print(f"Totale righe inserite in staging: {inserted_rows}")
        # Stampa il numero totale di righe saltate.
        print(f"Totale righe saltate: {skipped_rows}")

        return {
            "file": source_file_name,
            "inserted_rows": inserted_rows,
            "skipped_rows": skipped_rows,
        }
# ============================================================================
# MAIN PER DEBUG DA IDE
# ============================================================================

def main():
    """
    Punto di ingresso principale.

    In questa versione lo script legge un manifest esterno e carica
    tutti i CSV abilitati nella staging table.
    """

    # Avvia il caricamento del CSV nella staging table.
    '''
    load_csv_to_staging(
        # Percorso del file CSV da caricare.
        csv_path=CSV_FILE_PATH,
        # Decide se svuotare la staging table prima del caricamento.
        truncate_before_load=TRUNCATE_STAGING_BEFORE_LOAD,
        # Limite righe per debug; usare None per caricare tutto il file.
        max_rows=1000,
    )
    '''
    
    # Avvia il caricamento batch leggendo l'elenco dei file dal manifest.
    load_manifest_to_staging(
        # Percorso del manifest da caricare.
        manifest_path=MANIFEST_FILE_PATH,
        # Decide se svuotare la staging table prima del caricamento.
        truncate_before_batch=TRUNCATE_STAGING_BEFORE_BATCH,
    )

# Esegue main solo se il file viene lanciato direttamente.
if __name__ == "__main__":
    # Chiama il punto di ingresso principale.
    main()