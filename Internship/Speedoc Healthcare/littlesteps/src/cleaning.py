"""
cleaning.py
-----------
Data cleaning and preprocessing pipeline for the LittleSteps visits dataset.

Steps:
  1. Remove duplicate visit_id records
  2. Standardise datetime columns (multiple mixed formats)
  3. Correct typos in categorical columns (service_type, visit_location)
  4. Drop rows where visit_end_time is missing (duration cannot be computed)
  5. Remove duration outliers using the IQR method
"""

import re
import pandas as pd
from dateutil import parser as dtparser


# ---------------------------------------------------------------------------
# Typo correction maps (discovered during initial exploration)
# ---------------------------------------------------------------------------

SERVICE_TYPE_MAP = {
    "Pyhcisal Therapy": "Physical Therapy",
    "Wound Cae": "Wound Care",
    "General Chek-up": "General Check-up",
    "Medicatn Adminstratino": "Medication Administration",
}

LOCATION_MAP = {
    "Notrh": "North",
    "Easst": "East",
    "Wsst": "West",
    "Soutth": "South",
}


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------

def _safe_parse_dt(value) -> pd.Timestamp | None:
    """Parse a datetime string in any of the formats present in the dataset.

    Formats observed:
        - 2025-09-02 00:08:24   (ISO-like, most common)
        - 2025/09/02 11:56      (slash-separated, no seconds)
        - 09/15/2025 18:28      (US style MM/DD/YYYY)
        - August 24, 2025 10:42PM  (verbose natural language)
        - September 07, 2025 08:29PM
    """
    if pd.isna(value):
        return None
    try:
        return dtparser.parse(str(value).strip())
    except (ValueError, OverflowError):
        return None


def _remove_iqr_outliers(df: pd.DataFrame, col: str, factor: float = 1.5) -> pd.DataFrame:
    """Drop rows where *col* falls outside [Q1 - factor*IQR, Q3 + factor*IQR].

    For clinical visit durations the standard 1.5× IQR fence is appropriate
    because it preserves legitimately long visits (e.g., complex wound care)
    while removing implausibly short (<1 min) or long (>8 h) records caused
    by data entry errors.
    """
    q1 = df[col].quantile(0.25)
    q3 = df[col].quantile(0.75)
    iqr = q3 - q1
    lower = q1 - factor * iqr
    upper = q3 + factor * iqr
    before = len(df)
    df = df[(df[col] >= lower) & (df[col] <= upper)].copy()
    removed = before - len(df)
    print(f"  [outlier removal] {col}: IQR fence [{lower:.1f}, {upper:.1f}] min "
          f"→ removed {removed} rows")
    return df


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

def load_raw(path: str) -> pd.DataFrame:
    """Load raw CSV and return a DataFrame."""
    df = pd.read_csv(path, dtype=str)
    print(f"Loaded {len(df):,} rows × {df.shape[1]} columns from '{path}'")
    return df


def clean(df: pd.DataFrame) -> pd.DataFrame:
    """Full cleaning pipeline. Returns a cleaned DataFrame."""

    df = df.copy()

    # ------------------------------------------------------------------
    # 1. Remove duplicate visit_ids (keep first occurrence)
    # ------------------------------------------------------------------
    n_before = len(df)
    df = df.drop_duplicates(subset=["visit_id"], keep="first").reset_index(drop=True)
    print(f"[1] Duplicates removed: {n_before - len(df)} rows (keeping first occurrence)")

    # ------------------------------------------------------------------
    # 2. Parse and standardise datetime columns
    # ------------------------------------------------------------------
    for col in ["visit_start_time", "visit_end_time"]:
        df[col] = df[col].apply(_safe_parse_dt)
        df[col] = pd.to_datetime(df[col])
    print(f"[2] Datetimes parsed. Missing visit_end_time: {df['visit_end_time'].isna().sum()}")

    # ------------------------------------------------------------------
    # 3. Drop rows missing visit_end_time
    #    Justification: without an end time, visit_duration_minutes cannot
    #    be calculated and imputation would introduce systematic bias.
    # ------------------------------------------------------------------
    n_before = len(df)
    df = df.dropna(subset=["visit_end_time"]).reset_index(drop=True)
    print(f"[3] Dropped {n_before - len(df)} rows with missing visit_end_time")

    # ------------------------------------------------------------------
    # 4. Fix categorical typos
    # ------------------------------------------------------------------
    df["service_type"] = df["service_type"].replace(SERVICE_TYPE_MAP)
    df["visit_location"] = df["visit_location"].replace(LOCATION_MAP)
    print(f"[4] Categorical typos corrected")
    print(f"    service_type values: {sorted(df['service_type'].unique())}")
    print(f"    visit_location values: {sorted(df['visit_location'].unique())}")

    # ------------------------------------------------------------------
    # 5. Handle missing nurse_notes
    #    Justification: notes are supplementary text; we retain the row
    #    but mark the absence explicitly for downstream text analysis.
    # ------------------------------------------------------------------
    n_missing_notes = df["nurse_notes"].isna().sum()
    df["nurse_notes"] = df["nurse_notes"].fillna("No notes provided")
    print(f"[5] nurse_notes: {n_missing_notes} missing → filled with 'No notes provided'")

    print(f"\nCleaning complete. Final shape before outlier removal: {df.shape}")
    return df
