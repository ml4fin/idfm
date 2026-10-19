# EDGAR 8-K Risk Pipeline (Starter)

This starter pipeline does four things:
1. Downloads recent 8-K filings for a list of firms using the library `edgartools`.
2. Extracts filing text and splits it into paragraphs.
3. Scores each paragraph with the Loughran-McDonald dictionary.
4. Assigns a first-pass semantic risk label using TF-IDF cosine similarity against a user-defined taxonomy.

## Fast setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Example run

```bash
python edgar_risk_pipeline.py \
  --tickers AAPL MSFT NVDA META JPM \
  --start-date 2024-01-01 \
  --end-date 2026-04-16 \
  --lm-dictionary-csv /path/to/Loughran-McDonald_MasterDictionary_1993-2025.csv \
  --taxonomy risk_taxonomy.yml \
  --output-dir outputs
```

## Outputs

- `outputs/filings_level.parquet`: one row per filing
- `outputs/paragraph_level.parquet`: one row per paragraph
- `outputs/category_timeseries.csv`: monthly risk-category counts/intensity

## Suggested talking points for your call

- The pipeline targets the disclosure channel in the project outline, especially 8-Ks as event-driven disclosures.
- The first layer is dictionary-based and reproducible (Loughran-McDonald).
- The second layer is semantic categorization that can later be upgraded to weak supervision, sentence embeddings, or a fine-tuned classifier.
- The natural next step is to connect 8-K category diffusion to 10-K Item 1A adoption and estimate lead-lag dynamics.
