"""
visualisations.py
-----------------
All plot-generating functions for the LittleSteps analysis.

Each function saves its figure to *output_dir* and also returns the
matplotlib Figure so it can be embedded in a notebook.
"""

import os
import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
import seaborn as sns

# ---------------------------------------------------------------------------
# Global style
# ---------------------------------------------------------------------------

PALETTE = "Blues_d"
ACCENT = "#2471A3"
WARN_COLOR = "#E74C3C"
OK_COLOR = "#27AE60"

plt.rcParams.update({
    "figure.dpi": 120,
    "axes.spines.top": False,
    "axes.spines.right": False,
    "axes.titlesize": 13,
    "axes.labelsize": 11,
    "xtick.labelsize": 10,
    "ytick.labelsize": 10,
    "legend.fontsize": 10,
    "font.family": "DejaVu Sans",
})


def _save(fig: plt.Figure, output_dir: str, filename: str) -> str:
    os.makedirs(output_dir, exist_ok=True)
    path = os.path.join(output_dir, filename)
    fig.savefig(path, bbox_inches="tight")
    print(f"  Saved: {path}")
    return path


# ---------------------------------------------------------------------------
# 1. Histogram of visit duration (before vs after outlier removal)
# ---------------------------------------------------------------------------

def plot_duration_histogram(df_raw: pd.DataFrame, df_clean: pd.DataFrame,
                             output_dir: str) -> plt.Figure:
    """Side-by-side histograms showing the effect of outlier removal."""
    fig, axes = plt.subplots(1, 2, figsize=(12, 4), sharey=False)

    for ax, df, title, color in zip(
        axes,
        [df_raw, df_clean],
        ["Before Outlier Removal", "After Outlier Removal (IQR)"],
        [WARN_COLOR, OK_COLOR],
    ):
        ax.hist(df["visit_duration_minutes"].dropna(), bins=40,
                color=color, edgecolor="white", alpha=0.85)
        ax.set_title(title, fontweight="bold")
        ax.set_xlabel("Visit Duration (minutes)")
        ax.set_ylabel("Count")
        mean_val = df["visit_duration_minutes"].mean()
        ax.axvline(mean_val, color="black", linestyle="--", linewidth=1.2,
                   label=f"Mean: {mean_val:.1f} min")
        ax.legend()

    fig.suptitle("Distribution of Visit Duration (minutes)", fontsize=14, fontweight="bold", y=1.02)
    fig.tight_layout()
    _save(fig, output_dir, "01_duration_histogram.png")
    return fig


# ---------------------------------------------------------------------------
# 2. Average visit duration by service type
# ---------------------------------------------------------------------------

def plot_avg_duration_by_service(df: pd.DataFrame, output_dir: str) -> plt.Figure:
    avg = (df.groupby("service_type")["visit_duration_minutes"]
             .mean()
             .sort_values(ascending=False)
             .reset_index())
    avg.columns = ["service_type", "avg_duration"]

    fig, ax = plt.subplots(figsize=(9, 4))
    bars = ax.barh(avg["service_type"], avg["avg_duration"],
                   color=sns.color_palette("Blues_r", len(avg)),
                   edgecolor="white")

    # Annotate bars
    for bar, val in zip(bars, avg["avg_duration"]):
        ax.text(val + 0.5, bar.get_y() + bar.get_height() / 2,
                f"{val:.1f} min", va="center", ha="left", fontsize=9)

    ax.set_xlabel("Average Visit Duration (minutes)")
    ax.set_title("Average Visit Duration by Service Type", fontweight="bold")
    ax.invert_yaxis()
    fig.tight_layout()
    _save(fig, output_dir, "02_avg_duration_by_service.png")
    return fig


# ---------------------------------------------------------------------------
# 3. Boxplot of visit duration by service type
# ---------------------------------------------------------------------------

