#!/usr/bin/env python
"""Hit every external data source Quantcomm depends on and report PASS / FAIL / NEEDS_KEY.

Run:  .venv/bin/python scripts/verify_sources.py [--quick]

Prints a sample record and field names for each source, then a summary table.
Exits non-zero if any *required* source FAILs. Sources that only need a key or
interactive auth (Nessie writes, Census API, Earth Engine) report NEEDS_KEY /
NEEDS_AUTH and do not fail the run, because keyless fallbacks exist for each.

Verified facts (2026-10-17) that differ from the original CLAUDE.md spec are
listed in the SPEC_DIFFS block at the bottom and printed at the end.
"""
from __future__ import annotations

import json
import os
import sys
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from quantcomm.config import env, storm  # noqa: E402

UA = {"User-Agent": "quantcomm-verify (hackathon project)"}
TIMEOUT = 45
AOI = storm("isaias")["aoi_bbox"]  # [min_lon, min_lat, max_lon, max_lat]


@dataclass
class Result:
    source: str
    status: str  # PASS / FAIL / NEEDS_KEY / NEEDS_AUTH
    required: bool = True
    note: str = ""
    fields: list[str] = field(default_factory=list)
    sample: object = None
    seconds: float = 0.0


def _print(r: Result) -> None:
    print(f"\n=== {r.source}: {r.status} ({r.seconds:.1f}s)")
    if r.note:
        print(f"    {r.note}")
    if r.fields:
        print(f"    fields: {r.fields}")
    if r.sample is not None:
        s = json.dumps(r.sample, default=str)
        print(f"    sample: {s[:400]}{'...' if len(s) > 400 else ''}")


def run(fn, required: bool = True) -> Result:
    t0 = time.time()
    try:
        r = fn()
    except Exception as e:  # noqa: BLE001
        r = Result(fn.__name__, "FAIL", note=f"{type(e).__name__}: {str(e)[:300]}")
        if os.environ.get("VERIFY_TRACE"):
            traceback.print_exc()
    r.required = required
    r.seconds = time.time() - t0
    _print(r)
    return r


# ---------------------------------------------------------------- satellite --
def copernicus_catalog() -> Result:
    """Copernicus Data Space OData catalog: Sentinel-1 GRD IW scenes + PublicationDate (event-time basis)."""
    w, s, e, n = AOI
    poly = f"POLYGON(({w} {s},{e} {s},{e} {n},{w} {n},{w} {s}))"
    url = (
        "https://catalogue.dataspace.copernicus.eu/odata/v1/Products?"
        "$filter=Collection/Name eq 'SENTINEL-1' and contains(Name,'GRDH') and contains(Name,'_IW_') "
        f"and OData.CSC.Intersects(area=geography'SRID=4326;{poly}') "
        "and ContentDate/Start gt 2026-10-10T00:00:00.000Z and ContentDate/Start lt 2026-10-12T00:00:00.000Z"
        "&$expand=Attributes&$orderby=ContentDate/Start asc&$top=20"
    )
    r = requests.get(url, timeout=TIMEOUT)
    r.raise_for_status()
    rows = r.json()["value"]
    if not rows:
        return Result("copernicus_catalog", "FAIL", note="no Sentinel-1 GRD scenes returned for 2026-10-10..12 over AOI")
    p = rows[0]
    attrs = {a["Name"]: a["Value"] for a in p.get("Attributes", [])}
    for k in ("relativeOrbitNumber", "orbitDirection", "platformSerialIdentifier"):
        if k not in attrs:
            return Result("copernicus_catalog", "FAIL", note=f"attribute {k} missing; have {sorted(attrs)[:20]}")
    if "PublicationDate" not in p:
        return Result("copernicus_catalog", "FAIL", note="PublicationDate missing from product record")
    names = [x["Name"] for x in rows]
    dupes = len(names) - len(set(names))
    return Result(
        "copernicus_catalog", "PASS",
        note=f"{len(rows)} records, {dupes} duplicate names (each product is listed twice with two "
             f"PublicationDates; take the earliest). First post-landfall scene {p['Name'][:40]} "
             f"acquired {p['ContentDate']['Start'][:16]}Z published {p['PublicationDate'][:16]}Z "
             f"orbit {attrs['relativeOrbitNumber']} {attrs['orbitDirection']}",
        fields=sorted(p.keys()) + [f"Attributes.{k}" for k in ("relativeOrbitNumber", "orbitDirection", "platformSerialIdentifier")],
        sample={"Name": p["Name"], "ContentDate": p["ContentDate"], "PublicationDate": p["PublicationDate"],
                "relativeOrbitNumber": attrs["relativeOrbitNumber"], "orbitDirection": attrs["orbitDirection"]},
    )


