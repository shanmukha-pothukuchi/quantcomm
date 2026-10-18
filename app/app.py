"""FloodAlpha dashboard.

Run:  .venv/bin/streamlit run app/app.py

Six tabs: storm case study (default Isaias 2026), live alerts, bank exposure, market test,
validation against NFIP claims, methodology. Reads only pipeline outputs under
data/processed/; anything missing is shown as "not generated yet". Every number
carries a caption naming the file it came from. Sections derived from the generated
Nessie portfolio carry a SYNTHETIC banner.
"""
from __future__ import annotations

import json
from pathlib import Path

import altair as alt
import pandas as pd
import streamlit as st

from components import (ALERT_COLORS, ALERT_OTHER, C_AFFECTED, C_CONTROL, C_COVER, C_EXPOSED, C_FLOOD, C_OK, C_WARN,
                        DATA, INK, INK_2, PITCH, ROOT, S1_BLUE, S2_ORANGE, S3_AQUA, S4_YELLOW, altair_theme,
                        completed_storms, fmt_dt, fmt_num, hours_between, load_geoparquet, load_json,
                        load_parquet, load_text, missing, model_config, png_data_url, rel, source,
                        storms_config, synthetic_banner)

st.set_page_config(page_title="FloodAlpha", page_icon="🌊", layout="wide")
altair_theme()

STORMS_CFG = storms_config()["storms"]
MODEL = model_config()
BUFFER_M = MODEL.get("exposure", {}).get("buffer_m", 500)
DONE = completed_storms()

st.title("FloodAlpha")
st.caption(PITCH)

(tab_case, tab_live, tab_banks, tab_market, tab_valid, tab_method) = st.tabs(
    ["Storm case study", "Live", "Bank exposure", "Market test", "Validation", "Methodology"])


# =========================================================== case study ====
def _passes_table(meta: dict) -> pd.DataFrame | None:
    passes = meta.get("passes") or []
    if not passes:
        return None
    rows = []
    for p in passes:
        rows.append({"pass": p.get("pass_id"), "orbit": p.get("relative_orbit"), "direction": p.get("pass_direction"),
                     "acquired (UTC)": fmt_dt(p.get("post_acquired_utc"), False),
                     "published (UTC)": fmt_dt(p.get("published_utc"), False),
                     "hours after landfall": fmt_num(hours_between(meta.get("landfall_time_utc"), p.get("post_acquired_utc")), "{:.0f}"),
                     "frames": len(p.get("post_scene_ids") or []),
                     "pre passes": ", ".join((p.get("pre_passes") or {}).keys()),
                     "AOI coverage %": fmt_num(p.get("coverage_pct_of_aoi"), "{:.0f}"),
                     "flooded km²": fmt_num(p.get("flooded_km2"), "{:.2f}"),
                     "polygons": p.get("n_polygons")})
    return pd.DataFrame(rows)


def _sensitivity_table(meta: dict) -> pd.DataFrame | None:
    fs = meta.get("frame_stats") or []
    tot: dict[str, float] = {}
    for f in fs:
        for k, v in (f.get("threshold_sensitivity_km2_before_cluster_filter") or {}).items():
            tot[k] = tot.get(k, 0.0) + float(v)
    if not tot:
        return None
    post_max = meta.get("thresholds", {}).get("post_db_max", -15.0)
    rows = []
    for t in (-1.0, -1.5, -2.0, -3.0, -4.0):
        a = tot.get(f"km2_diff_lt_{t}")
        b = tot.get(f"km2_diff_lt_{t}_and_post_lt_{post_max}")
        if a is None and b is None:
            continue
        rows.append({"change threshold (dB)": t, "km² darker than threshold": round(a or 0, 1),
                     f"…and post < {post_max} dB": round(b or 0, 1),
                     "used": "✓" if t == meta.get("thresholds", {}).get("diff_db_threshold") else ""})
    return pd.DataFrame(rows)


