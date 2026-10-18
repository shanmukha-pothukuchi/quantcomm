"""Capital One Nessie API client.

Verified 2026-10-17:
  * base URL https://api.nessieisreal.com (plain http refuses connections; prod-api.* is the same hosts)
  * key is passed as ?key=; GET /branches and /atms return data with any key, POST returns
    401 "Invalid API key" without a real one.
  * /branches records have no geocode (address only) -> geocoded with the Census batch geocoder.
  * /atms records carry geocode{lat,lng}.
Request bodies for customers/accounts/loans/bills were verified by a live push of the full SYNTHETIC
portfolio on 2026-10-17. Deposits/withdrawals/purchases follow the same documented shape but have not
been exercised yet.
"""
from __future__ import annotations

import os

import csv
import io
import json
import logging
import time
from pathlib import Path

import pandas as pd
import requests

from floodalpha import config, schemas

log = logging.getLogger(__name__)

BASE_URL = "https://api.nessieisreal.com"
RAW = config.DATA_RAW / "nessie"
CREATED_IDS = RAW / "created_ids.jsonl"
PREFIX = config.model()["portfolio"]["nickname_prefix"]


class NessieError(RuntimeError):
    pass


class Nessie:
    def __init__(self, key: str | None = None, write_delay_s: float = 0.1, retries: int = 3):
        self.key = key or config.env("NESSIE_API_KEY")
        self.delay = write_delay_s
        self.retries = retries
        self.s = requests.Session()

    # ---- low level -------------------------------------------------------
    def _req(self, method: str, path: str, **kw) -> requests.Response:
        params = kw.pop("params", {}) or {}
        params["key"] = self.key or "none"
        last = None
        for i in range(self.retries):
            try:
                r = self.s.request(method, f"{BASE_URL}{path}", params=params, timeout=60, **kw)
                if r.status_code in (429, 502, 503, 504):
                    raise NessieError(f"{r.status_code} {r.text[:200]}")
                return r
            except (requests.RequestException, NessieError) as e:
                last = e
                time.sleep(1.5 * (i + 1))
        raise NessieError(f"{method} {path} failed after {self.retries} tries: {last}")

    def get(self, path: str, **params):
        r = self._req("GET", path, params=params)
        if r.status_code != 200:
            raise NessieError(f"GET {path} -> {r.status_code} {r.text[:200]}")
        return r.json()

    def post(self, path: str, body: dict, kind: str) -> dict:
        if not self.key:
            raise NessieError("NESSIE_API_KEY is not set; writes need a real key (GETs work without one)")
        time.sleep(self.delay)
        r = self._req("POST", path, json=body)
        if r.status_code not in (200, 201):
            raise NessieError(f"POST {path} -> {r.status_code} {r.text[:300]}")
        data = r.json()
        obj = data.get("objectCreated", data)
        oid = obj.get("_id")
        if oid:
            _record_created(kind, oid, body)
        return obj

    def delete(self, path: str) -> None:
        if not self.key:
            raise NessieError("NESSIE_API_KEY is not set")
        time.sleep(self.delay)
        r = self._req("DELETE", path)
        if r.status_code not in (200, 202, 204, 404):
            raise NessieError(f"DELETE {path} -> {r.status_code} {r.text[:200]}")

    # ---- reads -----------------------------------------------------------
    def branches(self) -> list[dict]:
        return self.get("/branches")

    def atms(self) -> list[dict]:
        return self.get("/atms")

    def merchants(self) -> list[dict]:
        return self.get("/merchants")

    def customers(self) -> list[dict]:
        return self.get("/customers")

    # ---- writes (customer/account/loan/bill bodies verified by a live push on 2026-10-17; deposit/withdrawal/purchase not yet exercised) ----
    def create_customer(self, first_name: str, last_name: str, address: dict) -> dict:
        return self.post("/customers", {"first_name": first_name, "last_name": last_name, "address": address}, "customer")

    def create_account(self, customer_id: str, type_: str, nickname: str, balance: float, rewards: int = 0) -> dict:
        body = {"type": type_, "nickname": nickname, "rewards": rewards, "balance": round(float(balance), 2)}
        return self.post(f"/customers/{customer_id}/accounts", body, "account")

    def create_loan(self, account_id: str, amount: float, monthly_payment: float, credit_score: int,
                    type_: str = "home", status: str = "pending", description: str = "") -> dict:
        body = {"type": type_, "status": status, "credit_score": int(credit_score),
                "monthly_payment": round(float(monthly_payment), 2), "amount": round(float(amount), 2),
                "description": description}
        return self.post(f"/accounts/{account_id}/loans", body, "loan")

    def create_bill(self, account_id: str, payee: str, nickname: str, payment_amount: float,
                    payment_date: str, recurring_date: int, status: str = "pending") -> dict:
        body = {"status": status, "payee": payee, "nickname": nickname, "payment_date": payment_date,
                "recurring_date": int(recurring_date), "payment_amount": round(float(payment_amount), 2)}
        return self.post(f"/accounts/{account_id}/bills", body, "bill")

    def create_deposit(self, account_id: str, amount: float, transaction_date: str, description: str = "") -> dict:
        body = {"medium": "balance", "transaction_date": transaction_date, "status": "pending",
                "amount": round(float(amount), 2), "description": description}
        return self.post(f"/accounts/{account_id}/deposits", body, "deposit")

    def create_withdrawal(self, account_id: str, amount: float, transaction_date: str, description: str = "") -> dict:
        body = {"medium": "balance", "transaction_date": transaction_date, "status": "pending",
                "amount": round(float(amount), 2), "description": description}
        return self.post(f"/accounts/{account_id}/withdrawals", body, "withdrawal")

    def create_merchant(self, name: str, category: str, address: dict, lat: float, lng: float) -> dict:
        body = {"name": name, "category": category, "address": address, "geocode": {"lat": lat, "lng": lng}}
        return self.post("/merchants", body, "merchant")

    def create_purchase(self, account_id: str, merchant_id: str, amount: float, purchase_date: str,
                        description: str = "") -> dict:
        body = {"merchant_id": merchant_id, "medium": "balance", "purchase_date": purchase_date,
                "amount": round(float(amount), 2), "status": "pending", "description": description}
        return self.post(f"/accounts/{account_id}/purchases", body, "purchase")

    # ---- cleanup ---------------------------------------------------------
    def cleanup(self) -> int:
        """Delete every object we created (from created_ids.jsonl) whose body carries the project prefix.
        Deleting a customer cascades its accounts in Nessie; we still try each id. Returns count attempted."""
        rows = created_ids()
        n = 0
        paths = {"purchase": "/purchases/{id}", "withdrawal": "/withdrawals/{id}", "deposit": "/deposits/{id}",
                 "bill": "/bills/{id}", "loan": "/loans/{id}", "account": "/accounts/{id}",
                 "customer": "/customers/{id}", "merchant": "/merchants/{id}"}
        order = ["purchase", "withdrawal", "deposit", "bill", "loan", "account", "customer", "merchant"]
        for kind in order:
            for r in rows:
                if r["kind"] != kind or PREFIX not in json.dumps(r.get("body", {})):
                    continue  # only ever delete objects tagged with the project prefix
                try:
                    self.delete(paths[kind].format(id=r["id"]))
                    n += 1
                except NessieError as e:
                    log.warning("cleanup %s %s: %s", kind, r["id"], e)
        if n:
            CREATED_IDS.rename(CREATED_IDS.with_suffix(f".deleted_{int(time.time())}.jsonl"))
        return n


