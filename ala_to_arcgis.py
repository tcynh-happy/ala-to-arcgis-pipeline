# -*- coding: utf-8 -*-
"""ALA Species Data to ArcGIS Online pipeline.

Fetches species occurrence data from the Atlas of Living Australia (ALA)
within a set of forest boundary polygons, then updates an ArcGIS Online
Feature Layer with the results.

Two modes (see --full flag):
    full    -> Clears all data and re-fetches from ALA_START_YEAR onwards
    weekly  -> Only checks recent months and appends new records (default)

Key features:
    - ALA health check (traffic light) before fetching
    - pageSize=100 to avoid ALA 503 errors
    - Efficient auto-split: only splits date ranges that exceed 5000 records
    - uuid used to skip duplicates (ALA's real unique ID field)
    - Does NOT use ALA's 'fl' parameter, because ALA drops the uuid field
      whenever 'fl' is specified. Instead we fetch full records and select
      the columns we need in Python, so deduplication actually works.
    - Marine species blacklist, conservation status, photo URLs
    - CSV accumulate mode (old data never deleted)

Configuration is read from environment variables (see .env.example).
Never hard-code credentials in this file.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from datetime import datetime

import geopandas as gpd
import pandas as pd
import requests
from arcgis.features import Feature
from arcgis.geometry import Point
from arcgis.gis import GIS
from shapely.ops import unary_union

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # python-dotenv is optional; env vars still work without it
    pass


# =============================================================================
# CONFIGURATION
# Non-secret defaults live here; secrets and machine-specific paths come from
# environment variables (see .env.example). Override any of these via env vars.
# =============================================================================
ARCGIS_URL = os.getenv("ARCGIS_URL", "https://www.arcgis.com")
ARCGIS_USERNAME = os.getenv("ARCGIS_USERNAME", "")
ARCGIS_PASSWORD = os.getenv("ARCGIS_PASSWORD", "")

# Company-specific service URL is intentionally NOT hard-coded here so that no
# private location info lives in the repo. Set it in your local .env file.
FEATURE_LAYER_URL = os.getenv("FEATURE_LAYER_URL", "")
LAYER_TITLE = os.getenv("LAYER_TITLE", "ALA_Species_Observations")
OUTPUT_CSV = os.getenv("OUTPUT_CSV", "ALA_species.csv")

ALA_SEARCH_URL = "https://biocache-ws.ala.org.au/ws/occurrences/search"
ALA_PAGE_SIZE = 100          # Keep at 100 or less (200+ triggers 503)
ALA_MAX_WINDOW = 5000        # ALA hard limit per query
ALA_START_YEAR = int(os.getenv("ALA_START_YEAR", "2000"))
RECENT_MONTHS = int(os.getenv("RECENT_MONTHS", "6"))  # weekly mode look-back

REQUEST_DELAY = 1
RETRY_DELAY = 20
MAX_RETRIES = 4

HEALTH_CHECK_TRIES = 5
HEALTH_CHECK_PASS = 3

UPLOAD_BATCH_SIZE = 500

MARINE_BLACKLIST = [
    "Cerithium",     # Marine sea snails
    "Thalassarche",  # Albatrosses - open ocean pelagic seabirds
]

# We deliberately do NOT send an 'fl' parameter to ALA: when 'fl' is used, ALA
# omits the 'uuid' field, which breaks deduplication. We request full records
# and select these fields in Python instead.
NEEDED_FIELDS = [
    "uuid", "scientificName", "vernacularName",
    "decimalLatitude", "decimalLongitude", "eventDate",
    "images", "stateConservation", "austConservation",
]

log = logging.getLogger("ala_to_arcgis")


# =============================================================================
# STEP 0: ALA health check (traffic light)
# =============================================================================
def check_ala_health() -> bool:
    """Ping ALA a few times; return True if it looks healthy enough to proceed."""
    log.info("=" * 60)
    log.info("STEP 0: Checking if ALA server is healthy...")

    ok_count = 0
    for i in range(HEALTH_CHECK_TRIES):
        try:
            r = requests.get(
                ALA_SEARCH_URL,
                params={"q": "*:*", "pageSize": 1},
                timeout=30,
            )
            if r.status_code == 200:
                r.json()
                ok_count += 1
                log.info("  Ping %d/%d: OK", i + 1, HEALTH_CHECK_TRIES)
            else:
                log.info("  Ping %d/%d: status %s", i + 1, HEALTH_CHECK_TRIES, r.status_code)
        except requests.RequestException:
            log.info("  Ping %d/%d: failed", i + 1, HEALTH_CHECK_TRIES)
        time.sleep(2)

    log.info("  Result: %d/%d pings succeeded", ok_count, HEALTH_CHECK_TRIES)

    if ok_count < HEALTH_CHECK_PASS:
        log.error("=" * 60)
        log.error("[RED LIGHT] ALA server is currently unstable.")
        log.error("  This is an ALA server problem, not this script.")
        log.error("  Please try again later (an hour or tomorrow morning).")
        log.error("=" * 60)
        return False

    log.info("[GREEN LIGHT] ALA server looks healthy. Continuing...")
    return True


# =============================================================================
# STEP 1: Login to ArcGIS Online
# =============================================================================
def login_arcgis() -> tuple[GIS, str]:
    """Log in once and return (GIS object, token)."""
    log.info("STEP 1: Logging in to ArcGIS Online...")
    if not ARCGIS_USERNAME or not ARCGIS_PASSWORD:
        raise SystemExit(
            "ARCGIS_USERNAME / ARCGIS_PASSWORD are not set. "
            "Copy .env.example to .env and fill them in."
        )

    gis = GIS(url=ARCGIS_URL, username=ARCGIS_USERNAME, password=ARCGIS_PASSWORD)
    token = gis._con.token  # reuse the session token; no second generateToken call
    log.info("  Logged in as: %s", gis.properties.user.username)
    return gis, token


# =============================================================================
# STEP 2: Load forest boundary
# =============================================================================
def load_forest_boundary(token: str):
    """Load the forest polygons and return (GeoDataFrame, bounds tuple)."""
    log.info("STEP 2: Loading forest boundary...")
    if not FEATURE_LAYER_URL:
        raise SystemExit(
            "FEATURE_LAYER_URL is not set. Copy .env.example to .env and "
            "fill in the forest boundary service URL."
        )

    response = requests.get(
        FEATURE_LAYER_URL,
        params={
            "where": "1=1",
            "outFields": "*",
            "outSR": "4326",
            "f": "geojson",
            "token": token,
        },
        timeout=60,
    )
    geojson_data = response.json()

    forest = gpd.GeoDataFrame.from_features(geojson_data["features"])
    forest = forest.set_crs("EPSG:4326", allow_override=True)
    log.info("  Forest loaded: %d polygons", len(forest))

    merged = unary_union(forest.geometry)
    minx, miny, maxx, maxy = merged.bounds

    if miny < -90 or maxy > 0:
        log.warning("  Unexpected coordinates, converting CRS...")
        forest = forest.set_crs("EPSG:3857").to_crs("EPSG:4326")
        merged = unary_union(forest.geometry)
        minx, miny, maxx, maxy = merged.bounds

    log.info("  Extent: Lat %.4f ~ %.4f, Lon %.4f ~ %.4f", miny, maxy, minx, maxx)
    return forest, (minx, miny, maxx, maxy)


# =============================================================================
# STEP 3: Fetch species occurrence data from ALA
# =============================================================================
def slim(occ: dict) -> dict:
    """Keep only the fields we need from a full ALA occurrence record."""
    return {f: occ.get(f, "") for f in NEEDED_FIELDS}


def fetch_range(start_date: str, end_date: str, bounds) -> tuple[list, int]:
    """Fetch records for date range [start_date, end_date).

    Does NOT use 'fl' (so uuid is returned). Slims records in Python.
    Returns (records, total).
    """
    minx, miny, maxx, maxy = bounds
    period_records: list = []
    start_index = 0
    total = 0

    while True:
        ala_data = None
        for attempt in range(MAX_RETRIES):
            try:
                ala_response = requests.get(
                    ALA_SEARCH_URL,
                    params={
                        "q": "*:*",
                        "fq": [
                            f"longitude:[{minx} TO {maxx}]",
                            f"latitude:[{miny} TO {maxy}]",
                            f"eventDate:[{start_date}T00:00:00Z TO {end_date}T00:00:00Z]",
                        ],
                        "pageSize": ALA_PAGE_SIZE,
                        "startIndex": start_index,
                        # NOTE: no 'fl' here on purpose (keeps uuid)
                    },
                    timeout=60,
                )
                ala_data = ala_response.json()
                break
            except requests.RequestException:
                if attempt < MAX_RETRIES - 1:
                    log.info("      Retry %d/%d for %s...", attempt + 1, MAX_RETRIES, start_date)
                    time.sleep(RETRY_DELAY)
                else:
                    log.warning("      Skipped %s after %d attempts", start_date, MAX_RETRIES)
                    ala_data = None

        if ala_data is None:
            break

        batch = ala_data.get("occurrences", [])
        total = ala_data.get("totalRecords", 0)

        if not batch:
            break

        period_records.extend(slim(o) for o in batch)

        if len(period_records) >= total or len(period_records) >= ALA_MAX_WINDOW:
            break

        start_index += ALA_PAGE_SIZE
        time.sleep(REQUEST_DELAY)

    return period_records, total


def fetch_smart(start_date: str, end_date: str, bounds, depth: int = 0) -> list:
    """Fetch a date range, splitting recursively only if it exceeds 5000 records."""
    recs, total = fetch_range(start_date, end_date, bounds)
    indent = "    " + "  " * depth

    if total <= ALA_MAX_WINDOW:
        if recs:
            log.info("%s%s to %s: %d/%d records", indent, start_date, end_date, len(recs), total)
        return recs

    log.info("%s%s to %s: %d records - splitting...", indent, start_date, end_date, total)

    d1 = datetime.strptime(start_date, "%Y-%m-%d")
    d2 = datetime.strptime(end_date, "%Y-%m-%d")
    mid = d1 + (d2 - d1) / 2
    mid_str = mid.strftime("%Y-%m-%d")

    if mid_str in (start_date, end_date):
        log.warning("%s  (single day >5000, keeping first %d)", indent, len(recs))
        return recs

    left = fetch_smart(start_date, mid_str, bounds, depth + 1)
    right = fetch_smart(mid_str, end_date, bounds, depth + 1)
    return left + right


def build_month_list(full_update: bool) -> list[tuple[int, int]]:
    """Return the list of (year, month) tuples to fetch."""
    now = datetime.now()
    months: list[tuple[int, int]] = []

    if full_update:
        for year in range(ALA_START_YEAR, now.year + 1):
            for month in range(1, 13):
                if year == now.year and month > now.month:
                    break
                months.append((year, month))
    else:
        y, m = now.year, now.month
        for _ in range(RECENT_MONTHS):
            months.append((y, m))
            m -= 1
            if m == 0:
                m = 12
                y -= 1
        months.reverse()

    return months


def fetch_all(full_update: bool, bounds) -> pd.DataFrame:
    """Fetch all months and return a DataFrame of slimmed records."""
    months_to_fetch = build_month_list(full_update)
    log.info("STEP 3: Fetching %d month(s) from ALA...", len(months_to_fetch))
    log.info("  (Only months that exceed %d get split)", ALA_MAX_WINDOW)

    all_records: list = []
    for year, month in months_to_fetch:
        start_date = f"{year}-{month:02d}-01"
        if month == 12:
            end_date = f"{year + 1}-01-01"
        else:
            end_date = f"{year}-{month + 1:02d}-01"

        month_recs = fetch_smart(start_date, end_date, bounds)
        if month_recs:
            all_records.extend(month_recs)
            log.info(
                "  %d-%02d DONE: %d records (Total so far: %d)",
                year, month, len(month_recs), len(all_records),
            )
        time.sleep(REQUEST_DELAY)

    records = pd.DataFrame(all_records)
    log.info("  Total fetched: %d records", len(records))
    return records


# =============================================================================
# STEP 4: Filter records to forest boundary
# =============================================================================
def filter_records(records: pd.DataFrame, forest) -> gpd.GeoDataFrame:
    """Deduplicate, validate coordinates, clip to forest, drop marine species."""
    log.info("STEP 4: Filtering records within forest boundary...")

    if len(records) == 0:
        raise SystemExit("No records found! Nothing to update.")

    if "uuid" in records.columns:
        before_dedup = len(records)
        records = records[records["uuid"].astype(str) != ""]
        records = records.drop_duplicates(subset="uuid", keep="first")
        if before_dedup != len(records):
            log.info("  Removed %d duplicate/blank uuids from fetch", before_dedup - len(records))

    records["decimalLatitude"] = pd.to_numeric(records["decimalLatitude"], errors="coerce")
    records["decimalLongitude"] = pd.to_numeric(records["decimalLongitude"], errors="coerce")
    records = records.dropna(subset=["decimalLatitude", "decimalLongitude"])
    log.info("  Valid coordinates: %d records", len(records))

    gdf = gpd.GeoDataFrame(
        records,
        geometry=gpd.points_from_xy(records["decimalLongitude"], records["decimalLatitude"]),
        crs="EPSG:4326",
    )

    result = gpd.sjoin(gdf, forest[["geometry"]], predicate="within")
    result = result.drop(columns=["index_right"])
    log.info("  Records within forest boundary: %d", len(result))

    marine_mask = ~result["scientificName"].str.contains(
        "|".join(MARINE_BLACKLIST), case=False, na=False
    )
    removed = len(result) - marine_mask.sum()
    result = result[marine_mask]
    log.info("  After marine species filter: %d records (%d removed)", len(result), removed)

    if len(result) == 0:
        raise SystemExit("No valid records remaining. Nothing to update.")

    return result


# =============================================================================
# STEP 5: Format data + CSV accumulate
# =============================================================================
def get_photo_url(images_val) -> str:
    """Build a thumbnail URL from ALA's images list, if present."""
    try:
        if isinstance(images_val, list) and len(images_val) > 0:
            img_id = images_val[0]
            return f"https://images.ala.org.au/image/proxyImageThumbnail?imageId={img_id}"
    except (TypeError, IndexError):
        pass
    return ""


