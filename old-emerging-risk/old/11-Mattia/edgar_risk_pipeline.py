from __future__ import annotations

# imports
import argparse
import json
import re
from dataclasses import dataclass
from datetime import datetime, UTC
from pathlib import Path
from typing import Dict, List, Tuple

import numpy as np
import pandas as pd
import yaml
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

from edgar import Company, set_identity

# base dirs for the whole project
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
RAW_DIR = DATA_DIR / "raw"
PROCESSED_DIR = DATA_DIR / "processed"
OUTPUT_DIR = BASE_DIR / "output"
CONFIG_DIR = BASE_DIR / "config"

# subdirs for inputs and outputs
LM_DIR = RAW_DIR / "lm"
EDGAR_RAW_DIR = RAW_DIR / "edgar"
FILINGS_PROCESSED_DIR = PROCESSED_DIR / "filings"
FEATURES_DIR = PROCESSED_DIR / "features"

TABLES_DIR = OUTPUT_DIR / "tables"
FIGURES_DIR = OUTPUT_DIR / "figures"
LOGS_DIR = OUTPUT_DIR / "logs"


@dataclass
class RiskCategory:
    # simple container for each risk category in the taxonomy
    name: str
    description: str
    seed_terms: List[str]


def parse_args() -> argparse.Namespace:
    """
    parse cli args for the pipeline

    Args:
        none

    Returns:
        argparse.Namespace: parsed args object
    """
    parser = argparse.ArgumentParser(
        description="Download 8-K filings and score risk language."
    )

    # list of tickers to pull from edgar
    parser.add_argument(
        "--tickers",
        nargs="+",
        required=True,
        help="Ticker symbols, e.g. AAPL MSFT NVDA",
    )

    # date window for filings
    parser.add_argument("--start-date", required=True, help="YYYY-MM-DD")
    parser.add_argument("--end-date", required=True, help="YYYY-MM-DD")

    # path to lm dictionary csv
    parser.add_argument(
        "--lm-dictionary-csv",
        default=str(LM_DIR / "Loughran-McDonald_MasterDictionary_1993-2025.csv"),
        help="Path to the LM dictionary CSV",
    )

    # path to taxonomy yaml
    parser.add_argument(
        "--taxonomy",
        default=str(CONFIG_DIR / "risk_taxonomy.yml"),
        help="Path to config/risk_taxonomy.yml",
    )

    # sec user identity string
    parser.add_argument(
        "--identity",
        default="Mattia Hunziker hunziker.mattiad@gmail.com",
        help="SEC-compliant identity string",
    )

    # max nr of filings per ticker to avoid going too crazy
    parser.add_argument(
        "--max-filings-per-ticker",
        type=int,
        default=50,
        help="Cap filings per ticker",
    )

    # min len for a paragraph to be kept
    parser.add_argument("--min-paragraph-chars", type=int, default=120)

    # min similarity to assign a semantic label
    parser.add_argument(
        "--semantic-threshold",
        type=float,
        default=0.10,
        help="Minimum cosine similarity required to assign a category",
    )

    # optional flag to save raw text files
    parser.add_argument(
        "--save-raw-text",
        action="store_true",
        help="Save raw filing text to data/raw/edgar",
    )

    return parser.parse_args()


def load_taxonomy(path: str) -> List[RiskCategory]:
    """
    load taxonomy yaml into riskcategory objects

    Args:
        path: path to yaml taxonomy file

    Returns:
        List[RiskCategory]: list of taxonomy categories
    """
    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)

    categories = []
    for name, payload in raw["risk_categories"].items():
        categories.append(
            RiskCategory(
                name=name,
                description=payload["description"],
                seed_terms=list(payload.get("seed_terms", [])),
            )
        )

    return categories