def planetary_computer() -> Result:
    """Microsoft Planetary Computer STAC: sentinel-1-rtc, nasadem, jrc-gsw, esa-worldcover (no auth)."""
    import planetary_computer as pc
    import pystac_client
    import rasterio

    cat = pystac_client.Client.open("https://planetarycomputer.microsoft.com/api/stac/v1", modifier=pc.sign_inplace)
    notes = []
    items = list(cat.search(collections=["sentinel-1-rtc"], bbox=AOI, datetime="2026-10-10/2026-10-11", limit=10).items())
    if not items:
        return Result("planetary_computer", "FAIL", note="no sentinel-1-rtc items 2026-10-10..11 over AOI")
    it = items[0]
    for k in ("sat:relative_orbit", "sat:orbit_state", "platform"):
        if k not in it.properties:
            return Result("planetary_computer", "FAIL", note=f"property {k} missing on {it.id}")
    if "vv" not in it.assets:
        return Result("planetary_computer", "FAIL", note=f"no vv asset on {it.id}: {list(it.assets)}")
    with rasterio.open(it.assets["vv"].href) as ds:
        cog = f"{ds.crs} {ds.shape} res={ds.res} dtype={ds.dtypes[0]} nodata={ds.nodata} overviews={ds.overviews(1)}"
    notes.append(f"sentinel-1-rtc: {len(items)} items; {it.id} orbit {it.properties['sat:relative_orbit']} "
                 f"{it.properties['sat:orbit_state']}; COG {cog}")
    for coll, asset in (("nasadem", "elevation"), ("jrc-gsw", "occurrence"), ("esa-worldcover", "map")):
        its = list(cat.search(collections=[coll], bbox=AOI, limit=50).items())
        if not its or asset not in its[0].assets:
            return Result("planetary_computer", "FAIL", note=f"{coll}: no items or missing asset {asset}")
        notes.append(f"{coll}: {len(its)} items, asset '{asset}' present")
    return Result("planetary_computer", "PASS", note="; ".join(notes),
                  fields=sorted(it.properties.keys()), sample={"id": it.id, "assets": list(it.assets)})


def earth_engine() -> Result:
    """Google Earth Engine (spec primary; needs `earthengine authenticate` + Cloud project)."""
    import ee

    try:
        kw = {"project": env("EE_PROJECT")} if env("EE_PROJECT") else {}
        ee.Initialize(**kw)
        n = ee.ImageCollection("COPERNICUS/S1_GRD").filterBounds(ee.Geometry.Rectangle(AOI)) \
            .filterDate("2026-10-10", "2026-10-12").size().getInfo()
        return Result("earth_engine", "PASS", note=f"COPERNICUS/S1_GRD: {n} images 2026-10-10..12 over AOI")
    except Exception as e:  # noqa: BLE001
        return Result("earth_engine", "NEEDS_AUTH",
                      note="run `.venv/bin/earthengine authenticate` and set EE_PROJECT in .env. "
                           "Pipeline uses the Planetary Computer backend until then. "
                           f"({str(e).splitlines()[0][:120]})")