def format_records(result: gpd.GeoDataFrame) -> pd.DataFrame:
    """Add photo URL, conservation columns, and a display date; return tidy rows."""
    log.info("STEP 5: Formatting data...")
    result = result.copy()

    if "images" in result.columns:
        result["photoURL"] = result["images"].apply(get_photo_url)
    else:
        result["photoURL"] = ""

    for col in ["stateConservation", "austConservation", "uuid"]:
        if col not in result.columns:
            result[col] = ""
        result[col] = result[col].fillna("").astype(str)

    result["eventDate_display"] = pd.to_datetime(
        result["eventDate"], unit="ms", origin="unix", errors="coerce"
    ).dt.strftime("%d/%m/%Y")

    new_rows = result[[
        "uuid", "scientificName", "vernacularName",
        "decimalLatitude", "decimalLongitude", "eventDate_display",
        "photoURL", "stateConservation", "austConservation",
    ]].copy()
    new_rows = new_rows.rename(columns={"eventDate_display": "eventDate"})
    new_rows["dataSource"] = "ALA"
    return new_rows


def update_csv(new_rows: pd.DataFrame, full_update: bool) -> None:
    """Write the CSV in accumulate mode (weekly) or fresh (full)."""
    log.info("STEP 5b: Updating CSV (accumulate mode)...")

    if full_update:
        combined = new_rows
        log.info("  Full update: writing a fresh CSV.")
    elif os.path.exists(OUTPUT_CSV):
        try:
            old_rows = pd.read_csv(OUTPUT_CSV)
            before = len(old_rows)
            combined = pd.concat([old_rows, new_rows], ignore_index=True)
            if "uuid" in combined.columns:
                combined = combined.drop_duplicates(subset="uuid", keep="first")
            log.info("  Existing CSV had %d rows.", before)
            log.info("  Added %d new rows, %d total after dedup.", len(new_rows), len(combined))
        except (OSError, pd.errors.ParserError) as e:
            log.warning("  Could not read existing CSV (%s). Writing new one.", e)
            combined = new_rows
    else:
        combined = new_rows
        log.info("  No existing CSV found. Creating a new one.")

    try:
        combined.to_csv(OUTPUT_CSV, index=False)
        log.info("  CSV saved: %s", OUTPUT_CSV)
    except PermissionError:
        log.warning("  PERMISSION ERROR: the CSV file is open (e.g. in Excel).")
        log.warning("  Please close it and run again. (ArcGIS upload below still runs.)")


