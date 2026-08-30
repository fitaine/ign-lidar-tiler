# IGN LiDAR Tiler — Plan

Status: agreed 2026-08-30, nothing built yet.
Repo: https://github.com/fitaine/ign-lidar-tiler (public)

## Purpose

Remove the two manual ends of the LiDAR workflow:

1. **Acquisition.** Today: browse the IGN portal, download tiles by hand, run
   `lidar_pipeline.py` with a hand-picked voxel, guess the ortho settings.
   Tomorrow: draw a shape on a map, get a working PLY.
2. **Render prep.** Today: whatever is in the .blend is what renders, so the
   cloud has to be light enough for the GUI, which caps render detail.
   Tomorrow: light and carve a sparse cloud in the GUI, and the app builds a
   headless-only .blend carrying a much denser cloud of the same scene.

The second is the reason the first exists. Blender's *viewport* dies on 70M+
points, Cycles does not (see `dense-cloud-gui-vs-headless`). Rendafar renders
headless. So the density ceiling in the render is an artefact of the GUI, and
this app removes it.

## Core design decisions

**Radius is derived, never typed.** The pipeline convention is
`radius = voxel / 2`, the value at which neighbouring spheres touch without
overlapping. Each variant gets its own radius from its own voxel, so the dense
variant's radius is smaller. The app writes it into
`Lidar node base -> Mesh to Points -> Radius` automatically. A **multiplier is
exposed as a control** (working files have run near 1.33x nominal); it defaults
to the touch-no-overlap value and is stored per scene.

**The dense variant is built at render prep, not at download.** The shape that
matters is the one that survives the Blender edit, and that shape is often a
few percent of the downloaded footprint. Building a full-footprint dense PLY up
front would be mostly wasted disk and mostly wasted PDAL time.

**The archive is the raw tiles.** No second colorized full-density copy on
disk. Archive = the downloaded `.copc.laz` tiles plus the ortho GeoTIFF.
Colorizing only ever runs over the cropped region.

**No coupling to rendafar.** The swap needs PDAL and the archive, so it lives
here. Rendafar receives a finished .blend exactly as it does today.

**`lidar_pipeline.py` is wrapped, not rewritten.** It is proven on 35 scenes.
The app calls it, and the pieces that need to be reused directly (voxel
estimation, tiled WMS fetch, PLY export) are imported rather than duplicated.

## Data model, the scene manifest

One `scene.json` per scene, the single source of truth. Filenames keep the
`-NNN` suffix so existing habits and existing files still read correctly, but
nothing in the app parses a filename for truth.

```jsonc
{
  "name": "aravis",
  "created": "2026-08-30",
  "crs": "EPSG:2154",
  "origin": [966000, 6537000, 1027.26],   // shared by every variant
  "footprint": { "type": "Polygon", "coordinates": [] },  // what was drawn
  "cropped_to_footprint": true,            // false = whole intersecting tiles
  "tiles": ["LHD_FXX_0966_6537_PTS_C_LAMB93_IGN69.copc.laz"],
  "raster": "aravis-020_raster.tif",
  "raster_res": 0.20,
  "radius_multiplier": 1.0,
  "variants": [
    { "role": "sparse", "file": "aravis-hd-150.ply",
      "voxel": 1.50, "radius": 0.75, "points": 16400000 }
  ],
  "renders": []                            // filled by stage 3
}
```

## Stage 1, acquire

- Map with two switchable base layers, IGN ortho and IGN plan, both already
  known to `lidar_pipeline.py` (`ORTHOIMAGERY.ORTHOPHOTOS`,
  `GEOGRAPHICALGRIDSYSTEMS.PLANIGNV2`).
- Draw a **rectangle** or a **free polygon**. Both are required.
- The app overlays the IGN LiDAR HD tile grid, highlights every intersecting
  tile, and shows the count, the covered area and an estimated download size
  before anything is fetched.
- **Crop to shape** is a checkbox. Off: whole 1 km tiles, ragged staircase
  edge, no extra pass. On: a PDAL crop to the drawn polygon, clean edges, one
  extra pass. Default off, since the edge is usually off camera.
- Download, then run the pipeline once: origin from the full selection, ortho
  fetched once at 0.20 m/px, one **sparse** PLY.
- Sparse target is a control, up to 50M points. The auto voxel formula is
  unreliable above 500 m of relief, which is why every alpine scene overrides
  it. So the app **solves for voxel by measurement**: run, count, adjust,
  repeat, at most two or three iterations, rather than trusting the formula.
- Writes `scene.json`.

Open item to verify at build time: the exact endpoint for the IGN LiDAR HD tile
index and per-tile download URLs. It is published by IGN Geoplateforme but the
URL scheme should be checked against the live service rather than assumed.

## Stage 2, Blender (unchanged, plus a small add-on)

Work as today: import, light, carve. Edits are **subtractive vertex deletion
plus a single translation of the object**, no scale, no duplicates. Deletions
made by hand and deletions made through the Blender MCP are identical to the
app, they are just absent vertices.

The add-on is one panel that tags a point cloud object with its scene name,
stored as a custom property on the object. This is what lets stage 3 know which
object is a proxy for which scene. Convention over object names was considered
and rejected: it breaks the first time an object is renamed.

## Precision rules (learned the hard way, 2026-08-30)