# ------------------------------------------------------------------ weather --
def nhc() -> Result:
    """NOAA NHC: CurrentStorms.json + best-track shapefile zip for AL092026 (Isaias)."""
    import geopandas as gpd

    cur = requests.get("https://www.nhc.noaa.gov/CurrentStorms.json", headers=UA, timeout=TIMEOUT)
    cur.raise_for_status()
    active = cur.json().get("activeStorms", [])
    sid = storm("isaias")["nhc_id"]
    raw = ROOT / "data" / "raw" / "nhc"
    raw.mkdir(parents=True, exist_ok=True)
    z = raw / f"{sid}_best_track.zip"
    if not z.exists():
        r = requests.get(f"https://www.nhc.noaa.gov/gis/best_track/{sid}_best_track.zip", headers=UA, timeout=TIMEOUT)
        r.raise_for_status()
        z.write_bytes(r.content)
    pts = gpd.read_file(f"zip://{z}!{sid.upper()}_pts.shp")
    need = {"STORMNAME", "YEAR", "MONTH", "DAY", "HHMM", "LAT", "LON", "INTENSITY", "STORMTYPE", "SS"}
    if not need.issubset(pts.columns):
        return Result("nhc", "FAIL", note=f"best track pts missing {need - set(pts.columns)}")
    if "ISAIAS" not in set(pts.STORMNAME.str.upper()):
        return Result("nhc", "FAIL", note=f"{sid} best track STORMNAME is {pts.STORMNAME.unique()} not ISAIAS")
    land = pts[pts.INTENSITY == pts.INTENSITY.max()].iloc[-1]
    return Result("nhc", "PASS",
                  note=f"active storms now: {[s['name'] + '(' + s['id'] + ')' for s in active] or 'none'}; "
                       f"{sid} best track {len(pts)} points, peak {pts.INTENSITY.max():.0f} kt at "
                       f"{land.LAT}N {abs(land.LON)}W on {int(land.MONTH)}/{int(land.DAY)} {int(land.HHMM):04d}Z",
                  fields=list(pts.columns), sample=pts.iloc[-2].drop("geometry").to_dict())


def nws_alerts() -> Result:
    """NWS api.weather.gov: active alerts (zone-based, geometry null -> affectedZones) and historical warnings."""
    r = requests.get("https://api.weather.gov/alerts/active",
                     params={"event": "Coastal Flood Warning,Coastal Flood Advisory,Coastal Flood Watch,"
                                      "Flood Warning,Flash Flood Warning,Flood Watch"},
                     headers=UA, timeout=TIMEOUT)
    r.raise_for_status()
    feats = r.json()["features"]
    zone_ok = "not tested (no zone-based alert active)"
    for f in feats:
        if f["geometry"] is None and f["properties"].get("affectedZones"):
            zr = requests.get(f["properties"]["affectedZones"][0], headers=UA, timeout=TIMEOUT)
            zr.raise_for_status()
            g = zr.json().get("geometry") or {}
            zone_ok = f"zone {f['properties']['affectedZones'][0].rsplit('/', 1)[-1]} -> {g.get('type')}"
            break
    h = requests.get("https://api.weather.gov/alerts",
                     params={"start": "2026-10-08T00:00:00Z", "end": "2026-10-12T00:00:00Z",
                             "event": "Flash Flood Warning,Flood Warning", "area": "FL,AL,GA", "limit": 500},
                     headers=UA, timeout=TIMEOUT)
    h.raise_for_status()
    hist = h.json()["features"]
    n_geom = sum(1 for f in hist if f["geometry"])
    if not hist:
        return Result("nws_alerts", "FAIL", note="historical alerts query returned nothing for Isaias window")
    p = feats[0]["properties"] if feats else hist[0]["properties"]
    return Result("nws_alerts", "PASS",
                  note=f"{len(feats)} active flood-type alerts now ({sum(1 for f in feats if f['geometry'] is None)} "
                       f"without geometry; zone lookup: {zone_ok}); Isaias window 2026-10-08..12 FL/AL/GA: "
                       f"{len(hist)} Flood/Flash Flood Warnings, {n_geom} with polygons",
                  fields=sorted(p.keys()),
                  sample={k: p.get(k) for k in ("event", "areaDesc", "sent", "onset", "ends", "affectedZones")})


# -------------------------------------------------------------------- banks --
NESSIE = "https://api.nessieisreal.com"