with tab_case:
    if not DONE:
        missing("Flood maps", DATA / "isaias" / "flood_meta.json", "scripts/run_storm.py --storm isaias")
        storm = "isaias"
    else:
        default = DONE.index("isaias") if "isaias" in DONE else 0
        storm = st.selectbox("Storm", DONE, index=default, format_func=lambda k: f"{STORMS_CFG[k]['name']} ({STORMS_CFG[k]['year']})")
    D = DATA / storm
    cfg = STORMS_CFG.get(storm, {})
    meta = load_json(D / "flood_meta.json")
    st.subheader(f"{cfg.get('name', storm)} {cfg.get('year', '')} — Sentinel-1 flood map")
    if cfg.get("notes"):
        st.caption(cfg["notes"])

    landfall = cfg.get("landfall_time_utc")
    if meta:
        event = meta.get("event_time_utc")
        lag_h = meta.get("hours_landfall_to_event") or hours_between(landfall, event)
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Landfall (NHC)", fmt_dt(landfall))
        c2.metric("Event time = first post-storm SAR published", fmt_dt(event))
        c3.metric("Landfall → SAR publication", f"{lag_h:.0f} h" if lag_h is not None else "—")
        c4.metric("Flooded area (km²)", fmt_num(meta.get("flooded_km2"), "{:,.1f}"))
        c5, c6, c7, c8 = st.columns(4)
        c5.metric("Post-storm SAR coverage of AOI", fmt_num(meta.get("coverage_pct_of_aoi"), "{:.0f} %"))
        c6.metric("Passes used (orbits)", f"{meta.get('n_passes', 1)} ({', '.join(str(o) for o in meta.get('relative_orbits_used', [meta.get('relative_orbit')]))})")
        c7.metric("Mosaic complete", fmt_dt(meta.get("mosaic_complete_utc") or event))
        c8.metric("Resolution", f"{meta.get('thresholds', {}).get('resolution_m', '—')} m")
        source(D / "flood_meta.json", "event time is the Copernicus publication time of the first post-landfall pass; "
                                      "nothing published later feeds the strict signal")
    else:
        missing("Flood map metadata", D / "flood_meta.json", f"scripts/run_storm.py --storm {storm}")

    import folium
    from streamlit_folium import st_folium

    bbox = (meta or {}).get("aoi_bbox") or cfg.get("aoi_bbox") or [-88.6, 29.6, -83.5, 32.2]
    center = [(bbox[1] + bbox[3]) / 2, (bbox[0] + bbox[2]) / 2]
    nws = load_json(D / "nws_sanity.json")
    exp = load_parquet(D / "nessie_exposure.parquet")

    left, right = st.columns([1, 4])
    with left:
        layer = st.radio("Radar backdrop", ["None", "Pre-storm", "Post-storm", "Change (flood)"], index=3, key=f"layer_{storm}")
        show_polys = st.checkbox("Flood polygons", value=True, key=f"polys_{storm}")
        show_cover = st.checkbox("SAR coverage per pass", value=True, key=f"cover_{storm}")
        show_nws = st.checkbox("NWS flood warnings (landfall −1 d … +3 d)", value=True, key=f"nws_{storm}")
        show_cap1 = st.checkbox("Capital One branches / ATMs", value=True, key=f"cap1_{storm}")
        show_track = st.checkbox("NHC best track", value=True, key=f"track_{storm}")

    m = folium.Map(location=center, zoom_start=7, tiles="OpenStreetMap", control_scale=True)
    folium.Rectangle([[bbox[1], bbox[0]], [bbox[3], bbox[2]]], color=C_COVER, weight=1, fill=False,
                     dash_array="4", tooltip="Area of interest").add_to(m)
    img_file = {"Pre-storm": "preview_pre.png", "Post-storm": "preview_post.png", "Change (flood)": "preview_change.png"}.get(layer)
    if img_file and meta and meta.get("preview_bounds"):
        url = png_data_url(D / img_file)
        if url:
            folium.raster_layers.ImageOverlay(url, bounds=meta["preview_bounds"], opacity=0.85, name=layer,
                                              interactive=False).add_to(m)
        else:
            st.info(f"{img_file} not generated yet.")
    if show_cover:
        covp = load_json(D / "coverage_passes.geojson") or load_json(D / "coverage.geojson")
        if covp:
            props = (covp.get("features") or [{}])[0].get("properties") or {}
            fields = [k for k in ("pass_id", "published_utc") if k in props]
            folium.GeoJson(covp, name="SAR coverage",
                           style_function=lambda f: {"color": C_COVER, "weight": 2, "fill": False, "dashArray": "6 3"},
                           tooltip=folium.GeoJsonTooltip(fields=fields) if fields else "Post-storm Sentinel-1 coverage").add_to(m)
        else:
            missing("SAR coverage outline", D / "coverage.geojson")
    if show_polys:
        polys = load_geoparquet(D / "flood_polygons.parquet")
        if polys is not None and len(polys):
            keep = [c for c in ("pass_id", "area_m2") if c in polys.columns]
            gj = json.loads(polys[keep + ["geometry"]].to_json())
            folium.GeoJson(gj, name="Flood polygons",
                           style_function=lambda f: {"color": C_FLOOD, "weight": 0.5, "fillColor": C_FLOOD, "fillOpacity": 0.6},
                           tooltip=folium.GeoJsonTooltip(fields=keep) if keep else None).add_to(m)
        elif polys is None:
            missing("Flood polygons", D / "flood_polygons.parquet")
    if show_nws:
        if nws and nws.get("warnings_geojson") and (nws["warnings_geojson"].get("features") or []):
            feats = nws["warnings_geojson"]["features"]
            tip = [k for k in ("event", "area", "areaDesc") if k in (feats[0].get("properties") or {})]
            folium.GeoJson(nws["warnings_geojson"], name="NWS warnings",
                           style_function=lambda f: {"color": C_WARN, "weight": 1.5, "fill": False},
                           tooltip=folium.GeoJsonTooltip(fields=tip) if tip else None).add_to(m)
        elif nws is None:
            missing("NWS sanity check", D / "nws_sanity.json")
    if show_track:
        track = load_json(D / "nhc_track.geojson")
        if track:
            folium.GeoJson(track, name="NHC best track", style_function=lambda f: {"color": INK, "weight": 2}).add_to(m)
    if show_cap1 and exp is not None:
        for _, r in exp.iterrows():
            folium.CircleMarker([r.lat, r.lon], radius=4 if r.kind == "atm" else 5, color=C_EXPOSED if r.exposed else C_OK,
                                fill=True, fill_opacity=0.9,
                                tooltip=f"{r.kind}: {r['name']} — {'EXPOSED' if r.exposed else 'not exposed'}").add_to(m)
    folium.LayerControl().add_to(m)
    with right:
        st_folium(m, width=None, height=600, returned_objects=[], key=f"map_{storm}")
        st.caption(f"Flood: blue fill · SAR coverage: dashed grey outline per pass · NWS warnings: violet · "
                   f"NHC track: black · Capital One: red = within {BUFFER_M} m of flood, green = not exposed.")

    if meta:
        pt = _passes_table(meta)
        if pt is not None:
            st.markdown("**Sentinel-1 passes used** (each pass keeps its own publication time)")
            st.dataframe(pt, hide_index=True, width="stretch")
            source(D / "flood_meta.json")
        sens = _sensitivity_table(meta)
        if sens is not None:
            col_a, col_b = st.columns([2, 3])
            with col_a:
                st.markdown("**Threshold sensitivity** (km² before the cluster filter, summed over frames)")
                st.dataframe(sens, hide_index=True, width="stretch")
                st.caption("Logged for every run so the threshold can be tuned on training storms only "
                           "(Validation tab), never on the storm being shown.")
            with col_b:
                if (D / "diagnostic_diff.png").exists():
                    st.image(str(D / "diagnostic_diff.png"),
                             caption="post − pre VV difference (left) and pixels darker than −1.5 dB (right), "
                                     "with NWS warning polygons (red) and the NHC track (black)")
                    source(D / "diagnostic_diff.png")

    if nws:
        st.subheader("Sanity check against NWS warnings and FEMA declarations")
        a, b, c, d = st.columns(4)
        a.metric("NWS Flood / Flash Flood Warnings", nws.get("n_warnings", "—"))
        p1, p2 = nws.get("pct_flood_area_inside_warnings"), nws.get("pct_warning_area_flooded")
        b.metric("Flood area inside warning polygons", fmt_num(p1, "{:.0f} %"))
        c.metric("Warning area detected as flooded", fmt_num(p2, "{:.2f} %"))
        d.metric("Verdict", str(nws.get("verdict", "—")).split(":")[0])
        if nws.get("warnings_available") is False or nws.get("n_warnings", 0) == 0:
            st.caption("api.weather.gov keeps only recent alert history, so historical storms have no warning polygons here.")
        by_cty = nws.get("flooded_km2_by_county") or {}
        if by_cty:
            fema = set(nws.get("fema_declared_counties") or [])
            tbl = (pd.DataFrame({"county": list(by_cty), "flooded_km2": list(by_cty.values())})
                   .assign(fema_declared=lambda t: t.county.isin(fema)).sort_values("flooded_km2", ascending=False))
            chart = (alt.Chart(tbl.head(15)).mark_bar(size=14, color=C_FLOOD)
                     .encode(y=alt.Y("county:N", sort="-x", title=None), x=alt.X("flooded_km2:Q", title="flooded km²"),
                             tooltip=["county", alt.Tooltip("flooded_km2:Q", format=".2f"), "fema_declared"])
                     .properties(height=330, title="Flooded area by county (top 15)"))
            cc, ct = st.columns([3, 2])
            cc.altair_chart(chart, width="stretch")
            ct.dataframe(tbl, hide_index=True, width="stretch", height=330)
        source(D / "nws_sanity.json")

    st.subheader("Capital One branches and ATMs (Nessie)")
    if exp is None:
        missing("Capital One exposure", D / "nessie_exposure.parquet")
    else:
        n_exp, n_aoi = int(exp.exposed.sum()), int(exp.in_aoi.sum()) if "in_aoi" in exp else 0
        a, b, c = st.columns(3)
        a.metric("Locations pulled from Nessie", len(exp))
        b.metric("Inside this storm's AOI", n_aoi)
        c.metric(f"Exposed (≤ {BUFFER_M} m from flood)", n_exp)
        if n_exp == 0:
            st.success("Zero direct exposure. Capital One's Nessie branch and ATM footprint is entirely in Maryland, "
                       "Virginia and DC, so Gulf and Atlantic storms do not touch it. The Live tab shows the "
                       "mid-Atlantic coastal flood alerts that do; the synthetic portfolio below models customer-level exposure.")
        with st.expander("Location table"):
            cols = [c for c in ("kind", "name", "state", "lat", "lon", "in_aoi", "sar_covered", "dist_to_flood_m", "exposed", "geocode_source") if c in exp.columns]
            st.dataframe(exp[cols].sort_values(["exposed", "dist_to_flood_m"], ascending=[False, True]), hide_index=True, width="stretch", height=360)
        source(D / "nessie_exposure.parquet")

    port = load_parquet(D / "SYNTHETIC_portfolio.parquet")
    res = load_parquet(D / "SYNTHETIC_stress_results.parquet")
    summ = load_json(D / "stress_summary.json")
    if port is not None or res is not None:
        st.subheader("Synthetic Capital One portfolio stress test")
        synthetic_banner()
        if port is not None:
            pushed = int(port.nessie_customer_id.notna().sum()) if "nessie_customer_id" in port else 0
            st.caption(f"{len(port)} synthetic customers generated; {pushed} created in Nessie "
                       f"(tagged `{MODEL.get('portfolio', {}).get('nickname_prefix', 'FLOODALPHA_')}`).")
        if res is None:
            missing("Stress test results", D / "SYNTHETIC_stress_results.parquet")
        else:
            g = (res.groupby("is_flooded")
                 .agg(n=("customer_key", "size"), ead=("ead", "sum"), expected_loss=("expected_loss", "sum"),
                      pd_12m=("pd_12m", "mean"), withdrawal=("withdrawal_shock", "sum"), missed=("missed_bill", "mean"))
                 .rename(index={True: "Flooded tract", False: "Not flooded"}))
            g["loss_rate_pct"] = 100 * g.expected_loss / g.ead
            fl = g.loc["Flooded tract"] if "Flooded tract" in g.index else None
            nf = g.loc["Not flooded"] if "Not flooded" in g.index else None
            a, b, c, d = st.columns(4)
            a.metric("Customers in flooded tracts", int(fl.n) if fl is not None else 0)
            b.metric("Expected loss — flooded", f"${fl.expected_loss:,.0f}" if fl is not None else "—",
                     delta=f"{fl.loss_rate_pct:.2f} % of EAD" if fl is not None else None, delta_color="off")
            c.metric("Expected loss — not flooded", f"${nf.expected_loss:,.0f}" if nf is not None else "—",
                     delta=f"{nf.loss_rate_pct:.2f} % of EAD" if nf is not None else None, delta_color="off")
            d.metric("Incremental loss from flooding", f"${(summ or {}).get('incremental_expected_loss', float('nan')):,.0f}" if summ else "—")
            if fl is None:
                st.info("No synthetic customer sits in a tract with ≥ 1 % mapped flooding, so the stress uplift is not "
                        "triggered and incremental expected loss is $0. That is the model's honest output for this map.")
            long = (g.reset_index().rename(columns={"is_flooded": "group"})
                    .melt(id_vars="group", value_vars=["ead", "expected_loss"], var_name="measure", value_name="usd"))
            long["measure"] = long.measure.map({"ead": "Exposure at default", "expected_loss": "Expected loss"})
            chart = (alt.Chart(long).mark_bar(size=26)
                     .encode(x=alt.X("measure:N", title=None, axis=alt.Axis(labelAngle=0)),
                             y=alt.Y("usd:Q", title="USD", axis=alt.Axis(format="$,.0f")),
                             color=alt.Color("group:N", title=None, scale=alt.Scale(domain=["Flooded tract", "Not flooded"], range=[C_AFFECTED, C_CONTROL])),
                             xOffset="group:N", tooltip=["group:N", "measure:N", alt.Tooltip("usd:Q", format="$,.0f")])
                     .properties(height=280, title="Synthetic portfolio: exposure vs expected loss"))
            l1, r1 = st.columns([3, 2])
            l1.altair_chart(chart, width="stretch")
            show = g[["n", "ead", "expected_loss", "loss_rate_pct", "pd_12m", "withdrawal", "missed"]].copy()
            show.columns = ["n", "EAD $", "Expected loss $", "Loss rate %", "Mean PD (12m)", "Withdrawal shock $", "Missed-bill rate"]
            r1.dataframe(show.style.format({"EAD $": "{:,.0f}", "Expected loss $": "{:,.0f}", "Loss rate %": "{:.2f}",
                                            "Mean PD (12m)": "{:.3f}", "Withdrawal shock $": "{:,.0f}", "Missed-bill rate": "{:.2f}"}), width="stretch")
            if "group" in res:
                g2 = res.groupby(["group", "is_flooded"]).agg(n=("customer_key", "size"), expected_loss=("expected_loss", "sum")).reset_index()
                r1.dataframe(g2, hide_index=True, width="stretch")
            source(D / "SYNTHETIC_stress_results.parquet")
            if summ:
                with st.expander("stress_summary.json"):
                    st.json(summ)
        with st.expander("Model parameters (config/model.yaml)"):
            params = {**{f"stress.{k}": v for k, v in MODEL.get("stress", {}).items()},
                      **{f"portfolio.{k}": v for k, v in MODEL.get("portfolio", {}).items()}}
            st.dataframe(pd.DataFrame({"parameter": list(params), "value": [str(v) for v in params.values()]}), hide_index=True, width="stretch")

    with st.expander("Sentinel-1 acquisitions over the AOI (catalog)"):
        cat = load_parquet(D / "s1_catalog.parquet")
        if cat is None:
            missing("Satellite catalog", D / "s1_catalog.parquet")
        else:
            view = cat.drop(columns=[c for c in ("geometry_json", "vv_href") if c in cat.columns]).copy()
            for c in ("acquired_utc", "published_utc"):
                if c in view:
                    view[c] = pd.to_datetime(view[c], utc=True).dt.strftime("%Y-%m-%d %H:%M")
            if "aoi_coverage_pct" in view:
                view["aoi_coverage_pct"] = view.aoi_coverage_pct.round(1)
            st.dataframe(view.sort_values("acquired_utc", ascending=False), hide_index=True, width="stretch", height=400)
            source(D / "s1_catalog.parquet")


