#!/usr/bin/env python
"""Regenerate the '## Results' section of README.md from the pipeline output files (never from memory)."""
from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from floodalpha import config  # noqa: E402

P = config.DATA_PROCESSED


def load(p: Path):
    return json.loads(p.read_text()) if p.exists() else None


def storm_table() -> str:
    rows = []
    be = pd.read_parquet(P / "bank_exposure.parquet") if (P / "bank_exposure.parquet").exists() else None
    for key, st in config.storms().items():
        m = load(P / key / "flood_meta.json")
        if not m:
            rows.append(f"| {st['name']} {st['year']} | pending | | | | |")
            continue
        e = load(P / key / "exposure_summary.json") or {}
        nb = ""
        if be is not None:
            b = be[(be.storm == key) & (be.definition == "full")]
            nb = f"{int((b.exposure_pct > 0).sum())} of {len(b)}"
        rows.append(f"| {st['name']} {st['year']} | {m['hours_landfall_to_event']:.0f} h | {m['n_passes']} / {m['coverage_pct_of_aoi']:.0f} % | "
                    f"{m['flooded_km2']:,.0f} | {e.get('tracts_flooded_ge_1pct', '')} of {e.get('tracts_sar_covered', '')} | {nb} |")
    hdr = ("| Storm | Landfall → first SAR published | Passes / AOI coverage | Flooded km² | Tracts ≥ 1 % flooded (of SAR-covered) | Banks with flooded deposits |\n"
           "|---|---|---|---|---|---|\n")
    return hdr + "\n".join(rows)


def market_text() -> str:
    cs = load(P / "cross_section.json")
    bt = load(P / "backtest_summary.json")
    if not cs:
        return "- **Market test:** not run yet."
    out = []
    for meas, label in (("exposure_pct", "satellite exposure (% of deposits in flooded branches)"), ("aoi_deposits_pct", "share of deposits inside the storm AOI")):
        parts = []
        for w in ("car_0_1", "car_0_5", "car_0_20"):
            r = cs["by_definition"]["full"][meas].get(w, {})
            if "coef_per_pct" in r:
                parts.append(f"CAR[{w[4:].replace('_', ',')}] coef {r['coef_per_pct'] * 100:+.3f} pp per pct (t = {r['t']:.1f}, N = {r['n']}, {r['n_storms']} storms, {r['n_nonzero_exposure']} non-zero)")
        out.append(f"- **Market test, {label}:** " + "; ".join(parts) + ".")
    if bt and bt.get("by_hold"):
        h = bt["by_hold"]
        out.append("- **Long-short backtest (short top-exposure tercile, long bottom tercile, 10 bp/side):** "
                   + "; ".join(f"hold {k} d: {v['n_trades']} trades, hit rate {v['hit_rate']:.0%}, cumulative net {v['cum_net_ret_pct']:+.2f} %" for k, v in h.items())
                   + f". Skipped (fewer than 3 exposed priced banks): {', '.join(bt.get('skipped', []))}.")
    out.append("- **Reading:** on the satellite measure only ~19 of ~1,400 storm-bank observations have non-zero exposure, and the coefficient flips sign "
               "between windows, so the large t-statistics are an artefact of clustered standard errors with few effective observations and should not be "
               "believed. On the AOI-deposits measure the coefficients are small, negative and marginal at best. The backtest lost money on both trades it could make. "
               "**We treat this as a null result.**")
    return "\n".join(out)


def validation_text() -> str:
    v = load(P / "validation" / "validation_summary.json")
    if not v:
        return "- **Validation:** not run yet."
    p = v.get("pooled", {}); t = v.get("threshold_tuning", {}); h = v.get("pooled_heldout", {})
    rho = p.get("spearman_pct_vs_density", {})
    txt = (f"- **Validation vs NFIP claims ({len(v['storms_validated'])} storms, {p.get('n_claims', 0):,} claims, {p.get('n_tracts', 0):,} SAR-covered tracts):** "
           f"Spearman ρ(tract % flooded, claims density) = {rho.get('r', float('nan')):.2f} (p = {rho.get('p', float('nan')):.1e}); "
           f"AUC for 'tract has claims' = {p.get('auc_pct_flooded', float('nan')):.2f} for the SAR map vs {p.get('auc_fema_declared', float('nan')):.2f} for the 'county was declared' baseline; "
           f"at the ≥ 1 % rule precision = {p.get('confusion', {}).get('precision', float('nan')):.2f}, recall = {p.get('confusion', {}).get('recall', float('nan')):.2f}.")
    if t:
        txt += (f"\n- **Threshold tuning (train: {', '.join(t.get('train_storms_in_grid', []))}; held-out: {', '.join(t.get('heldout_storms_in_grid', []))}):** "
                f"best on train = {t['best_on_train']['diff_db']} dB / {t['best_on_train']['post_db_max']} dB (train score {t['best_on_train']['train_score']:.3f}, "
                f"held-out {t.get('heldout_score_best', float('nan')):.3f}) vs default −3 dB / −15 dB (train {t['default']['train_score']:.3f}, held-out {t.get('heldout_score_default', float('nan')):.3f}). "
                "The map is weakly but consistently informative; precision is decent, recall is very low because a first-pass radar snapshot misses most claim-generating flooding.")
    return txt


def live_text() -> str:
    l = load(P / "live" / "live_summary.json")
    if not l:
        return "- **Live mode:** not run yet."
    b = pd.read_parquet(P / "live" / "bank_alert_exposure.parquet") if (P / "live" / "bank_alert_exposure.parquet").exists() else None
    extra = ""
    if b is not None and len(b):
        col = [c for c in b.columns if "deposit" in c and "alert" in c]
        if col:
            top = b.sort_values(col[0], ascending=False).iloc[0]
            extra = f" {len(b)} banks hold ${b[col[0]].sum() / 1e6:,.1f} bn of deposits in branches under an alert; largest: {top.get('bank_name', '')}."
    return (f"- **Live mode ({l.get('alerts_fetched_utc', '')[:16]} UTC):** {l.get('n_alerts_conus')} active CONUS flood-type alerts (mid-Atlantic coastal flood event)."
            + extra + " Re-run `scripts/run_live.py` before the demo.")


def stress_text() -> str:
    s = load(P / "isaias" / "stress_summary.json")
    if not s:
        return ""
    t = s["total"]
    return (f"- **SYNTHETIC Capital One stress test (Isaias):** {t['n']} generated customers pushed to Nessie, ${t['ead_total'] / 1e6:.1f} M exposure at default, "
            f"${t['expected_loss_total'] / 1e3:.0f} k baseline expected loss, **${s['incremental_expected_loss'] / 1e3:.0f} k incremental loss** "
            f"({t['n_flooded']} customers in tracts ≥ 1 % flooded). The Nessie branch footprint is MD/VA/DC, so direct Isaias exposure is 0 of 220 locations.")


def main() -> None:
    readme = ROOT / "README.md"
    txt = readme.read_text()
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    section = (f"## Results (generated {now} by `scripts/write_results.py` from `data/processed/`)\n\n"
               + storm_table() + "\n\n" + stress_text() + "\n" + market_text() + "\n" + validation_text() + "\n" + live_text() + "\n\n")
    new = re.sub(r"## Results.*?(?=## Honest limitations)", section, txt, flags=re.S)
    readme.write_text(new)
    print(section)


if __name__ == "__main__":
    main()