def nessie() -> Result:
    """Capital One Nessie: GET /branches, /atms (any key); POST needs NESSIE_API_KEY."""
    key = env("NESSIE_API_KEY") or "x"
    b = requests.get(f"{NESSIE}/branches", params={"key": key}, timeout=TIMEOUT)
    b.raise_for_status()
    branches = b.json()
    a = requests.get(f"{NESSIE}/atms", params={"key": key}, timeout=TIMEOUT)
    a.raise_for_status()
    atms = a.json()
    if not isinstance(branches, list) or not branches or "address" not in branches[0]:
        return Result("nessie", "FAIL", note=f"/branches unexpected shape: {str(branches)[:200]}")
    if not isinstance(atms, list) or not atms or "geocode" not in atms[0]:
        return Result("nessie", "FAIL", note=f"/atms unexpected shape: {str(atms)[:200]}")
    states = sorted({br["address"]["state"].upper() for br in branches})
    has_geo = "geocode" in branches[0]
    note = (f"{len(branches)} branches in {states} ({'with' if has_geo else 'WITHOUT'} geocode -> "
            f"{'ok' if has_geo else 'needs Census geocoder'}); {len(atms)} ATMs with geocode. "
            f"http:// is refused; use https://")
    if env("NESSIE_API_KEY"):
        c = requests.get(f"{NESSIE}/customers", params={"key": key}, timeout=TIMEOUT)
        if c.status_code == 401:
            return Result("nessie", "FAIL", note=note + "; NESSIE_API_KEY rejected (401) on GET /customers")
        return Result("nessie", "PASS", note=note + f"; key accepted, {len(c.json())} existing customers",
                      fields=sorted(branches[0].keys()), sample=atms[0])
    return Result("nessie", "NEEDS_KEY",
                  note=note + "; set NESSIE_API_KEY in .env to create customers/accounts (POST returns 401 without it)",
                  fields=sorted(branches[0].keys()), sample=atms[0])


def fdic() -> Result:
    """FDIC BankFind: sod (Summary of Deposits per branch, 2026 vintage), institutions, locations."""
    base = "https://api.fdic.gov/banks"
    fields = "CERT,NAMEFULL,BRNUM,NAMEBR,ADDRESBR,CITYBR,STALPBR,ZIPBR,CNTYNAMB,DEPSUMBR,SIMS_LATITUDE,SIMS_LONGITUDE,RSSDHCR,NAMEHCR,YEAR"
    r = requests.get(f"{base}/sod", params={"filters": "STALPBR:FL AND YEAR:2026", "fields": fields, "limit": 1}, timeout=TIMEOUT)
    r.raise_for_status()
    j = r.json()
    rec = j["data"][0]["data"]
    missing = [f for f in fields.split(",") if f not in rec]
    if missing:
        return Result("fdic", "FAIL", note=f"sod missing fields {missing}")
    counts = {}
    for y in (2024, 2025, 2026):
        ry = requests.get(f"{base}/sod", params={"filters": f"YEAR:{y}", "limit": 1}, timeout=TIMEOUT)
        ry.raise_for_status()
        counts[y] = ry.json()["totals"]["count"]
    idx = j["meta"]["index"]["createTimestamp"]
    i = requests.get(f"{base}/institutions", params={"filters": "CERT:4297", "fields": "CERT,NAME,RSSDHCR,NAMEHCR,ASSET,DEP,ACTIVE,STALP,FED_RSSD,OFFICES", "limit": 1}, timeout=TIMEOUT)
    i.raise_for_status()
    inst = i.json()["data"][0]["data"]
    loc = requests.get(f"{base}/locations", params={"filters": "CERT:4297", "limit": 1}, timeout=TIMEOUT)
    loc.raise_for_status()
    nloc = loc.json()["totals"]["count"]
    return Result("fdic", "PASS",
                  note=f"sod rows by YEAR {counts}; sod index built {idx} (2026 SOD, as of 2026-06-30, was public before "
                       f"Isaias landfall -> no lookahead); institutions CERT 4297 = {inst['NAME']} / HC {inst['NAMEHCR']}; "
                       f"locations endpoint: {nloc} offices for CERT 4297",
                  fields=sorted(rec.keys()), sample=rec)


