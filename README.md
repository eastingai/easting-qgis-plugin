# Easting QGIS plugin

Turn recorded metes-and-bounds deeds into validated parcel geometry, inside
QGIS. A vision model reads the deed and returns every boundary call with its
verbatim source text; deterministic software then recomputes the traverse,
checks closure against the stated acreage, and attaches a GroundTruth verdict
(PASS, REVIEW, or FAIL) so uncertain output is never silently treated as
certain. Click the point of beginning on your map and the parcel lands as a
polygon plus a per-call line layer, exportable to GeoPackage.

## Install

Inside QGIS: Plugins → Manage and Install Plugins → search "Easting"
or install from a zip of this repository.

The plugin needs an **Easting API key**: extraction runs on the hosted API at
[easting.ai](https://easting.ai), and each document you extract is uploaded
there for processing. There's no offline mode. Subscribe at
[easting.ai](https://easting.ai) and paste the key into the plugin's settings.

## What it covers

Metes-and-bounds descriptions (including curve calls) and PLSS aliquot
descriptions. A metes-and-bounds deed defines shape, not location, so
placement is a click on the point of beginning; an aliquot tract
georeferences from BLM's public PLSS fabric and places itself. Lot-and-block
conveyances are captured as plat references. Deed metadata (parties,
recording references, transfer date) and tie courses render in the review
dock and export with the GeoPackage.

## About this repository

This repo mirrors the plugin source shipped to
[plugins.qgis.org](https://plugins.qgis.org). Issues and pull requests are
welcome here; development happens in a private monorepo and each release
syncs to this mirror at its version.

## License

GPL-2.0-or-later. See [LICENSE](LICENSE). The `easting/_vendor/` modules are
dual-licensed into this distribution from the Easting engine.