# ================================================================= live ====
with tab_live:
    L = DATA / "live"
    live = load_json(L / "live_summary.json")
    alerts = load_json(L / "alerts.geojson")
    nhc = load_json(L / "nhc.json")
    sat = load_json(L / "satellite_status.json")
    st.subheader("Active NWS flood alerts and NHC storms")
    if live is None and alerts is None:
        missing("Live mode outputs", L / "live_summary.json", "scripts/run_live.py")
    else:
        a, b, c, d = st.columns(4)
        a.metric("Alerts fetched", fmt_dt((live or {}).get("alerts_fetched_utc")))
        a.caption("re-fetched when older than 15 min")
        b.metric("Active flood-type alerts (CONUS)", (live or {}).get("n_alerts_conus", len((alerts or {}).get("features", []))))
        banks = (live or {}).get("banks") or {}
        c.metric("Banks with branches under an alert", banks.get("n_banks_with_branches_in_alert", "—"))
        dep = banks.get("deposits_in_alert_k_total")
        d.metric("Deposits under alert", f"${dep / 1e6:,.1f} bn" if dep else "—")
        source(L / "live_summary.json")

    lcol, rcol = st.columns([3, 2])
    with lcol:
        import folium
        from streamlit_folium import st_folium

        feats = (alerts or {}).get("features") or []
        if feats:
            import numpy as np
            from shapely.geometry import shape

            try:
                b = np.array([shape(f["geometry"]).bounds for f in feats if f.get("geometry")])
                center = [float((b[:, 1].min() + b[:, 3].max()) / 2), float((b[:, 0].min() + b[:, 2].max()) / 2)]
            except Exception:  # noqa: BLE001
                center = [36.5, -77.0]
            m2 = folium.Map(location=center, zoom_start=6, tiles="OpenStreetMap", control_scale=True)
            for ev in sorted({f["properties"].get("event") for f in feats}, key=lambda e: list(ALERT_COLORS).index(e) if e in ALERT_COLORS else 99):
                sub = {"type": "FeatureCollection", "features": [f for f in feats if f["properties"].get("event") == ev and f.get("geometry")]}
                if not sub["features"]:
                    continue
                col = ALERT_COLORS.get(ev, ALERT_OTHER)
                tip = [k for k in ("event", "areaDesc", "headline", "onset", "ends") if k in sub["features"][0]["properties"]]
                folium.GeoJson(sub, name=ev, style_function=lambda f, col=col: {"color": col, "weight": 1.2, "fillColor": col, "fillOpacity": 0.3},
                               tooltip=folium.GeoJsonTooltip(fields=tip)).add_to(m2)
            ne = load_parquet(L / "nessie_alert_exposure.parquet")
            if ne is not None:
                for _, r in ne.iterrows():
                    folium.CircleMarker([r.lat, r.lon], radius=3, color=C_EXPOSED if r.in_alert else C_OK, fill=True, fill_opacity=0.9,
                                        tooltip=f"Capital One {r.kind}: {r['name']} — {'UNDER ALERT' if r.in_alert else 'clear'}").add_to(m2)
            for key in ("cone", "track"):
                g = (nhc or {}).get(key)
                if g:
                    folium.GeoJson(g, name=f"NHC {key}", style_function=lambda f: {"color": INK, "weight": 2, "fillOpacity": 0.1}).add_to(m2)
            folium.LayerControl().add_to(m2)
            st_folium(m2, width=None, height=520, returned_objects=[], key="live_map")
            legend = " · ".join(f"{ev}: {('blue','aqua','yellow','orange','red','violet')[list(ALERT_COLORS).index(ev)]}" for ev in ALERT_COLORS if ev in {f['properties'].get('event') for f in feats})
            st.caption(f"{legend} · Capital One: red = under alert, green = clear")
        elif alerts is not None:
            st.info("No active flood-type alerts with geometry right now.")
        else:
            missing("Alert polygons", L / "alerts.geojson", "scripts/run_live.py")
    with rcol:
        if feats:
            ev = pd.DataFrame([f["properties"] for f in feats])
            keep = [c for c in ("event", "severity", "areaDesc", "onset", "ends") if c in ev.columns]
            for c in ("onset", "ends"):
                if c in ev:
                    ev[c] = ev[c].map(lambda s: fmt_dt(s, False))
            st.markdown("**Alerts**")
            st.dataframe(ev[keep].sort_values("event"), hide_index=True, width="stretch", height=260)
            source(L / "alerts.geojson")
        st.markdown("**NHC active storms**")
        if nhc is None:
            missing("NHC status", L / "nhc.json")
        else:
            act = nhc.get("active_storms") or []
            if act:
                st.dataframe(pd.DataFrame(act)[[c for c in ("name", "classification", "intensity", "basin", "lastUpdate") if c in pd.DataFrame(act).columns]], hide_index=True, width="stretch")
                if not nhc.get("atlantic"):
                    st.caption("No active Atlantic storm; the mid-Atlantic event is a non-tropical coastal flood (NWS alerts only).")
            else:
                st.caption(f"No active NHC storms at {fmt_dt(nhc.get('fetched_utc'))}.")
            source(L / "nhc.json")

    st.subheader("Pre-event exposure under the current alerts")
    be = load_parquet(L / "bank_alert_exposure.parquet")
    c1, c2 = st.columns([3, 2])
    with c1:
        st.markdown("**Banks** (FDIC Summary of Deposits 2026 branches inside alert polygons)")
        if be is None:
            missing("Bank alert exposure", L / "bank_alert_exposure.parquet", "scripts/run_live.py")
        else:
            v = be.sort_values("deposits_in_alert_k", ascending=False).copy()
            v["deposits_in_alert_$bn"] = v["deposits_in_alert_k"] / 1e6
            top = v.head(12)
            chart = (alt.Chart(top).mark_bar(size=14, color=S1_BLUE)
                     .encode(y=alt.Y("bank_name:N", sort="-x", title=None), x=alt.X("deposits_in_alert_$bn:Q", title="deposits under alert ($ bn)"),
                             tooltip=["bank_name", alt.Tooltip("deposits_in_alert_$bn:Q", format=",.2f"), alt.Tooltip("exposure_pct:Q", format=".1f", title="% of bank deposits"), "n_branches_in_alert"])
                     .properties(height=320, title="Deposits in branches under an active flood alert (top 12)"))
            st.altair_chart(chart, width="stretch")
            cols = [c for c in ("bank_name", "n_branches_in_alert", "deposits_in_alert_k", "exposure_pct", "alert_events", "total_deposits_k") if c in v.columns]
            st.dataframe(v[cols].style.format({"deposits_in_alert_k": "{:,.0f}", "exposure_pct": "{:.2f}", "total_deposits_k": "{:,.0f}"}), hide_index=True, width="stretch", height=300)
            source(L / "bank_alert_exposure.parquet", "exposure_pct = deposits under alert / bank's national deposits")
    with c2:
        st.markdown("**Capital One (Nessie) locations**")
        ne = load_parquet(L / "nessie_alert_exposure.parquet")
        if ne is None:
            missing("Capital One alert exposure", L / "nessie_alert_exposure.parquet")
        else:
            a, b = st.columns(2)
            a.metric("Locations", len(ne))
            b.metric("Under an alert", int(ne.in_alert.sum()))
            if ne.in_alert.any():
                st.dataframe(ne[ne.in_alert][[c for c in ("kind", "name", "city", "state", "alert_events") if c in ne.columns]], hide_index=True, width="stretch")
            else:
                st.caption("All Capital One branches and ATMs are inland (Arlington / DC / Maryland suburbs); the coastal "
                           "flood zones stop short of them. Honest result, not a bug.")
            source(L / "nessie_alert_exposure.parquet")
        pa = load_parquet(L / "SYNTHETIC_portfolio_alert_exposure.parquet")
        if pa is not None:
            st.markdown("**Synthetic portfolio**")
            synthetic_banner()
            a, b = st.columns(2)
            a.metric("Customers under an alert", int(pa.in_alert.sum()))
            b.metric("Loan balance under alert", f"${pa.loc[pa.in_alert, 'loan_amount'].sum():,.0f}")
            source(L / "SYNTHETIC_portfolio_alert_exposure.parquet")

    st.subheader("Satellite status")
    if sat is None:
        missing("Satellite status", L / "satellite_status.json", "scripts/run_live.py")
    else:
        st.caption(f"Checked {fmt_dt(sat.get('checked_utc'))}. Next expected pass = last pass + 12 days per relative orbit "
                   "(Sentinel-1 repeat cycle).")
        refresh = (live or {}).get("isaias_refresh") or {}
        if refresh.get("refresh_recommended"):
            st.warning(f"A Sentinel-1 pass newer than the processed Isaias map is available ({len(refresh.get('new_passes', []))} new). "
                       f"Re-run: `{refresh.get('command')}`", icon="🛰️")
        elif refresh:
            st.success(f"Isaias flood map is current through {fmt_dt(refresh.get('processed_through_utc'))}; no newer pass published yet.")
        rows = []
        for area, info in (sat.get("areas") or {}).items():
            for o in info.get("orbits") or []:
                rows.append({"area": area, "orbit": o.get("relative_orbit"), "direction": o.get("pass_direction"), "platform": o.get("platform"),
                             "last pass (UTC)": fmt_dt(o.get("last_pass_utc"), False), "next expected (UTC)": fmt_dt(o.get("next_expected_utc"), False),
                             "coverage %": fmt_num(o.get("coverage_pct"), "{:.0f}"), "scenes / 30 d": o.get("n_scenes_30d")})
        if rows:
            st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
        source(L / "satellite_status.json")
    with st.expander("Raw live JSON"):
        st.json({"live_summary": live, "nhc": nhc, "satellite_status": sat})


