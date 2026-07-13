# ALA → ArcGIS Online Species Pipeline

A production data pipeline that keeps an ArcGIS Online map layer up to date with
species occurrence records from the **Atlas of Living Australia (ALA)**.

Built for real-world forest biodiversity monitoring: it fetches occurrence data
from ALA within a set of forest boundary polygons, filters the records precisely
to those boundaries, and syncs them into a hosted ArcGIS Online Feature Layer —
plus an accumulating CSV backup.

> All credentials and site-specific locations are externalised to a local `.env`
> file, so this repository contains **no secrets and no private site data**.

## What it does

```
   Atlas of Living Australia API          ArcGIS Online (forest boundary layer)
                │                                       │
                ▼                                       ▼
     fetch by bounding box  ───────────────▶  clip to exact polygons
     (paging · retries · 5k split)            (spatial join)
                                                        │
                          dedup · marine filter · format
                                                        │
                        ┌───────────────────────────────┴──────────┐
                        ▼                                           ▼
                CSV backup (accumulate)             ArcGIS Online Feature Layer
                                                    (append new / full replace)
```

## Key features

- **ALA health check** (traffic-light) before doing any work, so a flaky API
  fails fast with a clear message instead of halfway through.
- **Handles ALA's 5,000-record query cap** using recursive binary date-range
  splitting — a month with too many records is split in half until each slice fits.
- **Works around a real ALA quirk:** ALA silently drops the `uuid` field when its
  `fl` (field list) parameter is used, which breaks de-duplication. The pipeline
  fetches full records and slims them in Python instead, so dedup by `uuid` works.
- **Two run modes:**
  - *Weekly* (default): fetch recent months, append only new records by `uuid`.
  - *Full* (`--full`): re-fetch from `ALA_START_YEAR` and replace everything.
- **Bounding box → true polygon** clipping with GeoPandas spatial join.
- Marine-species blacklist, conservation-status columns, and ALA photo thumbnails.
- Structured logging with adjustable verbosity (`--log-level`).

## Setup

### 1. Environment

```bash
python -m venv .venv
# Windows:      .venv\Scripts\activate
# macOS/Linux:  source .venv/bin/activate
```

### 2. Dependencies

`geopandas` and `shapely` need compiled geospatial libraries (GEOS/GDAL/PROJ)
that frequently fail with plain `pip` on Windows. **Conda is recommended:**

```bash
conda install -c conda-forge geopandas shapely
pip install requests pandas arcgis python-dotenv
```

Or, if pip works on your system: `pip install -r requirements.txt`

### 3. Configuration

Copy the template and fill in your own values:

```bash
cp .env.example .env      # Windows: copy .env.example .env
```

| Variable | Required | Description |
|----------|----------|-------------|
| `ARCGIS_USERNAME` | ✅ | ArcGIS Online username |
| `ARCGIS_PASSWORD` | ✅ | ArcGIS Online password |
| `FEATURE_LAYER_URL` | ✅ | Your boundary Feature Service query endpoint |
| `ARCGIS_URL` | | Org URL (default `https://www.arcgis.com`) |
| `LAYER_TITLE` | | Target ArcGIS layer title |
| `OUTPUT_CSV` | | Path for the accumulating CSV |
| `ALA_START_YEAR` | | First year for full updates (default 2000) |
| `RECENT_MONTHS` | | Look-back window for weekly updates (default 6) |
| `LOG_LEVEL` | | `DEBUG` / `INFO` / `WARNING` / `ERROR` |

**Never commit your `.env`** — it is already in `.gitignore`.

### 4. Run

```bash
python ala_to_arcgis.py            # weekly update (append new records)
python ala_to_arcgis.py --full     # full re-fetch and replace
python ala_to_arcgis.py --log-level DEBUG
```

## Automating

For a scheduled weekly sync (Windows Task Scheduler / cron), set the environment
variables at the system level (or point the process at the `.env` file) and run
`python ala_to_arcgis.py`.

## Security

- Credentials and the boundary service URL come from environment variables only —
  nothing sensitive is stored in the code.
- `.env` and `*.csv` are git-ignored, so secrets and location data never reach the
  repository. The repo ships a `.env.example` template only.

## Tech stack

Python · `requests` · `pandas` · `geopandas` · `shapely` · ArcGIS Python API ·
Atlas of Living Australia REST API

## Data & licence

Occurrence data comes from the [Atlas of Living Australia](https://www.ala.org.au/)
under CC-BY. Code is released under the [MIT License](LICENSE).

## Author

Chen-Yang Tsai — Forester & Resource Analyst.
<https://github.com/tcynh-happy>
<chenyangtsai0414@gmail.com>
