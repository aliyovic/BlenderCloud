"""Plot the scan and its local plan on the same LV95 coordinate axes."""
from pathlib import Path
import json

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.ticker import FuncFormatter, MultipleLocator
import numpy as np
from PIL import Image


ROOT = Path(__file__).resolve().parents[1]


def main():
    folder = ROOT / "data/railway_local"
    manifest = json.loads((folder / "project.json").read_text())
    origin = np.array(manifest["origin"], dtype=np.float64)
    with np.load(folder / "scan.npz") as cloud:
        xyz = cloud["points"].astype(np.float64) + origin
        rgb = cloud["rgb"].copy()
    plan = np.asarray(Image.open(folder / "scan_plan.png").convert("RGBA"))
    bounds = manifest["source"]["bounds"]
    xlim = (bounds[0][0]-4, bounds[1][0]+4)
    ylim = (bounds[0][1]-4, bounds[1][1]+4)
    plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 11})
    fig, axes = plt.subplots(1, 2, figsize=(17, 8.6), facecolor="#f4f6f8")
    fig.subplots_adjust(left=.085, right=.97, bottom=.24, top=.85, wspace=.23)
    fig.suptitle("Railway scan in LV95 coordinates", fontsize=23, fontweight="bold", x=.085, ha="left", y=.965)
    fig.text(.085, .912, "North is up. Both views use the same east and north coordinates in metres.", fontsize=12, color="#4a5661")
    order = np.argsort(xyz[:, 2], kind="stable")
    axes[0].scatter(xyz[order, 0], xyz[order, 1], c=rgb[order], s=.25, linewidths=0, rasterized=True)
    area = manifest["scan_plan_area"]
    axes[1].imshow(plan, extent=(area[0], area[2], area[1], area[3]), origin="upper", interpolation="nearest")
    for ax, title in zip(axes, ("Measured scan points, top view", "Local plan image from scan RGB")):
        ax.set_facecolor("#e8edf0")
        ax.set_xlim(xlim)
        ax.set_ylim(ylim)
        ax.set_aspect("equal")
        ax.set_title(title, loc="left", fontsize=14, pad=18)
        ax.xaxis.set_major_locator(MultipleLocator(20))
        ax.yaxis.set_major_locator(MultipleLocator(10))
        ax.xaxis.set_major_formatter(FuncFormatter(lambda value, _: f"{value:,.0f}"))
        ax.yaxis.set_major_formatter(FuncFormatter(lambda value, _: f"{value:,.0f}"))
        ax.tick_params(axis="both", labelsize=9, colors="#4a5661")
        ax.grid(True, color="white", alpha=.7, linewidth=.8)
        ax.set_axisbelow(False)
        ax.set_xlabel("LV95 east, metres", labelpad=14, color="#34404b")
        ax.set_ylabel("LV95 north, metres", labelpad=12, color="#34404b")
        ax.plot(origin[0], origin[1], "+", color="#e74a24", markersize=13, markeredgewidth=2)
        ax.annotate("Project origin", xy=origin[:2], xytext=(8, 12), textcoords="offset points",
                    fontsize=9, color="#aa361c", bbox={"facecolor":"white", "edgecolor":"none", "alpha":.85, "pad":3})
        ax.annotate("N", xy=(.94, .95), xytext=(.94, .84), xycoords="axes fraction", ha="center", va="center",
                    fontsize=12, fontweight="bold", color="#34404b", arrowprops={"arrowstyle":"-|>", "lw":1.5, "color":"#34404b"})
        for spine in ax.spines.values():
            spine.set_color("#bcc7d0")
    fig.text(.085, .145, f"Shared origin    E {origin[0]:,.3f} m    N {origin[1]:,.3f} m    H {origin[2]:,.3f} m", fontsize=12, color="#243443")
    fig.text(.085, .104, f"Scan footprint {bounds[1][0]-bounds[0][0]:.3f} × {bounds[1][1]-bounds[0][1]:.3f} m. Plan cells are 0.5 m.", fontsize=11, color="#4a5661")
    fig.text(.085, .063, "The plan comes from the scan. No independent aerial map is loaded. LV95 is assumed; the height datum is unknown.", fontsize=11, color="#87522f")
    path = ROOT / "artifacts/scan_and_plan_top_view.png"
    fig.savefig(path, dpi=180, facecolor=fig.get_facecolor())
    plt.close(fig)
    print(path)


if __name__ == "__main__":
    main()