# ======================================================= bank exposure ====
with tab_banks:
    st.subheader("Exposure of every FDIC-insured bank, by storm")
    be_all = load_parquet(DATA / "bank_exposure.parquet")
    rep = load_json(DATA / "bank_exposure_report.json")
    if be_all is None:
        missing("Bank exposure table", DATA / "bank_exposure.parquet", "scripts/run_banks.py")
    else:
        st.caption("exposure_pct = deposits in branches within the flood buffer ÷ the bank's national deposits (FDIC SOD, vintage before the storm). "
                   "aoi_deposits_pct = deposits inside the storm's AOI box regardless of mapped flooding. "
                   "`strict` uses only polygons published by the event time (first pass); `full` uses every pass in the window.")
        f1, f2, f3, f4, f5 = st.columns(5)
        storms_avail = [s for s in STORMS_CFG if s in set(be_all.storm)]
        storm_sel = f1.selectbox("Storm", storms_avail, format_func=lambda k: STORMS_CFG[k]["name"]) if storms_avail else None
        defn = f2.selectbox("Definition", sorted(be_all.definition.unique()), index=sorted(be_all.definition.unique()).index("full") if "full" in set(be_all.definition) else 0)
        min_dep = f3.number_input("Min national deposits ($ bn)", 0.0, 2000.0, 1.0, 0.5)
        ticker_only = f4.checkbox("Public (ticker) banks only", value=False)
        measure = f5.selectbox("Rank by", ["exposure_pct", "aoi_deposits_pct", "flooded_deposits_k"])
        v = be_all[(be_all.storm == storm_sel) & (be_all.definition == defn) & (be_all.total_deposits_k >= min_dep * 1e6)].copy()
        if ticker_only:
            v = v[v.ticker.fillna("") != ""]
        v = v.sort_values(measure, ascending=False)
        a, b, c, d = st.columns(4)
        a.metric("Banks shown", len(v))
        b.metric("Banks with any flooded deposits", int((v.flooded_deposits_k > 0).sum()))
        c.metric("Flooded deposits (sum)", f"${v.flooded_deposits_k.sum() / 1e6:,.2f} bn")
        d.metric("Banks with branches in AOI", int((v.n_branches_in_aoi > 0).sum()))
        top = v.head(15)
        if len(top) and top[measure].max() > 0:
            chart = (alt.Chart(top).mark_bar(size=14, color=S1_BLUE)
                     .encode(y=alt.Y("bank_name:N", sort="-x", title=None), x=alt.X(f"{measure}:Q", title=measure),
                             tooltip=["bank_name", "ticker", alt.Tooltip("exposure_pct:Q", format=".2f"), alt.Tooltip("aoi_deposits_pct:Q", format=".2f"),
                                      alt.Tooltip("flooded_deposits_k:Q", format=",.0f"), "n_flooded_branches", "n_branches_in_aoi"])
                     .properties(height=360, title=f"Top banks by {measure} — {STORMS_CFG[storm_sel]['name']} ({defn})"))
            st.altair_chart(chart, width="stretch")
        else:
            st.info(f"No bank has {measure} > 0 for this storm and definition. With sparse SAR flood maps that is a real result, not a missing file.")
        cols = [c for c in ("bank_name", "ticker", "ticker_status", "sod_year", "total_deposits_k", "flooded_deposits_k", "exposure_pct", "aoi_deposits_pct",
                            "n_branches", "n_flooded_branches", "n_branches_in_aoi", "n_branches_sar_covered") if c in v.columns]
        st.dataframe(v[cols].style.format({"total_deposits_k": "{:,.0f}", "flooded_deposits_k": "{:,.0f}", "exposure_pct": "{:.3f}", "aoi_deposits_pct": "{:.2f}"}),
                     hide_index=True, width="stretch", height=420)
        source(DATA / "bank_exposure.parquet")
        with st.expander("Per-storm join report and ticker coverage"):
            if rep:
                st.dataframe(pd.DataFrame(rep.get("storms", [])), hide_index=True, width="stretch")
                if rep.get("pending"):
                    st.caption(f"Pending storms (flood map not finished yet): {', '.join(rep['pending'])}")
                source(DATA / "bank_exposure_report.json")
            tk = Path(ROOT / "config" / "bank_tickers.csv")
            if tk.exists():
                t = pd.read_csv(tk)
                st.caption(f"Ticker map: {int((t.status == 'active').sum())} active, {int((t.status == 'delisted').sum())} delisted holding companies. "
                           "Delisted banks (SunTrust, BB&T, IBERIABANK, CenterState, …) still get exposure numbers but have no price history, "
                           "so the market test is survivorship-biased toward banks that still trade.")
                st.dataframe(t[[c for c in ("company", "ticker", "status", "matched_name_2024", "notes") if c in t.columns]], hide_index=True, width="stretch", height=260)
                source(tk)


