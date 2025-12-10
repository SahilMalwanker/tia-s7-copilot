"""Results figure for the README (light + dark SVG).

    pip install matplotlib
    python tools/make_charts.py

Left: cycle time vs. robot speed - the interpolation model used by the copilot
(tia_s7_copilot.api.calculate_production_metrics) against cycle times measured on the
physical cell (17 cycles, mean ± standard deviation).
Right: energy per assembled part from the utilisation model the LLM is instructed to use
(prompts.ENERGY_CONTEXT), including the arm power-tier step above 50 % speed.
"""

from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

OUT = Path(__file__).resolve().parents[1] / "docs" / "assets"

CYCLE_POINTS = [(30, 100), (50, 80), (75, 70), (100, 66)]  # same table as the API
MEASURED = {50: (89.3, 3.1, 6), 70: (73.4, 2.0, 5), 90: (66.3, 1.6, 6)}  # speed: (mean s, std s, cycles)
UTILISATION = {"arm_1": 0.18, "arm_2": 0.32, "arm_3": 0.10, "conveyor_1": 0.08, "conveyor_2": 0.08}
EUR_PER_KWH = 0.15


def cycle_time(speed):
    pts = CYCLE_POINTS
    if speed <= pts[0][0]:
        return pts[0][1]
    if speed >= pts[-1][0]:
        return pts[-1][1]
    for (s0, t0), (s1, t1) in zip(pts, pts[1:]):
        if s0 <= speed <= s1:
            return t0 + (speed - s0) / (s1 - s0) * (t1 - t0)


def energy_per_part_wh(speed):
    cycle = cycle_time(speed)
    arm_active = 250 if speed > 50 else 150
    total_ws = 0.0
    for part, share in UTILISATION.items():
        active, idle = (arm_active, 5) if part.startswith("arm") else (50, 2)
        total_ws += active * share * cycle + idle * (1 - share) * cycle
    return total_ws / 3600


def draw(dark):
    ink = "#ffffff" if dark else "#000028"
    muted = "#9999b3" if dark else "#66667e"
    grid = "#26265a" if dark else "#e2e2dc"
    bg = "#000028" if dark else "#f3f3f0"
    panel = "#0e0e38" if dark else "#ffffff"
    petrol, mint = ("#00cccc", "#00ffb9") if dark else ("#009999", "#00a37a")

    plt.rcParams.update({
        "font.family": ["Segoe UI", "DejaVu Sans", "Arial", "sans-serif"],
        "font.size": 11,
        "svg.fonttype": "none",
        "text.color": ink, "axes.labelcolor": muted, "xtick.color": muted, "ytick.color": muted,
    })
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(12, 4.3), dpi=100)
    fig.patch.set_facecolor(bg)

    speeds = [s / 2 for s in range(60, 201)]
    for ax in (ax1, ax2):
        ax.set_facecolor(panel)
        ax.grid(True, color=grid, linewidth=0.8)
        ax.set_axisbelow(True)
        for side in ("top", "right"):
            ax.spines[side].set_visible(False)
        for side in ("left", "bottom"):
            ax.spines[side].set_color(grid)
        ax.set_xlabel("robot speed (%)")
        ax.set_xlim(28, 102)

    ax1.plot(speeds, [cycle_time(s) for s in speeds], color=petrol, linewidth=2.6, label="copilot model (interpolated)")
    ax1.scatter([s for s, _ in CYCLE_POINTS], [t for _, t in CYCLE_POINTS], s=26, color=petrol, zorder=3)
    xs = list(MEASURED)
    ax1.errorbar(xs, [MEASURED[s][0] for s in xs], yerr=[MEASURED[s][1] for s in xs], fmt="o", markersize=8,
                 color=mint, ecolor=mint, elinewidth=2, capsize=5, zorder=4, label="measured on the cell (mean ± sd)")
    for s in xs:
        mean, _, n = MEASURED[s]
        ax1.annotate(f"{mean:.1f} s · n={n}", (s, mean), textcoords="offset points", xytext=(10, 8), color=ink, fontsize=10)
    ax1.set_ylabel("cycle time (s)")
    ax1.set_ylim(60, 104)
    ax1.set_title("Cycle time vs. speed", loc="left", fontsize=13.5, fontweight="bold", color=ink, pad=12)
    legend = ax1.legend(frameon=False, loc="upper right", fontsize=10)
    for text in legend.get_texts():
        text.set_color(muted)

    low = [s for s in speeds if s <= 50]
    high = [s for s in speeds if s > 50]
    ax2.plot(low, [energy_per_part_wh(s) for s in low], color=petrol, linewidth=2.6)
    ax2.plot(high, [energy_per_part_wh(s) for s in high], color=petrol, linewidth=2.6)
    ax2.axvline(50, color=muted, linestyle=(0, (4, 4)), linewidth=1.2)
    ax2.text(51.5, 4.04, "arm power tier 150 W → 250 W", ha="left", color=muted, fontsize=10)
    offsets = {50: ((8, -16), "left"), 80: ((0, 12), "center"), 100: ((-4, -22), "right")}
    for s in (50, 80, 100):
        wh = energy_per_part_wh(s)
        ax2.scatter([s], [wh], s=30, color=mint, zorder=3)
        offset, align = offsets[s]
        ax2.annotate(f"{wh:.2f} Wh · €{wh / 1000 * EUR_PER_KWH:.5f}", (s, wh), textcoords="offset points",
                     xytext=offset, ha=align, color=ink, fontsize=10)
    ax2.set_ylabel("energy per part (Wh)")
    ax2.set_ylim(2.2, 4.2)
    ax2.set_title("Energy per assembled part", loc="left", fontsize=13.5, fontweight="bold", color=ink, pad=12)

    fig.tight_layout(pad=1.6, w_pad=3)
    name = "results-dark.svg" if dark else "results-light.svg"
    fig.savefig(OUT / name, facecolor=bg, metadata={"Date": None, "Creator": None})
    plt.close(fig)
    print(f"{name}: {(OUT / name).stat().st_size / 1024:.1f} KB")


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    assert abs(energy_per_part_wh(80) * 3600 - 12018.656) < 0.01  # worked example in prompts.ENERGY_CONTEXT
    draw(dark=False)
    draw(dark=True)
