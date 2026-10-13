# Point cloud and BIM test data

## Blender scan viewer

The working add-on and installation guide are in [scan_viewer/](../scan_viewer/README.md). It was tested in Blender against the point clouds and matching IFCs below.

## Local Swiss GIS

The separate [Swiss GIS Local extension](../swiss_gis/README.md) aligns local LV95 point clouds, terrain GeoTIFFs, and aerial GeoTIFFs without network services. Its railway example includes small local swisstopo crops covering the scan and its 50-metre surroundings.

## Railway BIM

The [Railway BIM extension](../railway_bim/README.md) builds an editable IFC 4.3 model from the fitted railway crop. Its [Bonsai workspace](../railway_bim/output/railway_crop.blend) retains the complete scan, measured alignments, and source evidence.

## Downloaded pairs

- [CRAS lab](../CRAS_Lab/README.md): a real registered, labelled laser scan and its matching as-built IFC model. The full point cloud stays in its ZIP archive. `craslab_sample_1in1000.ply` is a smaller sample for quick imports.
- [Synthetic building](../Synthetic_Building/README.md): a compact E57 point cloud and matching IFC model of a three-space test building.

Both downloaded pairs are published under CC BY 4.0. See each folder's README for source links and attribution details.

## Access-limited datasets

`BIMNet/` contains the public repository and file documentation only. The scan and model data require Matterport3D approval. Its published agreement is limited to noncommercial academic use, so it was not signed for developer workflow testing. Matterport and the BIMNet team have been asked whether they offer suitable access terms.

DeKH is not downloaded. Its Hugging Face repository requires accepted access conditions, and its CC BY-NC-SA 4.0 license limits use to noncommercial purposes.