# ========================================================= market test ====
def _cs_table(cs: dict) -> pd.DataFrame:
    rows = []
    for defn, by_meas in (cs.get("by_definition") or {}).items():
        for meas, by_win in by_meas.items():
            for win, r in by_win.items():
                rows.append({"definition": defn, "exposure measure": meas, "window": win.replace("car_", "CAR [").replace("_", ", ") + "]" if win.startswith("car_") else win,
                             "coef per pct": r.get("coef_per_pct"), "t": r.get("t"), "p": r.get("p"), "N": r.get("n"), "storms": r.get("n_storms"),
                             "non-zero exposure": r.get("n_nonzero_exposure"), "SE": r.get("se_type"), "note": r.get("note", "")})
    return pd.DataFrame(rows)


with tab_market:
    st.subheader("Does the market price satellite-measured flood exposure?")
    cs = load_json(DATA / "cross_section.json")
    es = load_parquet(DATA / "event_study.parquet")
    bt = load_parquet(DATA / "backtest.parquet")
    bts = load_json(DATA / "backtest_summary.json")
    if cs and cs.get("caveats"):
        st.error("**Read this before the numbers.**\n\n" + "\n".join(f"- {c}" for c in cs["caveats"]), icon="⚠️")
    if es is None and cs is None:
        missing("Event study", DATA / "event_study.parquet", "scripts/run_market.py")
    else:
        if es is not None:
            n_storms = es.storm.nunique()
            a, b, c, d = st.columns(4)
            a.metric("Storms with prices", n_storms)
            b.metric("Bank × storm observations", int(len(es[es.definition == "full"])) if "definition" in es else len(es))
            nz = int((es[es.definition == "full"].exposure_pct > 0).sum()) if "definition" in es else int((es.exposure_pct > 0).sum())
            c.metric("…with exposure_pct > 0", nz)
            d.metric("Benchmark", str(MODEL.get("event_study", {}).get("benchmark", "KRE")))
            if nz < 3:
                st.info("Almost no priced bank has non-zero satellite exposure yet, so the cross-section on exposure_pct has no variation "
                        "to explain. This is a null result by construction, not evidence either way. The AOI-deposits measure is shown as a looser proxy.")
        if cs:
            st.markdown("**Cross-section: CAR on exposure, storm fixed effects, SE clustered by storm**")
            t = _cs_table(cs)
            if len(t):
                st.dataframe(t.style.format({"coef per pct": "{:.2e}", "t": "{:.2f}", "p": "{:.3f}"}, na_rep="—"), hide_index=True, width="stretch")
            source(DATA / "cross_section.json")
        if es is not None:
            st.markdown("**Event study** (market model vs benchmark, estimation window "
                        f"{MODEL.get('event_study', {}).get('estimation_window', [-250, -30])} trading days)")
            d_es = es[es.definition == "full"] if "definition" in es else es
            meas = st.radio("x-axis", ["exposure_pct", "aoi_deposits_pct"], horizontal=True, key="es_meas")
            win = st.radio("window", [c for c in ("car_0_1", "car_0_5", "car_0_20") if c in d_es.columns], horizontal=True, key="es_win")
            plot = d_es.dropna(subset=[win, meas])
            if len(plot):
                storms_in = [s for s in STORMS_CFG if s in set(plot.storm)]
                palette = [S1_BLUE, S2_ORANGE, S3_AQUA, S4_YELLOW, "#e87ba4", "#008300", "#4a3aa7", "#e34948"]
                if len(storms_in) > 8:
                    plot = plot.assign(storm=plot.storm.where(plot.storm.isin(storms_in[:7]), "other"))
                    storms_in = storms_in[:7] + ["other"]
                chart = (alt.Chart(plot).mark_point(filled=True, opacity=0.75)
                         .encode(x=alt.X(f"{meas}:Q", title=meas), y=alt.Y(f"{win}:Q", title=win.replace("car_", "CAR [").replace("_", ", ") + "]", axis=alt.Axis(format=".1%")),
                                 color=alt.Color("storm:N", scale=alt.Scale(domain=storms_in, range=palette[:len(storms_in)]), title="storm"),
                                 tooltip=["ticker", "bank_name", "storm", alt.Tooltip(f"{meas}:Q", format=".2f"), alt.Tooltip(f"{win}:Q", format=".2%"), alt.Tooltip("pre_car:Q", format=".2%")])
                         .properties(height=340, title=f"{win} vs {meas} (one point per bank × storm)"))
                st.altair_chart(chart, width="stretch")
                with st.expander("Event-study table"):
                    cols = [c for c in ("storm", "ticker", "bank_name", "exposure_pct", "aoi_deposits_pct", "day0", "days_landfall_to_day0", "pre_car", "car_0_1", "car_0_5", "car_0_20", "beta", "n_est") if c in d_es.columns]
                    st.dataframe(d_es[cols].sort_values(["storm", "exposure_pct"], ascending=[True, False]), hide_index=True, width="stretch", height=360)
            source(DATA / "event_study.parquet", "pre_car = abnormal return from the last close before landfall to day −1, i.e. what the market already knew")
            for png in sorted((DATA / "market").glob("*.png")) if (DATA / "market").exists() else []:
                st.image(str(png), caption=rel(png))
        st.markdown("**Long-short backtest** (short top exposure tercile, long bottom tercile or KRE, 10 bp per side)")
        if bts is None and bt is None:
            missing("Backtest", DATA / "backtest_summary.json", "scripts/run_market.py")
        else:
            if bts:
                by_hold = bts.get("by_hold") or {}
                cols = st.columns(max(1, len(by_hold)))
                for i, (h, r) in enumerate(by_hold.items()):
                    with cols[i]:
                        st.metric(f"Hold {h} d — trades", r.get("n_trades", 0))
                        if r.get("n_trades"):
                            st.metric("Cumulative P&L", f"{r.get('cum_return', float('nan')):.2%}" if r.get("cum_return") is not None else "—")
                            st.metric("Hit rate", f"{r.get('hit_rate', float('nan')):.0%}" if r.get("hit_rate") is not None else "—")
                        else:
                            st.caption(r.get("note", "no trades"))
                if bts.get("skipped"):
                    st.caption(f"Skipped storms (fewer than 3 exposed banks): {', '.join(bts['skipped'])}")
            if bt is not None:
                st.dataframe(bt, hide_index=True, width="stretch")
            source(DATA / "backtest_summary.json")