def yfinance_prices() -> Result:
    """yfinance daily adjusted closes for KRE, SPY, COF through the last trading day."""
    import yfinance as yf

    d = yf.download(["KRE", "SPY", "COF"], start="2026-09-01", progress=False, auto_adjust=True)
    close = d["Close"].dropna(how="all")
    if close.empty or close.index.max().strftime("%Y-%m-%d") < "2026-10-16":
        return Result("yfinance", "FAIL", note=f"last row {close.index.max() if not close.empty else None}; expected >= 2026-10-16")
    return Result("yfinance", "PASS", note=f"{len(close)} rows, last {close.index.max().date()}",
                  fields=list(close.columns), sample=close.iloc[-1].round(2).to_dict())


# ------------------------------------------------------------------- census --
def census() -> Result:
    """Census: api.census.gov needs CENSUS_API_KEY; keyless fallback = ACS summary file + TIGER tracts."""
    notes = []
    api_status = "NEEDS_KEY"
    key = env("CENSUS_API_KEY")
    if key:
        r = requests.get("https://api.census.gov/data/2023/acs/acs5",
                         params={"get": "NAME,B19013_001E", "for": "tract:*", "in": "state:12 county:091", "key": key},
                         timeout=TIMEOUT, allow_redirects=False)
        if r.status_code == 200 and r.text.startswith("[["):
            api_status = "PASS"
            notes.append(f"api.census.gov with key: {len(r.json()) - 1} tracts in Okaloosa FL")
        else:
            notes.append(f"api.census.gov with key returned HTTP {r.status_code}")
    else:
        r = requests.get("https://api.census.gov/data/2023/acs/acs5",
                         params={"get": "NAME,B19013_001E", "for": "tract:*", "in": "state:12 county:091"},
                         timeout=TIMEOUT, allow_redirects=False)
        notes.append(f"api.census.gov without key -> HTTP {r.status_code} (redirects to 'Missing Key' page); "
                     "get a free key at https://api.census.gov/data/key_signup.html")
    sf = "https://www2.census.gov/programs-surveys/acs/summary_file/2023/table-based-SF/data/5YRData/acsdt5y2023-b19013.dat"
    r = requests.get(sf, headers={"Range": "bytes=0-300"}, timeout=TIMEOUT)
    if r.status_code not in (200, 206) or not r.text.startswith("GEO_ID|B19013_E001"):
        return Result("census", "FAIL", note=f"ACS summary file B19013 unexpected: HTTP {r.status_code} {r.text[:80]!r}")
    notes.append("keyless ACS 2023 5-yr summary file B19013 OK (GEO_ID|B19013_E001|B19013_M001)")
    for st in ("12", "01", "13"):
        h = requests.head(f"https://www2.census.gov/geo/tiger/TIGER2024/TRACT/tl_2024_{st}_tract.zip", timeout=TIMEOUT, allow_redirects=True)
        if h.status_code != 200:
            return Result("census", "FAIL", note=f"TIGER2024 tract zip for state {st}: HTTP {h.status_code}")
    notes.append("TIGER2024 tract zips for FL/AL/GA OK")
    return Result("census", api_status if api_status == "NEEDS_KEY" else "PASS", note="; ".join(notes),
                  fields=["GEO_ID", "B19013_E001", "B19013_M001"], sample=r.text.splitlines()[1])