def _record_created(kind: str, oid: str, body: dict) -> None:
    RAW.mkdir(parents=True, exist_ok=True)
    with open(CREATED_IDS, "a") as f:
        f.write(json.dumps({"kind": kind, "id": oid, "body": body, "ts": time.time()}) + "\n")


def created_ids() -> list[dict]:
    if not CREATED_IDS.exists():
        return []
    with open(CREATED_IDS) as f:
        return [json.loads(line) for line in f if line.strip()]


# ---- geocoding --------------------------------------------------------------
GEOCODER = "https://geocoding.geo.census.gov/geocoder/locations/addressbatch"


def census_batch_geocode(df: pd.DataFrame, cache: Path) -> pd.DataFrame:
    """df columns: id, street, city, state, zip. Returns id, lat, lon, match (bool). Cached as CSV."""
    if cache.exists():
        return pd.read_csv(cache, dtype={"id": "string"})
    buf = io.StringIO()
    w = csv.writer(buf)
    for r in df.itertuples(index=False):
        w.writerow([r.id, r.street, r.city, r.state, r.zip])
    files = {"addressFile": ("addrs.csv", buf.getvalue(), "text/csv")}
    r = requests.post(GEOCODER, files=files, data={"benchmark": "Public_AR_Current"}, timeout=600)
    r.raise_for_status()
    cols = ["id", "input", "match", "match_type", "matched_address", "lonlat", "tiger_id", "side"]
    out = pd.read_csv(io.StringIO(r.text), header=None, names=cols, dtype=str)
    ll = out["lonlat"].str.split(",", expand=True)
    out["lon"] = pd.to_numeric(ll[0], errors="coerce") if ll.shape[1] > 0 else float("nan")
    out["lat"] = pd.to_numeric(ll[1], errors="coerce") if ll.shape[1] > 1 else float("nan")
    out["match"] = out["match"].eq("Match")
    res = out[["id", "lat", "lon", "match", "matched_address"]]
    cache.parent.mkdir(parents=True, exist_ok=True)
    res.to_csv(cache, index=False)
    return res