# ========================================================== validation ====
with tab_valid:
    V = DATA / "validation"
    vs = load_json(V / "validation_summary.json")
    st.subheader("Satellite flood map vs FEMA NFIP claims (ground truth)")
    if vs is None:
        missing("Validation summary", V / "validation_summary.json", "scripts/run_validation.py")
    else:
        defs = vs.get("definitions") or {}
        st.caption(" · ".join(f"{k}: {v}" for k, v in defs.items()))
        st.caption(f"Validated: {', '.join(vs.get('storms_validated', []))} · pending: {', '.join(vs.get('storms_pending', [])) or 'none'}")
        for n in vs.get("notes") or []:
            st.caption(n)
        pooled = vs.get("pooled") or {}
        if pooled:
            a, b, c, d = st.columns(4)
            a.metric("Tracts scored (SAR-covered)", pooled.get("n_tracts", "—"))
            sp = (pooled.get("spearman_pct_vs_density") or {})
            b.metric("Spearman ρ (flood % vs claims density)", fmt_num(sp.get("r"), "{:.3f}"), delta=f"p = {fmt_num(sp.get('p'), '{:.2f}')}", delta_color="off")
            c.metric("AUC — SAR flood %", fmt_num(pooled.get("auc_pct_flooded"), "{:.3f}"))
            d.metric("AUC — baseline (county declared)", fmt_num(pooled.get("auc_fema_declared"), "{:.3f}"))
            conf, base = pooled.get("confusion") or {}, pooled.get("baseline_fema_declared") or {}
            cm = pd.DataFrame([
                {"classifier": "SAR flooded tract (≥ 1 %)", "TP": conf.get("tp"), "FP": conf.get("fp"), "FN": conf.get("fn"), "TN": conf.get("tn"),
                 "precision": conf.get("precision"), "recall": conf.get("recall")},
                {"classifier": "Baseline: county FEMA-declared", "TP": base.get("tp"), "FP": base.get("fp"), "FN": base.get("fn"), "TN": base.get("tn"),
                 "precision": base.get("precision"), "recall": base.get("recall")}])
            st.dataframe(cm.style.format({"precision": "{:.3f}", "recall": "{:.3f}"}, na_rep="—"), hide_index=True, width="stretch")
            auc = pooled.get("auc_pct_flooded")
            if auc is not None and auc < 0.55:
                st.warning("At the current thresholds the SAR map does **not** rank tracts by claims better than chance on the storms validated so far. "
                           "The first post-storm pass often comes before river crests and after surge recedes; this is the central limitation of the "
                           "approach and is reported as such.", icon="📉")
        per = vs.get("per_storm") or {}
        if per:
            rows = []
            for s, r in per.items():
                rows.append({"storm": s, "tracts": r.get("n_tracts"), "tracts with claims": r.get("n_tracts_with_claims"), "claims": r.get("n_claims"),
                             "Spearman ρ": (r.get("spearman_pct_vs_density") or {}).get("r"), "AUC SAR": r.get("auc_pct_flooded"), "AUC declared": r.get("auc_fema_declared"),
                             "mean claims, flooded tracts": r.get("mean_claims_flooded_tracts"), "mean claims, dry tracts": r.get("mean_claims_dry_tracts")})
            st.dataframe(pd.DataFrame(rows).style.format({"Spearman ρ": "{:.3f}", "AUC SAR": "{:.3f}", "AUC declared": "{:.3f}", "mean claims, flooded tracts": "{:.1f}", "mean claims, dry tracts": "{:.1f}"}, na_rep="—"),
                         hide_index=True, width="stretch")
        source(V / "validation_summary.json")
        pngs = sorted(V.glob("*.png")) if V.exists() else []
        if pngs:
            cols = st.columns(min(2, len(pngs)))
            for i, p in enumerate(pngs):
                cols[i % len(cols)].image(str(p), caption=rel(p))
        st.markdown("**Threshold tuning** — grid searched on training storms only; held-out storms report the chosen thresholds without re-tuning")
        tt = vs.get("threshold_tuning") or {}
        if tt:
            a, b, c = st.columns(3)
            a.metric("Training storms in grid", ", ".join(tt.get("train_storms_in_grid", [])) or "—")
            best, dflt = tt.get("best_on_train") or {}, tt.get("default") or {}
            b.metric("Best on train", f"{best.get('diff_db', '—')} dB / {best.get('post_db_max', '—')} dB", delta=f"score {fmt_num(best.get('train_score'), '{:.3f}')}", delta_color="off")
            c.metric("Default thresholds", f"{dflt.get('diff_db', '—')} dB / {dflt.get('post_db_max', '—')} dB", delta=f"score {fmt_num(dflt.get('train_score'), '{:.3f}')}", delta_color="off")
            if tt.get("heldout"):
                st.json(tt["heldout"])
            st.caption(f"Metric: {tt.get('metric', '—')}. Train = Harvey, Irma, Florence, Michael, Laura, Sally; held-out = Ida, Ian, Idalia, Helene, Milton.")
        grid = load_parquet(V / "threshold_grid.parquet")
        if grid is not None:
            st.dataframe(grid.style.format({"flooded_km2": "{:.1f}", "spearman": "{:.3f}", "auc": "{:.3f}", "precision": "{:.3f}", "recall": "{:.3f}"}, na_rep="—"), hide_index=True, width="stretch", height=300)
            source(V / "threshold_grid.parquet")
        tv = load_parquet(V / "tract_validation.parquet")
        if tv is not None and len(tv):
            with st.expander("Tract-level table (SAR-covered tracts)"):
                st.dataframe(tv[tv.sar_covered].sort_values("n_claims", ascending=False).head(500), hide_index=True, width="stretch", height=360)
                source(V / "tract_validation.parquet")


