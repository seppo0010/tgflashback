import collections
import datetime
import io

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt
from matplotlib.cm import ScalarMappable
from matplotlib.colors import Normalize, LinearSegmentedColormap
from matplotlib.patches import Rectangle

from bot.config import PLAYER_CODES

# Chart chrome, taken from the categorical/ink reference palette (light mode —
# Telegram renders photos on a fixed light card regardless of chat theme).
SURFACE = "#fcfcfb"
INK_PRIMARY = "#0b0b0b"
INK_SECONDARY = "#52514e"
INK_MUTED = "#898781"
UNPLAYED = "#e1e0d9"  # no score: uncolored gray, no number

# Score heatmap.
SCORE_CMAP = LinearSegmentedColormap.from_list(
    "flashback_clamped",
    reversed([
        (1.0, "#008238"),    # Full green for perfect score
        (20/28, "#f7f775"),  # Yellow for a mediocre 20/28
        (13/28, "#f12c21"),  # Red for an indisputably bad 13/28
        (.0001, "#663D19"),  # 💩
        (0.0, "#E033F0"),    # Easter egg for pink cow contrarians
    ])
)
MAX_SCORE = 28

CELL_WIDTH_IN = 0.55  # figure inches per week column
CELL_HEIGHT_IN = 0.6  # figure inches per player row
MAX_FIG_WIDTH_IN = 32


def _text_color(hex_color: str) -> str:
    r, g, b = (int(hex_color[i : i + 2], 16) for i in (1, 3, 5))
    return _text_color_rgb(r, g, b)


def _text_color_rgb(r: float, g: float, b: float) -> str:
    # Accepts either 0-255 ints or 0-1 floats.
    if r <= 1 and g <= 1 and b <= 1:
        r, g, b = r * 255, g * 255, b * 255
    luminance = 0.299 * r + 0.587 * g + 0.114 * b
    return INK_PRIMARY if luminance > 140 else "#ffffff"


def _bucket_by_iso_week(
    weekly_scores: dict[str, list[tuple[str, int]]],
) -> tuple[list[str], dict[str, dict[str, int]]]:
    # Editions are normally exactly 7 days apart, so grouping by ISO
    # (year, week) is a no-op for the common case. It only kicks in when a
    # player's score lands a day or two off from the rest of the group (e.g.
    # they solved a different day's puzzle), which would otherwise show up
    # as its own near-empty column next to the real one for that week.
    week_dates: dict[tuple[int, int], collections.Counter] = collections.defaultdict(
        collections.Counter
    )
    for points in weekly_scores.values():
        for puzzle_date, _ in points:
            iso_year, iso_week, _ = datetime.date.fromisoformat(puzzle_date).isocalendar()
            week_dates[(iso_year, iso_week)][puzzle_date] += 1

    representative_date = {
        week_key: min(counts.items(), key=lambda kv: (-kv[1], kv[0]))[0]
        for week_key, counts in week_dates.items()
    }

    dates = [representative_date[k] for k in sorted(representative_date)]

    scores_by_date: dict[str, dict[str, int]] = {code: {} for code in PLAYER_CODES}
    for code, points in weekly_scores.items():
        for puzzle_date, score in points:
            iso_key = datetime.date.fromisoformat(puzzle_date).isocalendar()[:2]
            scores_by_date[code][representative_date[iso_key]] = score

    return dates, scores_by_date


def render_scores_heatmap(year: int, weekly_scores: dict[str, list[tuple[str, int]]]) -> bytes:
    """One row per player, one column per week; each cell shows that week's
    score colored on a fixed 0..MAX_SCORE red-to-green scale. Unplayed weeks
    are left an uncolored gray cell with no number."""
    dates, scores_by_date = _bucket_by_iso_week(weekly_scores)
    n_weeks = len(dates)
    n_players = len(PLAYER_CODES)

    fig_width = min(MAX_FIG_WIDTH_IN, max(6.0, n_weeks * CELL_WIDTH_IN))
    fig_height = n_players * CELL_HEIGHT_IN + 1.6
    fig, ax = plt.subplots(figsize=(fig_width, fig_height), dpi=200)
    fig.patch.set_facecolor(SURFACE)
    ax.set_facecolor(SURFACE)

    for row_idx, code in enumerate(PLAYER_CODES):
        for col_idx, d in enumerate(dates):
            score = scores_by_date[code].get(d)
            if score is None:
                color = UNPLAYED
            else:
                color = SCORE_CMAP(max(0.0, min(1.0, score / MAX_SCORE)))
            ax.add_patch(
                Rectangle(
                    (col_idx, row_idx),
                    1,
                    1,
                    facecolor=color,
                    edgecolor=SURFACE,
                    linewidth=1.5,
                )
            )
            if score is not None:
                ax.text(
                    col_idx + 0.5,
                    row_idx + 0.5,
                    str(score),
                    ha="center",
                    va="center",
                    fontsize=8.5,
                    fontweight="bold",
                    color=_text_color_rgb(*color[:3]),
                )

    ax.set_title(
        f"Puntajes Flashback {year}",
        fontsize=14,
        fontweight="bold",
        color=INK_PRIMARY,
        loc="left",
        pad=14,
    )

    ax.set_xlim(0, n_weeks)
    ax.set_ylim(0, n_players)
    ax.invert_yaxis()

    ax.set_yticks([i + 0.5 for i in range(n_players)])
    ax.set_yticklabels(PLAYER_CODES)

    # Only label the first week of each month, else the x-axis is just as
    # cluttered as a per-week label would be.
    tick_positions = []
    tick_labels = []
    last_month = None
    for idx, d in enumerate(dates):
        month = datetime.date.fromisoformat(d).month
        if month != last_month:
            tick_positions.append(idx + 0.5)
            tick_labels.append(datetime.date.fromisoformat(d).strftime("%b"))
            last_month = month
    ax.set_xticks(tick_positions)
    ax.set_xticklabels(tick_labels)

    ax.tick_params(axis="both", colors=INK_MUTED, labelsize=9, length=0)
    ax.tick_params(axis="y", labelsize=10, labelcolor=INK_PRIMARY)
    for spine in ax.spines.values():
        spine.set_visible(False)

    colorbar = fig.colorbar(
        ScalarMappable(norm=Normalize(vmin=0, vmax=MAX_SCORE), cmap=SCORE_CMAP),
        ax=ax,
        orientation="horizontal",
        location="bottom",
        pad=0.25 if n_players <= 3 else 0.15,
        fraction=0.06,
        aspect=40,
        label=f"Puntaje (0-{MAX_SCORE})",
    )
    colorbar.ax.tick_params(colors=INK_MUTED, labelsize=8, length=0)
    colorbar.set_label(f"Puntaje (0-{MAX_SCORE})", color=INK_SECONDARY, fontsize=9)
    colorbar.outline.set_visible(False)

    fig.tight_layout(rect=(0, 0, 1, 1))

    buf = io.BytesIO()
    fig.savefig(buf, format="png", facecolor=SURFACE)
    plt.close(fig)
    buf.seek(0)
    return buf.getvalue()
