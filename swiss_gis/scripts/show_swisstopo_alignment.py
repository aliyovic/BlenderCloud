"""Compare the measured railway with independent local swisstopo imagery."""
from pathlib import Path
import json

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.ticker import FuncFormatter, MultipleLocator
import numpy as np
from PIL import Image
from scipy.spatial import ConvexHull


ROOT = Path(__file__).resolve().parents[1]


def main():
    manifest = json.loads((ROOT / "data/railway_swisstopo/project.json").read_text())
    origin = np.array(manifest["origin"], dtype=np.float64)
    with np.load(ROOT / "data/railway_swisstopo/scan.npz") as scan:
        xyz = scan["points"].astype(np.float64) + origin
        rgb = scan["rgb"].copy()
        valid = scan["classification"] != 7
    aerial = np.asarray(Image.open(ROOT / "data/railway_swisstopo/aerial.png").convert("RGBA"))
    footprint = xyz[valid, :2][ConvexHull(xyz[valid, :2]).vertices]
    footprint = np.vstack((footprint, footprint[0]))
    image_area = manifest["aerial_area"]
    extent = (image_area[0], image_area[2], image_area[1], image_area[3])
    bounds = manifest["source"]["bounds"]
    zoom = (bounds[0][0]-4, bounds[0][1]-4, bounds[1][0]+4, bounds[1][1]+4)
    fit = ROOT.parent / "infrastructure/reconstruction/work"
    alignment = json.loads((fit / "alignment.json").read_text())
    components = json.loads((fit / "component_spec.json").read_text())
    fit_origin = np.array(json.loads((fit / "source_header.json").read_text())["origin"])
    along, across = np.array(alignment["along"]), np.array(alignment["across"])
    plt.rcParams.update({"font.family":"DejaVu Sans", "font.size":10})
    fig, axes = plt.subplots(1, 3, figsize=(20, 8.7), facecolor="#f3f5f7")
    fig.subplots_adjust(left=.06, right=.985, top=.82, bottom=.27, wspace=.27)
    fig.suptitle("Your railway scan over the swisstopo aerial map", x=.06, ha="left", y=.964,
                 fontsize=23, fontweight="bold")
    fig.text(.06, .909, "North is up. Every view uses the original LV95 coordinates, with no alignment shift.",
             fontsize=12, color="#4b5964")
    axes[0].imshow(aerial, extent=extent, origin="upper", interpolation="nearest")
    axes[0].plot(footprint[:, 0], footprint[:, 1], color="#ffb333", linewidth=1.5)
    area = manifest["area"]
    axes[0].set_xlim(area[0], area[2])
    axes[0].set_ylim(area[1], area[3])
    order = np.argsort(xyz[:, 2], kind="stable")
    axes[1].scatter(xyz[order, 0], xyz[order, 1], c=rgb[order], s=.3, linewidths=0, rasterized=True)
    axes[2].imshow(aerial, extent=extent, origin="upper", interpolation="nearest")
    axes[2].plot(footprint[:, 0], footprint[:, 1], color="#ffb333", linewidth=1)
    for rail in components["rails"]:
        u = np.linspace(*rail["range_u"], 300)
        v = np.polyval(rail["v_coefficients"], u)
        xy = u[:, None]*along + v[:, None]*across + fit_origin[:2]
        axes[1].plot(xy[:, 0], xy[:, 1], color="#ffcc45", linewidth=.7, alpha=.9)
        axes[2].plot(xy[:, 0], xy[:, 1], color="#ffcc45", linewidth=.9, alpha=.95)
    for ax in axes[1:]:
        ax.set_xlim(zoom[0], zoom[2])
        ax.set_ylim(zoom[1], zoom[3])
    for i, (ax, title) in enumerate(zip(axes, (
        "SWISSIMAGE 2025, selected surroundings", "Measured scan, top view", "Measured rail paths on the aerial image"))):
        ax.set_facecolor("#e5ebef")
        ax.set_aspect("equal")
        ax.set_title(title, loc="left", fontsize=12, pad=16)
        ax.set_xlabel("LV95 east, metres", labelpad=12)
        ax.set_ylabel("LV95 north, metres", labelpad=10)
        ax.xaxis.set_major_locator(MultipleLocator(80 if i == 0 else 40))
        ax.yaxis.set_major_locator(MultipleLocator(40 if i == 0 else 20))
        ax.xaxis.set_major_formatter(FuncFormatter(lambda value, _: f"{value:,.0f}"))
        ax.yaxis.set_major_formatter(FuncFormatter(lambda value, _: f"{value:,.0f}"))
        ax.tick_params(labelsize=8, colors="#4b5964")
        ax.grid(color="white", alpha=.35, linewidth=.7)
        ax.plot(origin[0], origin[1], "+", color="#ed6d36", markersize=11, markeredgewidth=1.8)
        ax.annotate("N", xy=(.94,.95), xytext=(.94,.80), xycoords="axes fraction", ha="center", va="center",
                    fontsize=11, fontweight="bold", color="white" if i != 1 else "#34404b",
                    arrowprops={"arrowstyle":"-|>", "lw":1.5, "color":"white" if i != 1 else "#34404b"})
        for spine in ax.spines.values():
            spine.set_color("#bec9d2")
    fig.text(.06, .185, "Orange outline = scan footprint. Yellow lines = four rail paths measured from the scan.",
             fontsize=11, color="#334553")
    fig.text(.06, .14, f"Origin E {origin[0]:,.3f} m, N {origin[1]:,.3f} m, H {origin[2]:,.3f} m. Local context {area[2]-area[0]:.3f} × {area[3]-area[1]:.3f} m.",
             fontsize=11, color="#334553")
    fig.text(.06, .095, "SWISSIMAGE 2025 and swissALTI3D 2026 files retained locally. Source: Federal Office of Topography swisstopo.",
             fontsize=10, color="#52616d")
    fig.text(.06, .05, "Horizontal placement can be compared visually. The scan has no CRS record and its height datum is still unconfirmed.",
             fontsize=10, color="#86532f")
    destination = ROOT / "artifacts/swisstopo_alignment_top_view.png"
    fig.savefig(destination, dpi=160, facecolor=fig.get_facecolor())
    plt.close(fig)
    print(destination)


if __name__ == "__main__":
    main()
