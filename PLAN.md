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
  edge. On: the per-tile crop that already removes the grid anchor is given
  the drawn polygon instead of the bounding box, so it costs nothing extra.
  Default off, since the edge is usually off camera.
- Download, then run the pipeline once: origin from the full selection, ortho
  fetched once at 0.20 m/px, one **sparse** PLY.
- Sparse target is a control, up to 50M points. The auto voxel formula is
  unreliable above 500 m of relief, which is why every alpine scene overrides
  it. So the app **solves for voxel by measurement**: run, count, adjust,
  repeat, at most two or three iterations, rather than trusting the formula.
- Writes `scene.json`.

**Tile index, resolved 2026-08-31.** The Geoplateforme WFS layer
`IGNF_NUAGES-DE-POINTS-LIDAR-HD:dalle` at `https://data.geopf.fr/wfs/ows`.
Each feature carries the 1 km footprint in Lambert 93 and a direct download
URL. Verified by querying Mont Aiguille's footprint and getting back exactly
the six tiles already on disk, 3.49 GB, matching the local total.

**Voxel solving, resolved 2026-08-31.** Scale the probe by **area, not raw
point count**: the downsample output counts occupied voxels, which follow the
surface, while raw counts follow flight overlap (Mont Aiguille's tiles span
2.4x in raw points but only 1.8x in occupied voxels). Scaling by raw counts
missed by 27%; by area it lands within 1%.

## Stage 2, Blender (unchanged, plus a small add-on)

Work as today: import, light, carve. Edits are **subtractive vertex deletion
plus a single translation of the object**, no scale, no duplicates. Deletions
made by hand and deletions made through the Blender MCP are identical to the
app, they are just absent vertices.

`blender_addon/ign_lidar_tiler.py` adds one panel under Properties > Object.
It tags a cloud with the path to its `scene.json`, stored as a custom property
rather than a naming convention, which would break the first time an object is
renamed. `make_render_blend.py` then finds the cloud on its own and `--object`
becomes optional.

The panel also shows the manifest's variants and applies a variant's derived
radius, printing both the raw value and what the panel will display, since the
Radius socket is unit-scaled (a scene at `scale_length` 0.01 shows 0.00325 for
a raw 0.325). The tag survives the swap, so the render file stays identified.

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
4. Crop to the mask, colorize from the ortho, downsample to the dense voxel,
   export the dense PLY recentred on the same origin.

   **Measured mask behaviour (2026-08-31).** The cell must be comfortably
   coarser than the sparse cloud's point spacing, or cells inside the kept
   region come up empty by chance and the mask punches holes in itself: at
   3.4x spacing a synthetic test kept 74% where truth was 87%. `extract_mask.py`
   warns below 4x. At an adequate cell the mask slightly over-keeps at the
   edges, which is the safe direction. Default 3 m sits at 12-18x on real
   scenes. End-to-end on Mont Aiguille (uncarved, so the mask should be a
   no-op): 152,755,274 of 153,027,647 kept, **99.8%**. On La Plagne (carved)
   the mask covers 39.7% of the footprint, matching an independent measurement
   of 39.0%, and only **1.72% of the 3D grid volume**, since terrain is a thin
   surface in a tall box. That volume figure is the saving the crop delivers.
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

**Memory scaling, solved 2026-08-31.** `densify.py` merged all tiles before
downsampling, so peak memory was the whole raw cloud: 51 GB measured on Mont
Aiguille, a projected 107 GB for Aravis and 120 GB for Nantua, both impossible
on 64 GB. `densify_tiled.py` processes one tile at a time and concatenates the
PLY bodies, so peak follows the largest tile: 19.4 GB, ~5.1 GB and ~11.1 GB
respectively. Aravis is now *cheaper* than Mont Aiguille despite twice the
data, since its billion points are spread over 48 tiles rather than 6.

That needed the grid-anchor trick: PDAL anchors the voxel grid on the first
point it sees, so adjacent tiles otherwise land on different lattices
(measured offsets 0.12 and 0.39). A `readers.faux` point at an aligned
coordinate is fed first, then cropped away. Output matches the merged build to
0.011% in count and sub-voxel in bounds.

## Device choice (measured 2026-08-30, one 120k-sized tile)

| points | GPU OptiX 4070 | CPU 5950X | peak VRAM | peak GPU power |
|---|---|---|---|---|
| 98,979,482 @ r0.325 | 43.5 s | 141.5 s | 6,376 MiB | 118.9 W |
| 172,729,010 @ r0.225 | 107.1 s | 154.2 s | 11,531 MiB | 64.3 W |