def plot_boxplot_by_service(df: pd.DataFrame, output_dir: str) -> plt.Figure:
    order = (df.groupby("service_type")["visit_duration_minutes"]
               .median()
               .sort_values(ascending=False)
               .index.tolist())

    fig, ax = plt.subplots(figsize=(10, 5))
    sns.boxplot(data=df, x="service_type", y="visit_duration_minutes",
                order=order, palette="Blues", ax=ax,
                flierprops=dict(marker="o", markersize=3, alpha=0.4))
    ax.set_xlabel("Service Type")
    ax.set_ylabel("Visit Duration (minutes)")
    ax.set_title("Distribution of Visit Duration by Service Type", fontweight="bold")
    plt.xticks(rotation=20, ha="right")
    fig.tight_layout()
    _save(fig, output_dir, "03_boxplot_by_service.png")
    return fig


# ---------------------------------------------------------------------------
# 4. Average visit duration by location
# ---------------------------------------------------------------------------

def plot_avg_duration_by_location(df: pd.DataFrame, output_dir: str) -> plt.Figure:
    avg = (df.groupby("visit_location")["visit_duration_minutes"]
             .mean()
             .sort_values(ascending=False)
             .reset_index())
    avg.columns = ["visit_location", "avg_duration"]
    overall_mean = df["visit_duration_minutes"].mean()

    fig, ax = plt.subplots(figsize=(7, 4))
    colors = [WARN_COLOR if v > overall_mean else ACCENT for v in avg["avg_duration"]]
    bars = ax.bar(avg["visit_location"], avg["avg_duration"],
                  color=colors, edgecolor="white", width=0.6)
    ax.axhline(overall_mean, color="black", linestyle="--", linewidth=1.2,
               label=f"Overall mean: {overall_mean:.1f} min")

    for bar, val in zip(bars, avg["avg_duration"]):
        ax.text(bar.get_x() + bar.get_width() / 2, val + 0.5,
                f"{val:.1f}", ha="center", va="bottom", fontsize=9)

    ax.set_xlabel("Visit Location Zone")
    ax.set_ylabel("Average Visit Duration (minutes)")
    ax.set_title("Average Visit Duration by Location Zone", fontweight="bold")
    ax.legend()
    fig.tight_layout()
    _save(fig, output_dir, "04_avg_duration_by_location.png")
    return fig


# ---------------------------------------------------------------------------
# 5. Boxplot of visit duration by location (statistical comparison)
# ---------------------------------------------------------------------------

def plot_boxplot_by_location(df: pd.DataFrame, output_dir: str) -> plt.Figure:
    fig, ax = plt.subplots(figsize=(8, 4))
    sns.boxplot(data=df, x="visit_location", y="visit_duration_minutes",
                palette="Set2", ax=ax,
                flierprops=dict(marker="o", markersize=3, alpha=0.4))
    ax.set_xlabel("Visit Location Zone")
    ax.set_ylabel("Visit Duration (minutes)")
    ax.set_title("Visit Duration Distribution by Location Zone", fontweight="bold")
    fig.tight_layout()
    _save(fig, output_dir, "05_boxplot_by_location.png")
    return fig


# ---------------------------------------------------------------------------
# 6. Top 3 and bottom 3 nurses by average travel duration
# ---------------------------------------------------------------------------

def plot_nurse_travel(df: pd.DataFrame, output_dir: str) -> plt.Figure:
    nurse_avg = (df.groupby("nurse_id")["travel_duration_minutes"]
                   .mean()
                   .dropna()
                   .sort_values(ascending=False))

    top3 = nurse_avg.head(3)
    bot3 = nurse_avg.tail(3)
    overall_mean = nurse_avg.mean()

    fig, axes = plt.subplots(1, 2, figsize=(12, 4))

    for ax, data, title, color in zip(
        axes,
        [top3, bot3],
        ["Top 3 Nurses — Longest Avg Travel", "Bottom 3 Nurses — Shortest Avg Travel"],
        [WARN_COLOR, OK_COLOR],
    ):
        bars = ax.barh(data.index.astype(str), data.values, color=color,
                       edgecolor="white", alpha=0.85)
        ax.axvline(overall_mean, color="black", linestyle="--", linewidth=1,
                   label=f"Fleet mean: {overall_mean:.1f} min")
        for bar, val in zip(bars, data.values):
            ax.text(val + 0.3, bar.get_y() + bar.get_height() / 2,
                    f"{val:.1f} min", va="center", fontsize=9)
        ax.set_xlabel("Avg Travel Duration (minutes)")
        ax.set_title(title, fontweight="bold")
        ax.legend(fontsize=8)

    fig.suptitle("Nurse Travel Duration Ranking", fontsize=13, fontweight="bold", y=1.02)
    fig.tight_layout()
    _save(fig, output_dir, "06_nurse_travel_ranking.png")
    return fig


