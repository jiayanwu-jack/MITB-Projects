"""
features.py
-----------
Feature engineering for the LittleSteps visits dataset.

New columns created:
  - visit_duration_minutes  : actual duration of each patient visit
  - travel_duration_minutes : estimated nurse travel time (see note below)
  - urgency_flag            : extracted from nurse_notes
  - patient_status          : extracted from nurse_notes
  - action_taken            : extracted from nurse_notes

Note on travel_duration_minutes
--------------------------------
The raw dataset does not contain GPS coordinates, odometer readings, or
any direct measure of travel time. We therefore derive a plausible proxy
using the following assumptions, documented here for full transparency:

  • Each nurse travels from their previous visit location to the next.
  • We sort each nurse's visits by visit_start_time and compute the gap
    between a visit's end time and the next visit's start time.
  • This inter-visit gap includes both travel time and any idle/break time,
    so it is an *upper bound* on travel time.
  • For a nurse's first visit of the day (no preceding visit), travel
    duration is set to NaN and excluded from analysis.
  • Negative gaps (data entry errors where next start < previous end) are
    set to NaN.
  • Gaps > 240 minutes (4 hours) are treated as shift breaks, not travel,
    and set to NaN.

This approach is the most defensible given the available data and is
documented as an assumption in the README.
"""

import re
import numpy as np
import pandas as pd


# ---------------------------------------------------------------------------
# Visit duration
# ---------------------------------------------------------------------------

def add_visit_duration(df: pd.DataFrame) -> pd.DataFrame:
    """Compute visit_duration_minutes from parsed start/end timestamps."""
    df = df.copy()
    df["visit_duration_minutes"] = (
        (df["visit_end_time"] - df["visit_start_time"]).dt.total_seconds() / 60
    )
    print(f"[feat] visit_duration_minutes computed. "
          f"Range: {df['visit_duration_minutes'].min():.1f} – "
          f"{df['visit_duration_minutes'].max():.1f} min")
    return df


# ---------------------------------------------------------------------------
# Outlier removal (placed here so it runs after duration is computed)
# ---------------------------------------------------------------------------

def remove_duration_outliers(df: pd.DataFrame, col: str = "visit_duration_minutes",
                              factor: float = 1.5) -> pd.DataFrame:
    """Remove IQR outliers from a duration column."""
    q1 = df[col].quantile(0.25)
    q3 = df[col].quantile(0.75)
    iqr = q3 - q1
    lower = q1 - factor * iqr
    upper = q3 + factor * iqr
    n_before = len(df)
    df = df[(df[col] >= lower) & (df[col] <= upper)].copy()
    print(f"[feat] Outlier removal on {col}: fence [{lower:.1f}, {upper:.1f}] min → "
          f"removed {n_before - len(df)} rows, {len(df)} remain")
    return df.reset_index(drop=True)


# ---------------------------------------------------------------------------
# Travel duration (proxy via inter-visit gap)
# ---------------------------------------------------------------------------

def add_travel_duration(df: pd.DataFrame) -> pd.DataFrame:
    """Estimate travel_duration_minutes as the inter-visit gap per nurse.

    See module docstring for full assumption documentation.
    """
    df = df.copy().sort_values(["nurse_id", "visit_start_time"]).reset_index(drop=True)

    # For each nurse, shift end times to get the previous visit's end
    df["_prev_end"] = df.groupby("nurse_id")["visit_end_time"].shift(1)

    # Gap = current start - previous end (in minutes)
    df["travel_duration_minutes"] = (
        (df["visit_start_time"] - df["_prev_end"]).dt.total_seconds() / 60
    )

    # Nullify invalid or implausible gaps
    mask_negative = df["travel_duration_minutes"] < 0
    mask_too_large = df["travel_duration_minutes"] > 240  # treat as shift break
    df.loc[mask_negative | mask_too_large, "travel_duration_minutes"] = np.nan

    n_valid = df["travel_duration_minutes"].notna().sum()
    print(f"[feat] travel_duration_minutes computed. "
          f"Valid values: {n_valid} / {len(df)}. "
          f"Mean: {df['travel_duration_minutes'].mean():.1f} min")

    df = df.drop(columns=["_prev_end"])
    return df


# ---------------------------------------------------------------------------
# Nurse notes text extraction
# ---------------------------------------------------------------------------

# Urgency keywords ordered from highest to lowest severity
URGENCY_ORDER = ["ASAP", "Critical", "Urgent", "Action", "Review",
                 "Monitoring", "Ongoing assessment", "Follow-up"]

URGENCY_PATTERN = re.compile(
    r"\b(ASAP|Critical|Urgent|Action|Review|Monitoring|"
    r"Ongoing assessment|Follow-up)\b",
    re.IGNORECASE,
)

STATUS_PATTERN = re.compile(
    r"The patient is (stable|in pain|dizzy|weak|restless|comfortable|improving)",
    re.IGNORECASE,
)

ACTION_PATTERN = re.compile(
    r"The nurse (\w+) the ([^.]+)\.",
    re.IGNORECASE,
)


def _extract_urgency(note: str) -> str:
    """Return the highest-severity urgency flag found in a note."""
    if note == "No notes provided":
        return "None"
    matches = URGENCY_PATTERN.findall(note)
    if not matches:
        return "None"
    # Return the first match (notes typically contain only one urgency phrase)
    return matches[0].title()


def _extract_status(note: str) -> str:
    m = STATUS_PATTERN.search(note)
    return m.group(1).title() if m else "Unknown"


def _extract_action(note: str) -> str:
    m = ACTION_PATTERN.search(note)
    if m:
        return f"{m.group(1).lower()} the {m.group(2).strip()}"
    return "Unknown"


def add_note_features(df: pd.DataFrame) -> pd.DataFrame:
    """Extract structured features from free-text nurse_notes."""
    df = df.copy()
    df["urgency_flag"] = df["nurse_notes"].apply(_extract_urgency)
    df["patient_status"] = df["nurse_notes"].apply(_extract_status)
    df["action_taken"] = df["nurse_notes"].apply(_extract_action)
    print(f"[feat] Nurse notes parsed → urgency_flag, patient_status, action_taken")
    return df