CPU is nearly flat in point count (+9% for +74%); GPU degrades sharply once
VRAM fills. The curves cross near 210-230M points. So: under ~120M render GPU,
above ~220M render CPU, and treat ~120M as the GPU-path ceiling on a 12 GB card.

Two things the app must do as a result:

- **set the render device explicitly** in the generated file rather than
  inheriting whatever the scene was saved with (Mont Aiguille was saved CPU
  while her preferences are OptiX)
- **warn when a variant will exceed the card's VRAM**, since the binding limit
  is the GPU's memory, not the system's

## Production budget (measured 2026-08-30)

Real settings: 2048 samples, no denoise, no adaptive, no time limit. Grid from
`render_tiles_unlimited.py`: 30 x 30 = 900 tiles of 4096 x 3200 for a 120k
render of a 16000x12500 scene.

| points | per tile | 900-tile render |
|---|---|---|
| 98,979,482 @ r0.325 | ~184 s | ~46 h |
| 172,729,010 @ r0.225 | ~230 s | ~58 h |

A 200 h budget allows 800 s per tile, so **time is not the constraint**. The
dense penalty also shrinks with sample count: 2.46x at 128 spp but only 1.25x
at 2048 spp, because BVH build and memory stalls amortise over more sampling.

**VRAM is the constraint.** Working maximum set at **150M points**, now
verified: 153,027,647 pts at radius 0.245 peaks at 10,234 of 12,282 MiB (83%)
and 127.5 W, higher power than either neighbour, so the card is computing
rather than stalling. ~234 s per tile, ~58 h for 900 tiles. The app should warn when a variant would exceed the
card, and account for volumetrics, which on a 12 GB card are close to mutually
exclusive with a dense cloud.

## Build order

Stage 3 first. It is the part that unblocks scenes already lit, it can be
driven from the command line before any UI exists, and it is where the
uncertainty is. Stage 1's map is the more visible half but it automates work
that already works.

1. **3a** DONE. `densify.py` (dense PLY export), `extract_mask.py` +
   `crop_to_mask.py` (occupancy mask crop).
2. **3b** DONE. `make_render_blend.py`, radius applied, volumetrics stripped,
   verified by production tiles.
3. **3c** DONE. See the production budget above; working maximum 150M.
4. **1a** IN PROGRESS. `fetch_tiles.py` (tile index + download) and
   `solve_voxel.py` (voxel solve by measurement) are done and verified.
   Remaining: orchestration into a single acquire step that writes
   `scene.json`, and the map UI.
5. **1b** DONE. `--crop-to-shape` on `acquire.py`, `--polygon` on
   `densify_tiled.py`. Verified with a diamond covering 50.0% of a tile's
   area, which kept 54.1% of its points (the excess is terrain: the middle of
   that tile is steeper, so it carries more surface per square metre).
6. **2** DONE. `blender_addon/ign_lidar_tiler.py`.
7. **UI** DONE, both stages. `server.py` + `static/index.html`, two panels:
   *Acquire* draws a rectangle or free shape over IGN ortho/plan with a live
   tile grid and reports tile count, area, extent and download size before
   running `acquire.py`; *Render prep* loads a `scene.json`, shows its
   variants, and runs `prepare_render.py`. Both stream their log back.

## Stack

**Standard library only, no FastAPI.** The plan said FastAPI; this machine has
no web framework installed and a local single-user tool did not justify adding
one, so `server.py` is built on `http.server`. MapLibre GL is the only front
end dependency, from a CDN.

**No pyproj either.** The map works in longitude/latitude and everything
downstream in Lambert 93, so `lambert93.py` implements the projection directly
(Lambert Conformal Conic, 2 standard parallels, on GRS80). It round-trips with
zero error and agrees with `gdaltransform` to eight decimal places.

PDAL and GDAL come from the QGIS install, as `lidar_pipeline.py` already
assumes.

## Running it

    python server.py            # then open http://localhost:8765

Draw a rectangle or a free shape, name the scene, choose an output folder and
press Acquire. Then light and carve in Blender, tag the cloud with the add-on,
and use the Render prep panel, or the same thing on the command line:

    python prepare_render.py --scene scene.json --blend lit.blend --target 150000000

which runs these four steps in order and records the result in the manifest:

    blender -b scene.blend --python extract_mask.py -- --object <cloud> --cell 3.0 --out mask.npz
    python densify_tiled.py --tiles <dir> --raster <tif> --voxel <v> --origin <x,y,z> --name <n> --out <dir>
    python crop_to_mask.py --ply dense.ply --mask mask.npz --out dense-cropped.ply
    blender -b scene.blend --python make_render_blend.py -- --ply dense-cropped.ply --radius <r> --strip-volumes --out render.blend

Stage 3 is not in the UI yet; it runs from the command line.
