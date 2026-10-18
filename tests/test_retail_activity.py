import numpy as np
import pandas as pd
import pytest

from quantcomm.retail import activity, osm, parking_sar


def _readings(n_days=120, stores=("a", "b", "c"), holiday_boost=0.0, seed=0):
    """12-day revisit readings, 00:30 UTC (previous local evening)."""
    rng = np.random.default_rng(seed)
    t = pd.date_range("2022-01-01 00:30", periods=n_days, freq="12D", tz="UTC")
    rows = []
    for s in stores:
        for d in t:
            md = (d - pd.Timedelta(hours=6)).strftime("%m-%d")
            boost = holiday_boost if "11-15" <= md <= "12-31" else 0.0
            lot_db = -8 + boost + rng.normal(0, 0.5)
            rows.append({"scene_id": f"S1_{d:%Y%m%d}", "datetime_utc": d, "relative_orbit": 34, "pass_direction": "ascending",
                         "store_id": s, "metro": "m1", "lot_lin": 10 ** (lot_db / 10), "bldg_lin": 10 ** (-5 / 10), "lot_px": 300})
    return pd.DataFrame(rows)


def test_activity_index_local_date_and_store_count():
    idx = activity.activity_index(_readings(n_days=20))
    assert idx["date"].iloc[0] == pd.Timestamp("2021-12-31")  # 00:30 UTC Jan 1 = local evening Dec 31
    assert (idx["n_stores"] == 3).all()
    assert idx["activity"].abs().max() < 4


def test_activity_index_drops_stores_with_short_history():
    df = _readings(n_days=20)
    short = df[df["store_id"] == "a"].head(activity.MIN_STORE_READINGS - 1).assign(store_id="short")
    idx = activity.activity_index(pd.concat([df, short]))
    assert (idx["n_stores"] == 3).all()


def test_causal_index_never_uses_later_readings():
    df = _readings(n_days=40)
    full = activity.activity_index(df, causal=True)
    cut = df["datetime_utc"].iloc[25]
    early = activity.activity_index(df[df["datetime_utc"] <= cut], causal=True)
    merged = early.merge(full, on="date", suffixes=("_early", "_full"))
    assert len(merged) == len(early) > 0
    assert np.allclose(merged["activity_early"], merged["activity_full"])
    # first MIN_STORE_READINGS evenings have no past to normalise against
    assert full["date"].min() > activity.activity_index(df)["date"].min()


def test_validation_detects_injected_holiday_lift():
    idx = activity.activity_index(_readings(holiday_boost=1.5))
    v = activity.validate(idx, ["11-15", "12-31"])
    assert v["holiday"]["diff"] > 0.5 and v["holiday"]["p"] < 0.01
    flat = activity.validate(activity.activity_index(_readings(seed=3)), ["11-15", "12-31"])
    assert flat["holiday"]["p"] > 0.01


def test_yoy_signal_needs_a_full_prior_year():
    idx = activity.activity_index(_readings(n_days=60))
    sig = activity.yoy_signal(idx, 91, 4)
    assert not sig.empty
    assert sig["date"].min() >= idx["date"].min() + pd.Timedelta(days=365)
    assert (sig["n_cur"] >= 4).all() and (sig["n_prev"] >= 4).all()


def test_forward_excess_has_no_lookahead_and_nan_past_end():
    idx = pd.bdate_range("2024-01-01", periods=100)
    px = pd.DataFrame({"X": np.exp(np.linspace(0, 1, 100)), "SPY": np.ones(100)}, index=idx)
    d = pd.Series([idx[10], idx[95]])
    fwd = activity.forward_excess(px, "X", "SPY", d, lag_days=3, horizon=5)
    i = idx.searchsorted(idx[10] + pd.Timedelta(days=3))
    assert i > 10  # entry strictly after the acquisition date
    assert fwd.iloc[0] == pytest.approx(np.log(px["X"].iloc[i + 5] / px["X"].iloc[i]))
    assert np.isnan(fwd.iloc[1])


def test_backtest_adjusts_for_overlap_and_verdict_requires_validation():
    idx = pd.bdate_range("2023-01-01", periods=600)
    rng = np.random.default_rng(0)
    px = pd.DataFrame({"X": np.exp(np.cumsum(rng.normal(0, .01, 600))), "SPY": np.ones(600)}, index=idx)
    sig = pd.DataFrame({"date": idx[::8][:60], "signal": rng.normal(size=60)})
    bt = activity.backtest(sig, px, "X", "SPY", 3, 63)
    assert bt["n_independent"] < bt["n"]
    unval = {"holiday": {"diff": -0.1, "p": 0.5}}
    assert activity.verdict(0.3, bt, unval)["stance"] == "unvalidated"
    ok = {"holiday": {"diff": 0.4, "p": 0.01}}
    assert activity.verdict(0.3, {**bt, "p_overlap_adj": 0.5}, ok)["stance"] == "no edge"
    assert activity.verdict(0.3, {**bt, "p_overlap_adj": 0.01, "spearman": 0.4}, ok)["stance"] == "lean long"
    assert activity.verdict(None, bt, ok)["stance"] == "no reading"


def test_dedupe_keeps_one_reading_per_pass_even_across_midnight():
    base = pd.Timestamp("2024-05-01 23:59:50", tz="UTC")
    df = pd.DataFrame({"store_id": ["a", "a", "a"], "relative_orbit": [34, 34, 34],
                       "datetime_utc": [base, base + pd.Timedelta(seconds=25), base + pd.Timedelta(days=12)],
                       "lot_px": [100, 250, 200], "lot_lin": [1.0, 2.0, 3.0]})
    out = parking_sar.dedupe_overlapping_frames(df)
    assert len(out) == 2 and out["lot_px"].tolist() == [250, 200]


def _way(i, tags, ring):
    return {"type": "way", "id": i, "tags": tags, "geometry": [{"lon": x, "lat": y} for x, y in ring]}


def test_stores_from_overpass_joins_adjacent_lots_and_drops_small_ones():
    sq = lambda x0, y0, s: [(x0, y0), (x0 + s, y0), (x0 + s, y0 + s), (x0, y0 + s), (x0, y0)]
    brand = {"brand:wikidata": "Q1", "shop": "supermarket", "name": "Shop"}
    d = {"elements": [
        _way(1, brand, sq(-97.0, 32.0, 0.001)),                       # store with a big adjacent lot
        _way(2, {"amenity": "parking"}, sq(-97.0, 31.9985, 0.0015)),  # ~140 x 160 m, touches store 1
        _way(3, brand, sq(-96.0, 32.0, 0.001)),                       # store with only a tiny lot
        _way(4, {"amenity": "parking"}, sq(-96.0, 31.9997, 0.0003)),
        _way(5, {"amenity": "parking"}, sq(-95.0, 32.0, 0.002)),      # lot with no store
    ]}
    g = osm.stores_from_overpass(d, "X", "m", "Q1", radius_deg=0.001, min_lot_area_m2=8000)
    assert g["store_id"].tolist() == ["osm_way_1"]
    assert g.crs.to_epsg() == 4326 and g["lot_area_m2"].iloc[0] > 8000