def load_lm_dictionary(path: str) -> Dict[str, Dict[str, int]]:
    """
    load lm csv and turn it into a word lookup map

    Args:
        path: path to lm dictionary csv

    Returns:
        Dict[str, Dict[str, int]]: word -> lm category values map
    """
    lm = pd.read_csv(path)

    # strip cols just in case csv headers are messy
    lm.columns = [c.strip() for c in lm.columns]

    # keep only lm cols we care about
    keep_cols = [
        c
        for c in [
            "Word",
            "Negative",
            "Uncertainty",
            "Litigious",
            "Constraining",
            "Positive",
        ]
        if c in lm.columns
    ]

    lm = lm[keep_cols].copy()

    # lower words so lookup matches tokenized text
    lm["Word"] = lm["Word"].astype(str).str.lower()

    return lm.set_index("Word").to_dict(orient="index")


def clean_text(text: str) -> str:
    """
    do light text cleaning on filing text

    Args:
        text: raw text or html-like text

    Returns:
        str: cleaned text
    """
    if not isinstance(text, str):
        return ""

    # remove html tags
    text = re.sub(r"<[^>]+>", " ", text)

    # normalize line breaks
    text = text.replace("\r", "\n")

    # collapse too many blank lines
    text = re.sub(r"\n{3,}", "\n\n", text)

    # collapse repeated spaces and tabs
    text = re.sub(r"[ \t]+", " ", text)

    return text.strip()


def split_paragraphs(text: str, min_chars: int = 120) -> List[str]:
    """
    split cleaned text into paragraphs and filter short ones

    Args:
        text: full filing text
        min_chars: min paragraph len to keep

    Returns:
        List[str]: list of kept paragraphs
    """
    # split on empty lines
    rough = re.split(r"\n\s*\n", text)
    paragraphs = []

    for p in rough:
        # normalize whitespace inside each paragraph
        p = re.sub(r"\s+", " ", p).strip()

        # keep only paragraphs above threshold
        if len(p) >= min_chars:
            paragraphs.append(p)

    return paragraphs


def tokenize(text: str) -> List[str]:
    """
    basic tokenizer for alphabetic words

    Args:
        text: input text

    Returns:
        List[str]: token list
    """
    return re.findall(r"[A-Za-z][A-Za-z\-']+", text.lower())


def lm_score_text(text: str, lm_word_map: Dict[str, Dict[str, int]]) -> Dict[str, float]:
    """
    score text using lm categories and normalize by token count

    Args:
        text: input text
        lm_word_map: lm lookup dict

    Returns:
        Dict[str, float]: normalized lm scores + token count
    """
    tokens = tokenize(text)

    # handle empty token case
    if not tokens:
        return {
            "token_count": 0,
            "lm_negative": 0.0,
            "lm_uncertainty": 0.0,
            "lm_litigious": 0.0,
            "lm_constraining": 0.0,
            "lm_positive": 0.0,
        }

    # raw counts before normalization
    counts = {
        "lm_negative": 0,
        "lm_uncertainty": 0,
        "lm_litigious": 0,
        "lm_constraining": 0,
        "lm_positive": 0,
    }

    for tok in tokens:
        row = lm_word_map.get(tok)

        # skip words not in lm dict
        if row is None:
            continue

        # add 1 if word belongs to given lm bucket
        counts["lm_negative"] += int(row.get("Negative", 0) > 0)
        counts["lm_uncertainty"] += int(row.get("Uncertainty", 0) > 0)
        counts["lm_litigious"] += int(row.get("Litigious", 0) > 0)
        counts["lm_constraining"] += int(row.get("Constraining", 0) > 0)
        counts["lm_positive"] += int(row.get("Positive", 0) > 0)

    # normalize by nr of tokens
    denom = max(len(tokens), 1)
    out = {k: v / denom for k, v in counts.items()}
    out["token_count"] = len(tokens)

    return out