# ---------------------------------------------------------------------------
# 7. Patient status distribution (from nurse notes)
# ---------------------------------------------------------------------------

def plot_patient_status(df: pd.DataFrame, output_dir: str) -> plt.Figure:
    counts = df["patient_status"].value_counts()

    fig, ax = plt.subplots(figsize=(8, 4))
    bars = ax.bar(counts.index, counts.values,
                  color=sns.color_palette("RdYlGn_r", len(counts)),
                  edgecolor="white")
    for bar, val in zip(bars, counts.values):
        ax.text(bar.get_x() + bar.get_width() / 2, val + 1,
                str(val), ha="center", va="bottom", fontsize=9)
    ax.set_xlabel("Patient Status (from nurse notes)")
    ax.set_ylabel("Count")
    ax.set_title("Patient Status Distribution Inferred from Nurse Notes", fontweight="bold")
    plt.xticks(rotation=15, ha="right")
    fig.tight_layout()
    _save(fig, output_dir, "07_patient_status.png")
    return fig


# ---------------------------------------------------------------------------
# 8. Urgency flag vs average visit duration (heatmap-style bar chart)
# ---------------------------------------------------------------------------

def plot_urgency_vs_duration(df: pd.DataFrame, output_dir: str) -> plt.Figure:
    urgency_order = ["ASAP", "Critical", "Urgent", "Action", "Review",
                     "Monitoring", "Ongoing Assessment", "Follow-Up", "None"]
    avg = (df[df["urgency_flag"] != "None"]
             .groupby("urgency_flag")["visit_duration_minutes"]
             .mean()
             .reindex([u for u in urgency_order if u in df["urgency_flag"].unique()])
             .dropna())

    fig, ax = plt.subplots(figsize=(9, 4))
    colors = sns.color_palette("Reds_r", len(avg))
    bars = ax.barh(avg.index.astype(str), avg.values, color=colors, edgecolor="white")
    for bar, val in zip(bars, avg.values):
        ax.text(val + 0.3, bar.get_y() + bar.get_height() / 2,
                f"{val:.1f} min", va="center", fontsize=9)
    ax.set_xlabel("Average Visit Duration (minutes)")
    ax.set_title("Average Visit Duration by Urgency Flag (from nurse notes)",
                 fontweight="bold")
    ax.invert_yaxis()
    fig.tight_layout()
    _save(fig, output_dir, "08_urgency_vs_duration.png")
    return fig


# ---------------------------------------------------------------------------
# 9. Travel vs visit duration scatter (nurse-level)
# ---------------------------------------------------------------------------

def plot_travel_vs_visit_scatter(df: pd.DataFrame, output_dir: str) -> plt.Figure:
    nurse_summary = df.groupby("nurse_id").agg(
        avg_visit=("visit_duration_minutes", "mean"),
        avg_travel=("travel_duration_minutes", "mean"),
        n_visits=("visit_id", "count"),
    ).dropna()

    fig, ax = plt.subplots(figsize=(7, 5))
    sc = ax.scatter(nurse_summary["avg_travel"], nurse_summary["avg_visit"],
                    s=nurse_summary["n_visits"] * 3,
                    c=nurse_summary["n_visits"], cmap="Blues",
                    edgecolors="white", linewidths=0.5, alpha=0.85)
    plt.colorbar(sc, ax=ax, label="Number of visits")
    ax.set_xlabel("Avg Travel Duration (minutes)")
    ax.set_ylabel("Avg Visit Duration (minutes)")
    ax.set_title("Nurse-Level: Avg Travel vs Avg Visit Duration\n(bubble size = visit count)",
                 fontweight="bold")
    fig.tight_layout()
    _save(fig, output_dir, "09_travel_vs_visit_scatter.png")
    return fig