# ========================================================= methodology ====
PIPELINE = """
```
 Sentinel-1 RTC COGs (Planetary Computer) ─┐
 Copernicus catalog: PublicationDate ───────┤ flood/catalog.py      scenes, orbits, event time (no lookahead)
 JRC surface water · NASADEM slope ─────────┤ flood/mpc_backend.py  post−pre dB change → flood_mask.tif, polygons, previews
 NWS warnings · NHC track · FEMA counties ──┘ flood/sanity.py       independent check of each map
                                              │
 FDIC Summary of Deposits (vintage < storm) ──┤ banks/fdic.py, banks/exposure.py   every bank: flooded deposits / total
 config/bank_tickers.csv ─────────────────────┤ market/event_study.py, backtest.py  CARs vs KRE, cross-section, long-short
 Census ACS income · TIGER tracts ────────────┤ census/acs.py, exposure.py          tract % flooded
 Nessie /branches /atms (+ Census geocoder) ──┤ nessie/client.py                    Capital One locations
 SYNTHETIC customers → Nessie sandbox ────────┤ nessie/portfolio.py, stress.py      EL = EAD × PD × LGD, flooded vs control
 OpenFEMA NFIP claims v3 ─────────────────────┤ validation/nfip.py                  claims vs flood %, threshold tuning
 api.weather.gov alerts · NHC CurrentStorms ──┘ live/alerts.py, live/exposure.py    pre-event exposure, next satellite pass
                                              ▼
                                        app/app.py (this dashboard)
```
"""

with tab_method:
    st.subheader("How the numbers are made")
    st.markdown(PIPELINE)
    st.markdown("**Data-source verification.** `scripts/verify_sources.py` hits every external source, prints a sample record and "
                "schema, and exits non-zero if a required source fails. Run it before a demo: "
                "`.venv/bin/python scripts/verify_sources.py` (`--quick` skips the Earth Engine probe). "
                "Nessie writes need `NESSIE_API_KEY`; the Census API key is optional (keyless summary files are used).")
    st.markdown("**Reproduce.** `scripts/run_all.py` (flood maps for all storms) → `scripts/run_banks.py` → `scripts/run_market.py` → "
                "`scripts/run_validation.py` → `scripts/run_live.py`. Raw pulls are cached under `data/raw/`; outputs under `data/processed/`.")
    if DONE:
        st.caption(f"Storms with a finished flood map right now: {', '.join(DONE)}.")
    txt = load_text(ROOT / "ASSUMPTIONS.md")
    if txt:
        st.markdown(txt)
        source(ROOT / "ASSUMPTIONS.md")
    else:
        missing("ASSUMPTIONS.md", ROOT / "ASSUMPTIONS.md")
    with st.expander("config/model.yaml"):
        st.json(MODEL)