def build_category_profiles(
    categories: List[RiskCategory],
) -> Tuple[TfidfVectorizer, np.ndarray, List[str]]:
    """
    build tf-idf profiles for taxonomy categories

    Args:
        categories: list of risk categories

    Returns:
        Tuple[TfidfVectorizer, np.ndarray, List[str]]: vectorizer, profile matrix, category names
    """
    # combine desc + seed terms into one profile text per category
    profile_texts = [
        f"{cat.description} {' '.join(cat.seed_terms)}" for cat in categories
    ]
    names = [cat.name for cat in categories]

    # fit tf-idf on category profiles
    vectorizer = TfidfVectorizer(ngram_range=(1, 2), stop_words="english")
    matrix = vectorizer.fit_transform(profile_texts)

    return vectorizer, matrix, names


def classify_semantic(
    texts: List[str],
    vectorizer: TfidfVectorizer,
    profile_matrix: np.ndarray,
    category_names: List[str],
    min_similarity: float = 0.10,
) -> Tuple[List[str], List[float]]:
    """
    assign a semantic label to each text using tf-idf cosine sim

    Args:
        texts: list of paragraph texts
        vectorizer: fitted tf-idf vectorizer
        profile_matrix: category profile matrix
        category_names: list of category names
        min_similarity: min sim to assign a category

    Returns:
        Tuple[List[str], List[float]]: labels and sim scores
    """
    if not texts:
        return [], []

    # vectorize input texts using fitted vocab
    text_matrix = vectorizer.transform(texts)

    # compute cosine sim against category profiles
    sims = cosine_similarity(text_matrix, profile_matrix)

    # best match per text
    best_idx = sims.argmax(axis=1)
    best_scores = sims.max(axis=1)

    labels = []
    for idx, score in zip(best_idx, best_scores):
        # if sim too low just mark as unclassified
        if score < min_similarity:
            labels.append("unclassified")
        else:
            labels.append(category_names[idx])

    return labels, best_scores.tolist()


def extract_filing_text(filing) -> str:
    """
    try to extract filing text from html first then markdown

    Args:
        filing: edgar filing object

    Returns:
        str: cleaned filing text
    """
    html = None

    try:
        html = filing.html()
    except Exception:
        html = None

    # html is usually the better source if available
    if html:
        return clean_text(html)

    try:
        md = filing.markdown()
        return clean_text(md)
    except Exception:
        return ""


def get_company_8k_filings(
    ticker: str,
    start_date: str,
    end_date: str,
    max_filings: int,
):
    """
    pull 8-k filings for one ticker inside a date window

    Args:
        ticker: stock ticker
        start_date: start date in yyyy-mm-dd
        end_date: end date in yyyy-mm-dd
        max_filings: max nr of filings to keep

    Returns:
        list: filtered filing objects
    """
    start_dt = pd.to_datetime(start_date).date()
    end_dt = pd.to_datetime(end_date).date()

    company = Company(ticker)
    filings = company.get_filings(form="8-K")
    out = []

    for filing in filings:
        try:
            filing_date = pd.to_datetime(str(filing.filing_date)).date()
        except Exception:
            continue

        # skip filings after the end date
        if filing_date > end_dt:
            continue

        # skip filings before the start date
        if filing_date < start_dt:
            continue

        out.append(filing)

        # stop once cap is reached
        if len(out) >= max_filings:
            break

    return out


def make_raw_text_filename(ticker: str, filing_date: str, accession_no: str) -> str:
    """
    create a safe txt filename for raw filing text

    Args:
        ticker: stock ticker
        filing_date: filing date
        accession_no: accession number

    Returns:
        str: safe filename
    """
    safe_ticker = str(ticker).upper()
    safe_date = str(filing_date).replace(":", "-")
    safe_accession = str(accession_no).replace("/", "_")

    return f"{safe_ticker}_{safe_date}_{safe_accession}.txt"