**Always recentre before writing a PLY.** `writers.ply` with `dims: X=float`
writes float32. In raw Lambert 93 that gives a 0.5 m resolution in Y and
0.0625 m in X, so points get snapped to a lattice coarser than the voxel and
comparable to the ball radius. Four existing scenes are affected (Nantua,
Bunker Eperlecques, Montvernier, Lac des Milles Vaches). The app must never
emit a PLY in absolute coordinates.

**Recover offsets from the centroid, never the bounding box.** Quantisation
biases min and max by up to half a lattice step; on Montvernier the two
bbox-derived Y offsets disagreed by 0.34 m while the centroid was exact.

**Accumulate in float64.** A numpy float32 mean over 27M vertices was off by
about 4 m and looked like a genuine offset.

## Headroom check

Before offering a dense render, measure whether the scene has any. Voxelise the
cloud at a few cell sizes and read points per occupied cell: at 1.0 the cloud
has never been merged and already *is* the source data. Measured examples:
La Plagne 36.9 pts/m² and still 1.17 pts/cell at a 10 cm voxel, so no headroom
at all; Montvernier 9.1 pts/m² at voxel 0.6515, so roughly 3 to 4x available.

"Already at source density, render as is" is a legitimate answer and the app
should give it rather than doing a no-op swap.

## Extending a scene

Separate from densifying, and asked for on La Plagne: the footprint is too
tight to frame the camera. Extending means downloading the neighbouring tiles
and merging them into the existing cloud's coordinate frame, which needs the
same origin recovery as the dense swap. Stage 1 and stage 3 share that
machinery, so this is a small addition once both exist.

## Stage 3, prep render

1. Read the tagged object out of the .blend.
2. Recover the transform. The offset comes off the object's world matrix, and
   is **cross-checked against the source PLY centroid** so a move made in edit
   mode instead of object mode cannot silently break alignment. Disagreement
   beyond a tolerance is a hard error, not a warning.
3. Build an **occupancy mask** from the surviving vertices: voxelize them into
   a coarse grid, a few metres per cell. This reproduces arbitrary carving,
   including interior holes, vertical cuts and ragged edges, without anyone
   having to describe the shape.
4. Crop the archive tiles to the mask, colorize from the ortho, downsample to
   the dense voxel, export the dense PLY recentred on the same origin.
5. Write the render .blend: same scene, same lighting, same camera, cloud
   replaced, radius set from the dense voxel and the multiplier, material and
   GN modifier carried over, `Col` kept as `FLOAT_COLOR`.
6. Append the variant and the render entry to `scene.json`.

The output is explicitly **not for opening in the GUI**. The filename should
say so.

### Choosing the dense voxel

Radius is derived from voxel, so density and sphere size fall together and the
dense cloud is not automatically heavier per tile. But there is a real ceiling
worth measuring rather than guessing: below some spacing the added points sit
inside the union of their neighbours and contribute nothing visible except at
silhouettes and thin structures.

So stage 3 gets a **density budget control** plus a one-off calibration: render
one tile of one scene at three densities and look at where the difference stops
being visible. That number, expressed as a ratio to the sparse voxel, becomes
the default. Until it is measured the default stays conservative.

## Measured results (2026-08-30, Mont Aiguille)

Stages 3a and 3b are built and working end to end.

| points | voxel | raw radius | render 800x625/32 | peak RAM |
|---|---|---|---|---|
| 98,979,482 | 0.65 | 0.325 | 12.6 s | 16.0 GB |
| 172,729,010 | 0.45 | 0.225 | 17.9 s | 27.5 GB |

RAM is the binding constraint, about 0.16 GB per million points, so roughly a
350M ceiling on a 64 GB machine. Render time is sub-linear in point count
because the radius falls as density rises.

Two gotchas worth keeping:

**The radius socket is unit-scaled for display.** Mont Aiguille has
`scale_length = 0.01`, so a raw 0.325 shows as 0.00325 in the panel. Always set
and report the raw value alongside the displayed one.

**Volumetrics must be stripped before any render-time measurement.** Volume
objects are not enough to look for: a node linked into a material or world
Volume socket triggers full volumetrics on its own. Mont Aiguille had four
sources and `volume_bounces` at 8.

**densify.py does not scale past this scene.** It merges all tiles before
downsampling, so peak memory is the whole raw cloud: 40 GB for Mont Aiguille's
565M points. Aravis at 1.18 billion would not fit. The fix is per-tile
downsampling before the merge, pending a check on whether PDAL's voxel grid
origin stays consistent across separate views.

## Build order

Stage 3 first. It is the part that unblocks scenes already lit, it can be
driven from the command line before any UI exists, and it is where the
uncertainty is. Stage 1's map is the more visible half but it automates work
that already works.

1. **3a** Mask crop and dense PLY export, command line, against an existing
   scene and an existing .blend.
2. **3b** Render .blend generation, radius applied, verified by a single test
   tile against the sparse render.
3. **3c** Density calibration on that test tile, sets the default.
4. **1a** Tile index, map, selection, download, sparse PLY, `scene.json`.
5. **1b** Crop-to-shape option, voxel solve-by-measurement.
6. **2**  Blender add-on panel for tagging.
7. **UI** Wrap stage 3 in the web app once the command line version is trusted.

## Stack

FastAPI backend, MapLibre GL front end with the IGN WMTS layers, run locally
and opened in the browser. Same shape as rendafar without sharing anything with
it. PDAL and GDAL come from the QGIS install at
`C:\Program Files\QGIS 3.40.5\bin\`, as `lidar_pipeline.py` already assumes.
