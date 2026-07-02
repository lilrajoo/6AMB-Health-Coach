import io
from datetime import datetime, timedelta
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import numpy as np


def build_calorie_graph(calorie_rows):
    if not calorie_rows:
        return None

    today       = datetime.now().date()
    this_monday = today - timedelta(days=today.weekday())
    week_starts = [this_monday - timedelta(weeks=i) for i in range(4, -1, -1)]
    week_data   = {ws: [] for ws in week_starts}

    for date_str, calories in calorie_rows:
        try:
            entry_date = datetime.strptime(date_str, "%Y-%m-%d").date()
        except ValueError:
            continue
        for ws in week_starts:
            if ws <= entry_date <= ws + timedelta(days=6):
                week_data[ws].append(calories)
                break

    week_labels, week_avgs, has_data = [], [], []
    for i, ws in enumerate(week_starts):
        values = week_data[ws]
        week_labels.append(ws.strftime('%d %b'))
        if not values:
            week_avgs.append(None)
            has_data.append(False)
        else:
            divisor = (today.weekday() + 1) if i == len(week_starts) - 1 else 7
            week_avgs.append(round(sum(values) / divisor, 1))
            has_data.append(True)

    fig, ax = plt.subplots(figsize=(10, 6))
    fig.patch.set_facecolor("#1e1e2e")
    ax.set_facecolor("#2a2a3e")
    x_positions = list(range(len(week_labels)))

    for i, (avg, _) in enumerate(zip(week_avgs, week_labels)):
        if avg is not None:
            ax.bar(i, avg, color="#7c6af7", width=0.6, zorder=3)
            ax.text(i, avg + 20, f"{avg:.0f}", ha="center", va="bottom",
                    color="white", fontsize=10, fontweight="bold")

    trend_x = [i for i, h in enumerate(has_data) if h]
    trend_y = [a for a, h in zip(week_avgs, has_data) if h]
    if len(trend_x) >= 2:
        p = np.poly1d(np.polyfit(trend_x, trend_y, 1))
        ax.plot(np.linspace(trend_x[0], trend_x[-1], 100),
                p(np.linspace(trend_x[0], trend_x[-1], 100)),
                color="#f7a76a", linewidth=2, linestyle="--", label="Trend", zorder=4)
        ax.legend(facecolor="#2a2a3e", labelcolor="white", fontsize=10)

    ax.set_xticks(x_positions)
    ax.set_xticklabels(week_labels, color="white", fontsize=10)
    ax.set_ylabel("Avg Daily Calories (kcal)", color="white", fontsize=11)
    ax.set_xlabel("Week Starting", color="white", fontsize=11)
    ax.set_title("Average Daily Calories Per Week\n(Last 4 Weeks + Current)",
                 color="white", fontsize=13, fontweight="bold", pad=15)
    ax.tick_params(colors="white")
    ax.yaxis.label.set_color("white")
    for spine in ["top", "right"]: ax.spines[spine].set_visible(False)
    for spine in ["bottom", "left"]: ax.spines[spine].set_color("#555577")
    ax.grid(axis="y", color="#555577", linestyle="--", alpha=0.5, zorder=0)
    plt.tight_layout()

    buf = io.BytesIO()
    plt.savefig(buf, format="png", dpi=150, bbox_inches="tight")
    plt.close(fig)
    buf.seek(0)
    return buf


def build_weight_graph(weight_rows):
    if not weight_rows:
        return None

    recent         = weight_rows[-5:]
    dates, weights = [], []
    for date_str, w in recent:
        try:
            dates.append(datetime.strptime(date_str, "%Y-%m-%d"))
            weights.append(w)
        except ValueError:
            continue

    if not dates:
        return None

    fig, ax = plt.subplots(figsize=(10, 6))
    fig.patch.set_facecolor("#1e1e2e")
    ax.set_facecolor("#2a2a3e")

    ax.plot(dates, weights, color="#7c6af7", linewidth=2.5, marker="o",
            markersize=8, markerfacecolor="#f7a76a",
            markeredgecolor="white", markeredgewidth=1.5, zorder=3, label="Weight")

    for d, w in zip(dates, weights):
        ax.text(d, w + 0.3, f"{w} kg", ha="center", va="bottom",
                color="white", fontsize=10, fontweight="bold")

    if len(dates) >= 2:
        x_num   = mdates.date2num(dates)
        p       = np.poly1d(np.polyfit(x_num, weights, 1))
        x_range = np.linspace(x_num[0], x_num[-1], 100)
        ax.plot(mdates.num2date(x_range), p(x_range),
                color="#f7a76a", linewidth=2, linestyle="--", label="Trend", zorder=4)

    ax.xaxis.set_major_formatter(mdates.DateFormatter("%d %b"))
    plt.xticks(rotation=30, ha="right", color="white", fontsize=10)
    ax.set_ylabel("Weight (kg)", color="white", fontsize=11)
    ax.set_xlabel("Date", color="white", fontsize=11)
    ax.set_title("Last 5 Weight Entries", color="white", fontsize=13, fontweight="bold", pad=15)
    ax.tick_params(colors="white")
    for spine in ["top", "right"]: ax.spines[spine].set_visible(False)
    for spine in ["bottom", "left"]: ax.spines[spine].set_color("#555577")
    ax.grid(axis="y", color="#555577", linestyle="--", alpha=0.5, zorder=0)
    ax.legend(facecolor="#2a2a3e", labelcolor="white", fontsize=10)
    plt.tight_layout()

    buf = io.BytesIO()
    plt.savefig(buf, format="png", dpi=150, bbox_inches="tight")
    plt.close(fig)
    buf.seek(0)
    return buf