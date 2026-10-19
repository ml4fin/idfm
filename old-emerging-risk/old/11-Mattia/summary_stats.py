# imports
from __future__ import annotations

import argparse
from pathlib import Path

import matplotlib.pyplot as plt
import pandas as pd


def safe_read_csv(path: Path) -> pd.DataFrame:
    """"
    Helper function to read a CSV file from disk after verifying that it exists.

    Args:
    path : Path
        Filesystem path to the CSV file.
    
    Returns:
    pd.DataFrame
        df loaded from the .csv file.
    """
    if not path.exists():
        # raises an error if expected input is missing
        raise FileNotFoundError(f"Missing file: {path}")
    return pd.read_csv(path)


def main() -> None:
    """
    Main Helper function to build summary statistics from EDGAR pipeline outputs. 
    It reads the filing and paragraph level data, computes various summary statistics, 
    and saves them as CSV files and plots in the output/summary_statistics directory.
    """

    # command line parser 
    parser = argparse.ArgumentParser(
        description="Build summary statistics from EDGAR pipeline outputs."
    )
    parser.add_argument(
        "--base-dir",
        type=str,
        default=".",
        help="Path to 11-Mattia folder",
    )
    args = parser.parse_args()

    # setup directories to read inputs and save outputs
    base_dir = Path(args.base_dir).resolve()
    tables_dir = base_dir / "output" / "tables"
    summary_dir = base_dir / "output" / "summary_statistics"
    summary_dir.mkdir(parents=True, exist_ok=True)
    
    filings = safe_read_csv(tables_dir / "filings_level.csv")
    paragraphs = safe_read_csv(tables_dir / "paragraph_level.csv")

    # converts filing_date into datetime format
    filings["filing_date"] = pd.to_datetime(filings["filing_date"], errors="coerce") # errors="coerce" set to NaT for invalid dates
    paragraphs["filing_date"] = pd.to_datetime(paragraphs["filing_date"], errors="coerce")

    # removes rows w/ missing dates (if any)
    filings = filings.dropna(subset=["filing_date"]).copy()
    # same for paragraphs
    paragraphs = paragraphs.dropna(subset=["filing_date"]).copy()

    filings["month"] = filings["filing_date"].dt.to_period("M").astype(str)
    paragraphs["month"] = paragraphs["filing_date"].dt.to_period("M").astype(str)

    # 00. Headline metrics
    # Builds a compact summary table with:
    # - number of unique firms
    # - number of filings
    # - number of paragraphs
    # - first filing date in the sample
    # - last filing date in the sample
    headline = pd.DataFrame(
        {
            "metric": ["n_firms", "n_filings", "n_paragraphs", "date_min", "date_max"],
            "value": [
                filings["ticker"].nunique(),
                len(filings),
                len(paragraphs),
                filings["filing_date"].min(),
                filings["filing_date"].max(),
            ],
        }
    )
    headline.to_csv(summary_dir / "00_headline_metrics.csv", index=False)

    # 01. Filing counts
    filing_counts = (
        # groups obs by month & ticker
        filings.groupby(["month", "ticker"], dropna=False)
        # n of filings
        .size()
        # converts to standard df format
        .reset_index(name="filing_count")
        # sorts values by month & ticker
        .sort_values(["month", "ticker"])
    )
    filing_counts.to_csv(summary_dir / "01_filing_counts_by_month_and_ticker.csv", index=False)

    # 02. Category frequency
    category_freq = (
        paragraphs.groupby("semantic_category", dropna=False)
        .agg(
            paragraph_count=("paragraph_id", "count"),
            # avg similarity score of semantic classification
            avg_semantic_score=("semantic_score", "mean"),
            # avg LM negative tone in each cat
            avg_lm_negative=("lm_negative", "mean"),
            # avg LM uncertainty tone in each cat
            avg_lm_uncertainty=("lm_uncertainty", "mean"),
        )
        .reset_index()
        # from most to least frequent category
        .sort_values("paragraph_count", ascending=False)
    )
    category_freq.to_csv(summary_dir / "02_category_frequency.csv", index=False)

    # 03. Diffusion proxy
    firm_cat_month = (
        # keep only the fields needed to study whether a firm mentions a category in a month
        paragraphs[["month", "ticker", "semantic_category"]]
        .drop_duplicates()
    )

    total_firms = (
        # set of firms that filed at least one document
        filings.groupby("month")["ticker"]
        # distinct n of firms
        .nunique()
        .rename("total_firms")
        # to df
        .reset_index()
    )

    diffusion = (
        # for each month and risk cat...
        firm_cat_month.groupby(["month", "semantic_category"])["ticker"]
        # ...count how many unique firms mention that cat
        .nunique()
        .rename("firms_with_risk")
        .reset_index()
        # total number of active filing firms in that month
        .merge(total_firms, on="month")
    )
    # Computes the diffusion curve:
    # D_r(t) = firms disclosing category r at time t / total firms at time t.
    diffusion["D_r_t"] = diffusion["firms_with_risk"] / diffusion["total_firms"]
    diffusion.to_csv(summary_dir / "03_diffusion_curve_Dr.csv", index=False)

    # 04. Co-occurrence
    filing_category = (
        # retain identifiers needed to define a unique filing and its assigned cats
        paragraphs[["ticker", "filing_date", "accession_no", "semantic_category"]]
        .drop_duplicates()
        # create a binary indicator that category is present in the filing
        .assign(flag=1)
    )

    # creates a unique filing identifier by concatenating ticker, filing date, and accession number.
    # this is used as the row index in the filing-category matrix.
    filing_category["filing_key"] = (
        filing_category["ticker"].astype(str)
        + "|"
        + filing_category["filing_date"].astype(str)
        + "|"
        + filing_category["accession_no"].astype(str)
    )

    # builds a filing-by-category matrix:
    # - rows = filings
    # - columns = semantic categories
    # - value = 1 if the filing contains the category, 0 otherwise
    pivot = filing_category.pivot_table(
        index="filing_key",
        columns="semantic_category",
        values="flag",
        fill_value=0,
        aggfunc="max",
    )

    # matrix multiplication of the transposed binary matrix with itself
    # entry (i, j) counts how many filings contain both category i and category j
    cooccurrence = pivot.T.dot(pivot)
    cooccurrence.to_csv(summary_dir / "04_category_cooccurrence_matrix.csv")

    # 05. Example paragraphs
    examples = (
        paragraphs.sort_values(
            ["semantic_score", "lm_uncertainty", "lm_negative"],
            ascending=False,
        )
        .groupby("semantic_category", dropna=False)
        # keeps the top 3 examples per category.
        .head(3)
    )
    examples.to_csv(summary_dir / "05_example_paragraphs.csv", index=False)

    # 06. Filing overview
    filings.to_csv(summary_dir / "06_filing_level_overview.csv", index=False)

    # 07. Intensity metrics
    intensity = (
        paragraphs.groupby(["month", "semantic_category"])
        .agg(
            # measures avg LM negative tone within that category-month
            avg_lm_negative=("lm_negative", "mean"),
            # measures avg LM uncertainty tone within that category-month
            avg_lm_uncertainty=("lm_uncertainty", "mean"),
            # n of cat-labeled paragrpahs in that month
            paragraph_count=("paragraph_id", "count"),
        )
        .reset_index()
    )
    intensity.to_csv(summary_dir / "07_intensity_metrics.csv", index=False)

    # 08. Diffusion change (emergence proxy)
    # time evolution ordered within each cat
    diffusion_sorted = diffusion.sort_values(["semantic_category", "month"])
    # computes the first difference of D_r(t) within each category -> 
    # this is a crude proxy for sudden growth in disclosure diffusion.
    diffusion_sorted["delta_diffusion"] = (
        diffusion_sorted.groupby("semantic_category")["D_r_t"].diff()
    )
    diffusion_sorted.to_csv(summary_dir / "08_diffusion_change.csv", index=False)

    # 09. Emerging risk ranking
    emerging = (
        diffusion_sorted.groupby("semantic_category")["delta_diffusion"]
        # for each category compute the average month-to-month change in diffusion
        .mean()
        .reset_index()
        .sort_values("delta_diffusion", ascending=False)
    )
    emerging.to_csv(summary_dir / "09_emerging_risk_ranking.csv", index=False)

    # ---------- PLOTS ----------

    # Category frequency
    plt.figure(figsize=(10, 6))
    top_freq = category_freq.head(10) # retains top 10 most frequent cat
    plt.bar(top_freq["semantic_category"].astype(str), top_freq["paragraph_count"])
    plt.xticks(rotation=45, ha="right")
    plt.title("Category frequency")
    plt.tight_layout()
    plt.savefig(summary_dir / "plot_category_frequency.png", dpi=200)
    plt.close()

    # Diffusion curves
    top_categories = category_freq["semantic_category"].dropna().head(3)

    plt.figure(figsize=(10, 6))
    for cat in top_categories:
        sub = diffusion[diffusion["semantic_category"] == cat]
        plt.plot(sub["month"], sub["D_r_t"], marker="o", label=cat)

    plt.xticks(rotation=45)
    plt.legend()
    plt.title("Diffusion curves D_r(t)")
    plt.tight_layout()
    plt.savefig(summary_dir / "plot_diffusion_curves.png", dpi=200)
    plt.close()

    # LM tone
    lm_time = (
        paragraphs.groupby("month")
        .agg(
            avg_lm_negative=("lm_negative", "mean"),
            avg_lm_uncertainty=("lm_uncertainty", "mean"),
        )
        .reset_index()
    )

    plt.figure(figsize=(10, 6))
    plt.plot(lm_time["month"], lm_time["avg_lm_negative"], label="Negative")
    plt.plot(lm_time["month"], lm_time["avg_lm_uncertainty"], label="Uncertainty")
    plt.legend()
    plt.xticks(rotation=45)
    plt.title("LM tone over time")
    plt.tight_layout()
    plt.savefig(summary_dir / "plot_lm_tone.png", dpi=200)
    plt.close()

    print(f"Summary statistics saved to: {summary_dir}")


if __name__ == "__main__":
    main()