# =============================================================================
# STEP 6: Update ArcGIS Online Feature Layer
# =============================================================================
def build_features(records_to_add: gpd.GeoDataFrame) -> list:
    """Convert rows into ArcGIS Feature objects."""
    features = []
    for _, row in records_to_add.iterrows():
        try:
            date_ms = int(pd.to_numeric(row["eventDate"], errors="coerce"))
        except (ValueError, TypeError):
            date_ms = None

        features.append(
            Feature(
                geometry=Point({
                    "x": float(row["decimalLongitude"]),
                    "y": float(row["decimalLatitude"]),
                    "spatialReference": {"wkid": 4326},
                }),
                attributes={
                    "scientificName": str(row.get("scientificName", "")),
                    "vernacularName": str(row.get("vernacularName", "")),
                    "decimalLatitude": float(row.get("decimalLatitude", 0)),
                    "decimalLongitude": float(row.get("decimalLongitude", 0)),
                    "eventDate": date_ms,
                    "dataSource": "ALA",
                    "photoURL": str(row.get("photoURL", "")),
                    "uuid": str(row.get("uuid", "")),
                    "stateConservation": str(row.get("stateConservation", "")),
                    "austConservation": str(row.get("austConservation", "")),
                },
            )
        )
    return features


def push_to_arcgis(gis: GIS, result: gpd.GeoDataFrame, full_update: bool) -> None:
    """Update an existing Feature Layer, or create one from the CSV if none exists."""
    log.info("STEP 6: Pushing data to ArcGIS Online...")

    existing_items = gis.content.search(query=f"title:{LAYER_TITLE}", item_type="Feature Service")

    if existing_items:
        log.info("  Existing layer found...")
        existing_layer = existing_items[0].layers[0]

        if full_update:
            existing_layer.delete_features(where="1=1")
            log.info("  Old records cleared (full update).")
            records_to_add = result
        else:
            log.info("  Checking for existing records...")
            try:
                existing_df = existing_layer.query(where="1=1", out_fields=["uuid"]).sdf
                existing_ids = set(existing_df["uuid"].dropna().astype(str).tolist())
                records_to_add = result[~result["uuid"].astype(str).isin(existing_ids)]
                skipped = len(result) - len(records_to_add)
                log.info("  New records to add: %d (skipped %d duplicates)", len(records_to_add), skipped)
            except Exception as e:  # noqa: BLE001 - ArcGIS raises varied errors here
                records_to_add = result
                log.warning("  Could not check duplicates (%s), adding all records...", e)

        if len(records_to_add) == 0:
            log.info("  No new records to add. Layer is already up to date!")
            return

        features_to_add = build_features(records_to_add)
        added = 0
        for i in range(0, len(features_to_add), UPLOAD_BATCH_SIZE):
            chunk = features_to_add[i:i + UPLOAD_BATCH_SIZE]
            existing_layer.edit_features(adds=chunk)
            added += len(chunk)
            log.info("  Uploaded %d/%d...", added, len(features_to_add))

        log.info("  Successfully added %d new records!", len(features_to_add))

    else:
        log.info("  No existing layer found - creating new one...")
        csv_item = gis.content.add(
            item_properties={
                "title": LAYER_TITLE,
                "type": "CSV",
                "tags": "ALA, species, biodiversity",
                "description": "Species occurrence data from Atlas of Living Australia",
            },
            data=OUTPUT_CSV,
        )
        published = csv_item.publish(
            publish_parameters={
                "type": "csv",
                "locationType": "coordinates",
                "latitudeFieldName": "decimalLatitude",
                "longitudeFieldName": "decimalLongitude",
                "coordinateFieldType": "LatLong",
            }
        )
        log.info("  New Feature Layer created!")
        log.info("  URL: %s", published.url)


