import pandas as pd
import pytest

from floodalpha.flood.catalog import _scene_key, select_pair


def test_scene_key_matches_mpc_and_cdse_names():
    mpc = "S1D_IW_GRDH_1SDV_20261010T234452_20261010T234522_004960_0095B8_rtc"
    cdse = "S1D_IW_GRDH_1SDV_20261010T234452_20261010T234522_004960_0095B8_1A2B.SAFE"
    assert _scene_key(mpc) == _scene_key(cdse)


def _cat():
    rows = []
    for d, orb, post, cov in [("2026-09-16", 19, False, 30), ("2026-09-28", 19, False, 30), ("2026-10-10", 19, True, 30),
                              ("2026-10-03", 92, False, 20), ("2026-10-15", 92, True, 20), ("2026-10-15", 92, True, 5)]:
        rows.append({"scene_id": f"{d}_{orb}_{cov}", "acquired_utc": pd.Timestamp(d + "T23:45Z"), "published_utc": pd.Timestamp(d + "T23:45Z") + pd.Timedelta(hours=6),
                     "platform": "s1d", "relative_orbit": orb, "pass_direction": "ascending", "aoi_coverage_pct": float(cov), "is_post_landfall": post})
    return pd.DataFrame(rows)


def test_select_pair_prefers_best_covered_orbit_and_last_n_pre_passes():
    sel = select_pair(_cat(), pd.Timestamp("2026-10-10T01:30Z"), n_pre=2)
    assert sel["relative_orbit"] == 19
    assert sel["post_scene_ids"] == ["2026-10-10_19_30"]
    assert list(sel["pre_passes"]) == ["2026-09-16", "2026-09-28"]
    assert sel["event_time_utc"].startswith("2026-10-11T05:45")


def test_select_pair_respects_orbit_override_and_uses_first_post_pass_only():
    sel = select_pair(_cat(), pd.Timestamp("2026-10-10T01:30Z"), n_pre=1, relative_orbits=[92])
    assert sel["relative_orbit"] == 92 and len(sel["post_scene_ids"]) == 2
    assert list(sel["pre_passes"]) == ["2026-10-03"]


def test_select_pair_fails_without_post_scenes():
    c = _cat(); c["is_post_landfall"] = False
    with pytest.raises(RuntimeError):
        select_pair(c, pd.Timestamp("2026-10-10T01:30Z"), n_pre=1)