def nominatim_geocode(df: pd.DataFrame, cache: Path) -> pd.DataFrame:
    """Fallback for addresses the Census geocoder cannot match (suite numbers, PO boxes, mall names).
    1 request/s per Nominatim usage policy; results cached. Unmatched rows get NaN."""
    if cache.exists():
        return pd.read_csv(cache, dtype={"id": "string"})
    rows = []
    hdr = {"User-Agent": f"floodalpha-hackathon/0.1 ({os.environ.get('FLOODALPHA_CONTACT', 'contact-not-set')})"}
    for r in df.itertuples(index=False):
        q = f"{r.street}, {r.city}, {r.state} {r.zip}, USA"
        lat = lon = float("nan")
        try:
            resp = requests.get("https://nominatim.openstreetmap.org/search",
                                params={"q": q, "format": "json", "limit": 1, "countrycodes": "us"},
                                headers=hdr, timeout=30)
            js = resp.json() if resp.ok else []
            if not js:  # retry with city/state/zip only (mall / PO box addresses)
                time.sleep(1.1)
                resp = requests.get("https://nominatim.openstreetmap.org/search",
                                    params={"postalcode": r.zip, "country": "USA", "format": "json", "limit": 1},
                                    headers=hdr, timeout=30)
                js = resp.json() if resp.ok else []
                precision = "zip_centroid"
            else:
                precision = "address"
            if js:
                lat, lon = float(js[0]["lat"]), float(js[0]["lon"])
        except Exception as e:  # noqa: BLE001
            log.warning("nominatim failed for %s: %s", q, e)
            precision = "none"
        rows.append({"id": r.id, "lat": lat, "lon": lon, "precision": precision, "query": q})
        time.sleep(1.1)
    out = pd.DataFrame(rows)
    cache.parent.mkdir(parents=True, exist_ok=True)
    out.to_csv(cache, index=False)
    return out


def build_locations(storm_key: str = "isaias", force: bool = False) -> pd.DataFrame:
    """data/processed/<storm>/nessie_locations.parquet (NESSIE_LOCATIONS schema). Works without a key."""
    out = config.storm_dir(storm_key) / "nessie_locations.parquet"
    if out.exists() and not force:
        return pd.read_parquet(out)
    RAW.mkdir(parents=True, exist_ok=True)
    api = Nessie()
    branches, atms = api.branches(), api.atms()
    (RAW / "branches.json").write_text(json.dumps(branches))
    (RAW / "atms.json").write_text(json.dumps(atms))

    b = pd.DataFrame([{
        "nessie_id": x["_id"], "kind": "branch", "name": x["name"],
        "street": f"{x['address'].get('street_number','')} {x['address'].get('street_name','')}".strip(),
        "city": x["address"].get("city", ""), "state": str(x["address"].get("state", "")).upper(),
        "zip": x["address"].get("zip", ""),
        "lat": (x.get("geocode") or {}).get("lat"), "lon": (x.get("geocode") or {}).get("lng"),
    } for x in branches])
    b["geocode_source"] = "nessie"
    need = b["lat"].isna()
    if need.any():
        g = census_batch_geocode(b.loc[need, ["nessie_id", "street", "city", "state", "zip"]].rename(columns={"nessie_id": "id"}),
                                 RAW / "branch_geocodes_census.csv")
        g = g.set_index("id")
        b.loc[need, "lat"] = b.loc[need, "nessie_id"].map(g["lat"]).values
        b.loc[need, "lon"] = b.loc[need, "nessie_id"].map(g["lon"]).values
        b.loc[need, "geocode_source"] = "census_geocoder"
    need = b["lat"].isna()
    if need.any():
        n = nominatim_geocode(b.loc[need, ["nessie_id", "street", "city", "state", "zip"]].rename(columns={"nessie_id": "id"}),
                              RAW / "branch_geocodes_nominatim.csv").set_index("id")
        hit = b.loc[need, "nessie_id"].map(n["lat"]).notna().values
        idx = b.index[need][hit]
        b.loc[idx, "lat"] = b.loc[idx, "nessie_id"].map(n["lat"]).values
        b.loc[idx, "lon"] = b.loc[idx, "nessie_id"].map(n["lon"]).values
        b.loc[idx, "geocode_source"] = "nominatim"
    a = pd.DataFrame([{
        "nessie_id": x["_id"], "kind": "atm", "name": x["name"],
        "lat": x["geocode"]["lat"], "lon": x["geocode"]["lng"], "state": "",
        "geocode_source": "nessie",
    } for x in atms])
    df = pd.concat([b, a], ignore_index=True)
    df["state"] = df["state"].astype("string")
    for c in ("nessie_id", "kind", "name", "geocode_source"):
        df[c] = df[c].astype("string")
    unmatched = df["lat"].isna().sum()
    if unmatched:
        log.warning("%d locations could not be geocoded (kept with NaN lat/lon)", unmatched)
    cols = list(schemas.NESSIE_LOCATIONS) + ["street", "city", "zip"]
    df = df.reindex(columns=cols)
    schemas.check(df, schemas.NESSIE_LOCATIONS, "nessie_locations")
    df.to_parquet(out, index=False)
    log.info("wrote %s: %d branches, %d atms, %d ungeocoded", out, len(b), len(a), unmatched)
    return df


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO)
    d = build_locations(force="--force" in sys.argv)
    print(d.groupby(["kind", "geocode_source"]).size())
    print(d[d.lat.isna()][["name", "street", "city", "state", "zip"]])
    print(d.head())