# =============================================================================
# Orchestration
# =============================================================================
def run(full_update: bool) -> None:
    mode = "FULL UPDATE" if full_update else "WEEKLY UPDATE"
    detail = (
        f"fetching all records from {ALA_START_YEAR}"
        if full_update
        else f"checking last {RECENT_MONTHS} months"
    )
    log.info("=" * 60)
    log.info("MODE: %s (%s)", mode, detail)
    log.info("=" * 60)

    if not check_ala_health():
        raise SystemExit(1)

    gis, token = login_arcgis()
    forest, bounds = load_forest_boundary(token)
    records = fetch_all(full_update, bounds)
    result = filter_records(records, forest)
    new_rows = format_records(result)
    update_csv(new_rows, full_update)
    push_to_arcgis(gis, result, full_update)

    log.info("=" * 60)
    log.info("DONE! ALA species data successfully synced to ArcGIS Online.")
    log.info("Mode: %s", "Full update" if full_update else "Weekly update")
    log.info("=" * 60)


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--full",
        action="store_true",
        help="Full re-fetch from ALA_START_YEAR and replace all data "
             "(default: weekly update of recent months).",
    )
    parser.add_argument(
        "--log-level",
        default=os.getenv("LOG_LEVEL", "INFO"),
        help="Logging level (DEBUG, INFO, WARNING, ERROR). Default: INFO.",
    )
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout,
    )
    try:
        run(full_update=args.full)
    except SystemExit as e:
        if e.code not in (None, 0):
            log.error("Stopped: %s", e.code if isinstance(e.code, str) else "see messages above")
        return 1 if e.code not in (None, 0) else 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