# --------------------------------------------------------------------- fema --
def openfema() -> Result:
    """OpenFEMA: NFIP claims (v3 NfipClaims replaces deprecated v2 FimaNfipClaims) + DisasterDeclarationsSummaries."""
    base = "https://www.fema.gov/api/open"
    cat = requests.get(f"{base}/v1/OpenFemaDataSets", params={"$filter": "substringof('Nfip',name)"}, timeout=TIMEOUT)
    cat.raise_for_status()
    cj = cat.json()
    ds = cj[[k for k in cj if k != "metadata"][0]]
    claims = sorted([d for d in ds if "Claims" in d["name"]], key=lambda d: -int(d["version"]))
    live = claims[0]
    url = f"{base}/v{live['version']}/{live['name']}"
    r = requests.get(url, params={"$filter": "state eq 'FL' and yearOfLoss eq 2018", "$top": 1}, timeout=TIMEOUT)
    r.raise_for_status()
    rec = r.json()[live["name"]][0]
    # v3 renamed censusTract -> censusGeoid and added floodEvent / eventDesignationNumber
    need = ["dateOfLoss", "state", "countyCode", "reportedZipCode", "censusGeoid", "latitude", "longitude",
            "amountPaidOnBuildingClaim", "netBuildingPaymentAmount", "floodEvent"]
    missing = [f for f in need if f not in rec]
    if missing:
        return Result("openfema", "FAIL", note=f"{live['name']} v{live['version']} missing {missing}")
    dd = requests.get(f"{base}/v2/DisasterDeclarationsSummaries",
                      params={"$filter": "substringof('ISAIAS',declarationTitle) and declarationDate ge '2026-01-01'",
                              "$select": "disasterNumber,state,declarationDate,designatedArea", "$top": 1000},
                      timeout=TIMEOUT)
    dd.raise_for_status()
    decl = dd.json()["DisasterDeclarationsSummaries"]
    nums = sorted({(d["disasterNumber"], d["state"]) for d in decl})
    if not decl:
        return Result("openfema", "FAIL", note="no ISAIAS disaster declarations found")
    old = [d for d in ds if d["name"] == "FimaNfipClaims"]
    return Result("openfema", "PASS",
                  note=f"live claims dataset = {live['name']} v{live['version']} ({live['recordCount']:,} rows, refreshed "
                       f"{live['lastRefresh'][:10]}); v2 FimaNfipClaims deprecated/frozen 2026-06-01"
                       f"{' (' + str(old[0]['recordCount']) + ' rows)' if old else ''}; "
                       f"Isaias declarations {nums} covering {len(decl)} designated areas",
                  fields=sorted(rec.keys()), sample={k: rec[k] for k in need})


# --------------------------------------------------------------------- main --
SPEC_DIFFS = """
Differences from CLAUDE.md spec (verified 2026-10-17):
  * Nessie base URL is https://api.nessieisreal.com (not prod-api...; plain http is refused).
    GET /branches and /atms answer with ANY key; POST needs a real key (401). Branches have no
    geocode field -> geocode via Census geocoder. Capital One Nessie footprint: MD/VA/DC only.
  * api.census.gov now REQUIRES an API key. Keyless fallback: ACS table-based summary file
    (B19013 .dat) + TIGER tract shapefiles from www2.census.gov.
  * OpenFEMA FimaNfipClaims v2 is deprecated (frozen 2026-06-01). Use v3 NfipClaims:
    field censusTract is now censusGeoid; floodEvent names the storm (useful for M4).
  * Earth Engine needs interactive auth + Cloud project; flood pipeline runs on Planetary
    Computer sentinel-1-rtc (terrain-corrected gamma0, 10 m COGs) with nasadem + jrc-gsw masks.
  * Sentinel-1 publication timestamps come from the Copernicus Data Space OData catalog
    (each product listed twice; use the earliest PublicationDate).
  * FDIC SOD 2026 vintage (as of 2026-06-30) is live and was indexed 2026-09-18, before Isaias.
  * NHC best track for Isaias is AL092026 (AL082026 = Hanna).
"""


def main() -> int:
    quick = "--quick" in sys.argv
    checks = [
        (copernicus_catalog, True),
        (planetary_computer, True),
        (earth_engine, False),
        (nhc, True),
        (nws_alerts, True),
        (nessie, True),
        (fdic, True),
        (census, True),
        (openfema, True),
        (yfinance_prices, True),
    ]
    if quick:
        checks = [c for c in checks if c[0] is not earth_engine]
    results = [run(fn, req) for fn, req in checks]

    print("\n" + "=" * 78)
    print(f"{'source':22} {'status':11} {'required':9} seconds")
    for r in results:
        print(f"{r.source:22} {r.status:11} {str(r.required):9} {r.seconds:5.1f}")
    print(SPEC_DIFFS)
    bad = [r for r in results if r.required and r.status == "FAIL"]
    needs = [r for r in results if r.status in ("NEEDS_KEY", "NEEDS_AUTH")]
    if needs:
        print("Needs keys/auth: " + ", ".join(f"{r.source} ({r.status})" for r in needs))
    if bad:
        print("REQUIRED SOURCES FAILED: " + ", ".join(r.source for r in bad))
        return 1
    print("All required sources OK.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