def save_raw_filing_text(text: str, ticker: str, filing_date: str, accession_no: str) -> str:
    """
    save raw filing text to disk

    Args:
        text: raw filing text
        ticker: stock ticker
        filing_date: filing date
        accession_no: accession number

    Returns:
        str: saved filename
    """
    filename = make_raw_text_filename(ticker, filing_date, accession_no)
    outpath = EDGAR_RAW_DIR / filename
    outpath.write_text(text, encoding="utf-8")

    return filename


def summarize_filing_categories(labels: List[str]) -> Dict[str, int]:
    """
    count labels inside one filing

    Args:
        labels: semantic labels for filing paragraphs

    Returns:
        Dict[str, int]: label counts
    """
    if not labels:
        return {}

    return pd.Series(labels).value_counts(dropna=False).to_dict()


def main() -> None:
    """
    run the full 8-k ingestion and scoring pipeline

    Args:
        none

    Returns:
        None
    """
    args = parse_args()

    # set sec identity before calling edgar
    set_identity(args.identity)

    # make sure all needed dirs exist
    for path in [
        LM_DIR,
        EDGAR_RAW_DIR,
        FILINGS_PROCESSED_DIR,
        FEATURES_DIR,
        TABLES_DIR,
        FIGURES_DIR,
        LOGS_DIR,
    ]:
        path.mkdir(parents=True, exist_ok=True)

    lm_path = Path(args.lm_dictionary_csv)
    taxonomy_path = Path(args.taxonomy)

    # hard fail if key input files are missing
    if not lm_path.exists():
        raise FileNotFoundError(f"LM dictionary not found: {lm_path}")

    if not taxonomy_path.exists():
        raise FileNotFoundError(f"Taxonomy file not found: {taxonomy_path}")

    # load cfg pieces
    categories = load_taxonomy(str(taxonomy_path))
    lm_word_map = load_lm_dictionary(str(lm_path))
    vectorizer, profile_matrix, category_names = build_category_profiles(categories)

    # containers for final dfs
    filing_rows = []
    paragraph_rows = []

    # loop over each ticker
    for ticker in args.tickers:
        try:
            filings = get_company_8k_filings(
                ticker=ticker,
                start_date=args.start_date,
                end_date=args.end_date,
                max_filings=args.max_filings_per_ticker,
            )
        except Exception as exc:
            print(f"[WARN] Could not retrieve filings for {ticker}: {exc}")
            continue

        print(f"{ticker}: pulled {len(filings)} filings")

        # loop over filings for the ticker
        for filing in filings:
            text = extract_filing_text(filing)

            # skip empty text extractions
            if not text:
                continue

            filing_date = str(getattr(filing, "filing_date", None))
            accession_no = str(getattr(filing, "accession_no", None))

            raw_text_file = None

            # optionally save raw txt version
            if args.save_raw_text:
                raw_text_file = save_raw_filing_text(
                    text=text,
                    ticker=ticker,
                    filing_date=filing_date,
                    accession_no=accession_no,
                )

            # split filing into paragraphs
            paragraphs = split_paragraphs(text, min_chars=args.min_paragraph_chars)

            # classify each paragraph semantically
            labels, label_scores = classify_semantic(
                paragraphs,
                vectorizer,
                profile_matrix,
                category_names,
                min_similarity=args.semantic_threshold,
            )

            # compute lm scores for full filing
            filing_lm = lm_score_text(text, lm_word_map)

            # summarize which category dominates the filing
            category_counts = summarize_filing_categories(labels)
            top_category = max(category_counts, key=category_counts.get) if category_counts else None
            top_category_share = (
                category_counts[top_category] / len(labels)
                if category_counts and len(labels) > 0
                else None
            )

            # append filing-level row
            filing_rows.append(
                {
                    "ticker": ticker,
                    "company": getattr(filing, "company", None),
                    "cik": getattr(filing, "cik", None),
                    "form": getattr(filing, "form", None),
                    "filing_date": filing_date,
                    "accession_no": accession_no,
                    "paragraph_count": len(paragraphs),
                    "top_semantic_category": top_category,
                    "top_semantic_category_share": top_category_share,
                    "raw_text_file": raw_text_file,
                    **filing_lm,
                }
            )

            # append paragraph-level rows
            for i, paragraph in enumerate(paragraphs):
                para_lm = lm_score_text(paragraph, lm_word_map)

                paragraph_rows.append(
                    {
                        "ticker": ticker,
                        "company": getattr(filing, "company", None),
                        "cik": getattr(filing, "cik", None),
                        "form": getattr(filing, "form", None),
                        "filing_date": filing_date,
                        "accession_no": accession_no,
                        "paragraph_id": i,
                        "semantic_category": labels[i] if i < len(labels) else None,
                        "semantic_score": label_scores[i] if i < len(label_scores) else None,
                        **para_lm,
                        "paragraph_text": paragraph,
                    }
                )

    # build final dfs from collected rows
    filings_df = pd.DataFrame(filing_rows)
    paragraphs_df = pd.DataFrame(paragraph_rows)

    if not filings_df.empty:
        # convert filing date and save filing-level outputs
        filings_df["filing_date"] = pd.to_datetime(
            filings_df["filing_date"], errors="coerce"
        )

        filings_df.to_parquet(
            FILINGS_PROCESSED_DIR / "filings_level.parquet",
            index=False,
        )

        filings_df.to_csv(
            TABLES_DIR / "filings_level.csv",
            index=False,
        )

    if not paragraphs_df.empty:
        # convert filing date and save paragraph-level outputs
        paragraphs_df["filing_date"] = pd.to_datetime(
            paragraphs_df["filing_date"], errors="coerce"
        )

        paragraphs_df.to_parquet(
            FEATURES_DIR / "paragraph_level.parquet",
            index=False,
        )

        paragraphs_df.to_csv(
            TABLES_DIR / "paragraph_level.csv",
            index=False,
        )

        # create a simple monthly category time series
        ts = (
            paragraphs_df.assign(
                month=lambda df: df["filing_date"].dt.to_period("M").astype(str)
            )
            .groupby(["month", "semantic_category"], dropna=False)
            .agg(
                paragraph_count=("paragraph_id", "count"),
                avg_semantic_score=("semantic_score", "mean"),
                avg_lm_negative=("lm_negative", "mean"),
                avg_lm_uncertainty=("lm_uncertainty", "mean"),
            )
            .reset_index()
            .sort_values(["month", "paragraph_count"], ascending=[True, False])
        )

        ts.to_csv(TABLES_DIR / "category_timeseries.csv", index=False)

    # save run metadata for traceability
    run_metadata = {
        "timestamp_utc": datetime.now(UTC).isoformat(),
        "tickers": args.tickers,
        "start_date": args.start_date,
        "end_date": args.end_date,
        "max_filings_per_ticker": args.max_filings_per_ticker,
        "min_paragraph_chars": args.min_paragraph_chars,
        "semantic_threshold": args.semantic_threshold,
        "taxonomy": str(taxonomy_path),
        "lm_dictionary_csv": str(lm_path),
        "save_raw_text": args.save_raw_text,
        "n_filings": int(len(filings_df)),
        "n_paragraphs": int(len(paragraphs_df)),
    }

    with open(LOGS_DIR / "last_run_metadata.json", "w", encoding="utf-8") as f:
        json.dump(run_metadata, f, indent=2)

    # print summary paths in terminal
    print(f"Processed {len(filings_df)} filings and {len(paragraphs_df)} paragraphs")
    print(f"Processed parquet saved under: {PROCESSED_DIR.resolve()}")
    print(f"Reporting tables saved under: {TABLES_DIR.resolve()}")
    print(f"Logs saved under: {LOGS_DIR.resolve()}")


if __name__ == "__main__":
    main()