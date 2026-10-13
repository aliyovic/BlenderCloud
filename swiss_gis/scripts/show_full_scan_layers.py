"""Draw the full scan, aerial photograph and terrain as separate LV95 panels."""
from pathlib import Path
import json

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.colors import LinearSegmentedColormap
from matplotlib.ticker import FuncFormatter, MultipleLocator
import numpy as np
from PIL import Image
from scipy.spatial import ConvexHull


ROOT = Path(__file__).resolve().parents[1]


def main():
    bundle = ROOT / "data/full_scan_context"
    manifest = json.loads((bundle/"project.json").read_text())
    origin = np.array(manifest["origin"])
    area = manifest["area"]
    with np.load(bundle/"scan.npz") as scan:
        xy = scan["points"][:, :2].astype(np.float64)+origin[:2]
        hull = xy[ConvexHull(xy).vertices]
        hull = np.vstack((hull,hull[0]))
    with np.load(bundle/"terrain.npz") as terrain:
        vertices = terrain["vertices"].astype(np.float64)+origin
    rows = int(np.ceil((area[3]-area[1])/2))
    cols = int(np.ceil((area[2]-area[0])/2))
    assert len(vertices) == rows*cols, "Terrain has missing cells"
    z = vertices[:,2].reshape(rows,cols)
    heights = (float(z.min()),float(z.max()))
    cmap = LinearSegmentedColormap.from_list("terrain_height",[
        [.15,.32,.65], [.10,.65,.64], [.61,.77,.40], [.80,.68,.42], [.94,.89,.73]])
    fig, axes = plt.subplots(1,3,figsize=(20,9),facecolor="#f3f5f7")
    fig.subplots_adjust(left=.045,right=.98,top=.81,bottom=.28,wspace=.23)
    fig.suptitle("Full railway scan, aerial map and terrain",x=.045,ha="left",y=.96,
                 fontsize=24,fontweight="bold")
    fig.text(.045,.908,"Separate top views. North is up. All panels use the same LV95 extent and origin.",
             fontsize=12,color="#42515e")
    def extent(bounds):
        return (bounds[0],bounds[2],bounds[1],bounds[3])
    axes[0].imshow(Image.open(bundle/"scan_plan.png"),extent=extent(manifest["scan_plan_area"]),origin="upper")
    axes[1].imshow(Image.open(bundle/"aerial.png"),extent=extent(manifest["aerial_area"]),origin="upper")
    image = axes[2].imshow(z,extent=(area[0],area[0]+cols*2,area[3]-rows*2,area[3]),
                           origin="upper",cmap=cmap,vmin=heights[0],vmax=heights[1])
    x = area[0]+(np.arange(cols)+.5)*2
    y = area[3]-(np.arange(rows)+.5)*2
    contours = axes[2].contour(x,y,z,levels=np.arange(np.ceil(heights[0]),heights[1],2),
                               colors="#344e57",linewidths=.45,alpha=.6)
    axes[2].clabel(contours,fontsize=6,fmt="%.0f")
    colorbar = fig.colorbar(image,cax=fig.add_axes([.748,.225,.228,.016]),orientation="horizontal")
    colorbar.set_label("Terrain elevation, metres LN02",fontsize=10)
    colorbar.ax.tick_params(labelsize=8)
    titles = ["1. FULL SCAN","2. AERIAL MAP","3. SURROUNDING TERRAIN"]
    subtitles = ["Measured RGB plan from the full source","SWISSIMAGE 2025 orthophotograph",
                 "swissALTI3D 2026, 2 m elevation grid"]
    for index,ax in enumerate(axes):
        ax.set_title(titles[index]+"\n"+subtitles[index],loc="left",fontsize=12,pad=15,linespacing=1.7)
        ax.set_facecolor("#e3e9ee")
        ax.set_xlim(area[0],area[2]);ax.set_ylim(area[1],area[3]);ax.set_aspect("equal")
        ax.plot(hull[:,0],hull[:,1],color="#ffb63e",linewidth=1.1)
        ax.set_xlabel("LV95 east, metres",labelpad=10)
        ax.set_ylabel("LV95 north, metres",labelpad=8)
        ax.xaxis.set_major_locator(MultipleLocator(100));ax.yaxis.set_major_locator(MultipleLocator(50))
        formatter = FuncFormatter(lambda value,_:f"{value:,.0f}")
        ax.xaxis.set_major_formatter(formatter);ax.yaxis.set_major_formatter(formatter)
        ax.tick_params(labelsize=8,colors="#4c5b67")
        ax.grid(color="white",alpha=.25,linewidth=.6)
        ax.annotate("N",xy=(.94,.94),xytext=(.94,.76),xycoords="axes fraction",ha="center",
                    fontsize=10,fontweight="bold",color="#263849" if index!=1 else "white",
                    arrowprops={"arrowstyle":"-|>","lw":1.4,"color":"#263849" if index!=1 else "white"})
        for spine in ax.spines.values():
            spine.set_color("#c1cbd3")
    fig.text(.045,.20,"Orange outlines the scan footprint. The aerial map and terrain include a 50 m margin.",
             fontsize=11,color="#354858")
    fig.text(.045,.15,f"Full source: 21,862,802 points. Blender overview: {manifest['preview_points']:,} points. Full detail remains in the local source and cache.",
             fontsize=11,color="#354858")
    fig.text(.045,.105,"Source for aerial imagery and terrain: Federal Office of Topography swisstopo. Saved locally; no live map service.",
             fontsize=10,color="#536572")
    fig.text(.045,.06,"LV95 is assumed because the scan has no CRS record. The scan height datum is unknown; terrain heights are LN02.",
             fontsize=10,color="#84562f")
    output = ROOT / "artifacts/full_scan_separate_top_views.png"
    fig.savefig(output,dpi=170,facecolor=fig.get_facecolor())
    plt.close(fig)
    print(output)


if __name__ == "__main__":
    main()
