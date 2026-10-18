#!/usr/bin/env python
"""Build config/bank_tickers.csv: holding company (or bank, when it has no HC) -> ticker.

Candidates are hand-listed below (company, ticker, regex on NAMEHCR / NAMEFULL). The script resolves each
pattern against the 2024 SOD vintage to fill rssdhcr + matched_name + deposits, verifies the ticker on yfinance
(2024 prices exist and the long name resembles the company), and writes the CSV. Delisted/acquired names are
kept with a blank ticker so survivorship is explicit. Run after data/raw/fdic/sod_2024.parquet exists.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pandas as pd
import yfinance as yf

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from floodalpha import config  # noqa: E402
from floodalpha.banks.fdic import sod_year  # noqa: E402

# (company, ticker, pattern, match_on, notes). match_on: "hc" = NAMEHCR, "bank" = NAMEFULL (no holding company)
CANDIDATES = [
    ("JPMorgan Chase & Co.", "JPM", r"^JPMORGAN CHASE", "hc", ""),
    ("Bank of America Corp.", "BAC", r"^BANK OF AMERICA CORP", "hc", ""),
    ("Wells Fargo & Co.", "WFC", r"^WELLS FARGO & CO", "hc", ""),
    ("Citigroup Inc.", "C", r"^CITIGROUP", "hc", ""),
    ("U.S. Bancorp", "USB", r"^U\.?S\.? BANCORP", "hc", ""),
    ("PNC Financial Services Group", "PNC", r"^PNC FINANCIAL", "hc", ""),
    ("Truist Financial Corp.", "TFC", r"^TRUIST FINANCIAL|^BB&T CORP", "hc", "BB&T renamed Truist Dec 2019; yfinance TFC carries BB&T history"),
    ("Capital One Financial Corp.", "COF", r"^CAPITAL ONE FINANCIAL", "hc", ""),
    ("Goldman Sachs Group", "GS", r"^GOLDMAN SACHS GROUP", "hc", "no retail branches"),
    ("Morgan Stanley", "MS", r"^MORGAN STANLEY$", "hc", "no retail branches"),
    ("Bank of New York Mellon", "BNY", r"^BANK OF NEW YORK MELLON", "hc", "ticker changed BK -> BNY in 2025; yfinance BNY carries full history"),
    ("State Street Corp.", "STT", r"^STATE STREET CORP", "hc", ""),
    ("Charles Schwab Corp.", "SCHW", r"^CHARLES SCHWAB CORP", "hc", ""),
    ("Fifth Third Bancorp", "FITB", r"^FIFTH THIRD BANCORP", "hc", ""),
    ("Huntington Bancshares", "HBAN", r"^HUNTINGTON BANCSHARES", "hc", ""),
    ("Regions Financial Corp.", "RF", r"^REGIONS FINANCIAL", "hc", ""),
    ("KeyCorp", "KEY", r"^KEYCORP", "hc", ""),
    ("M&T Bank Corp.", "MTB", r"^M&T BANK CORP", "hc", ""),
    ("Citizens Financial Group", "CFG", r"^CITIZENS FINANCIAL GROUP", "hc", ""),
    ("First Citizens BancShares", "FCNCA", r"^FIRST CITIZENS BANCSHARES, INC", "hc", ""),
    ("Ally Financial", "ALLY", r"^ALLY FINANCIAL", "hc", "online bank"),
    ("Zions Bancorporation", "ZION", r"^ZIONS BANCORPORATION", "bank", "bank is the top-tier entity (no HC) since 2018"),
    ("Comerica Inc.", "CMA", r"^COMERICA INC", "hc", ""),
    ("Western Alliance Bancorporation", "WAL", r"^WESTERN ALLIANCE BANCORP", "hc", ""),
    ("East West Bancorp", "EWBC", r"^EAST WEST BANCORP", "hc", ""),
    ("Synovus Financial Corp.", "SNV", r"^SYNOVUS FINANCIAL", "hc", ""),
    ("Webster Financial Corp.", "WBS", r"^WEBSTER FINANCIAL", "hc", ""),
    ("Valley National Bancorp", "VLY", r"^VALLEY NATIONAL BANCORP", "hc", ""),
    ("Pinnacle Financial Partners", "PNFP", r"^PINNACLE FINANCIAL PARTNERS", "hc", ""),
    ("First Horizon Corp.", "FHN", r"^FIRST HORIZON (NATIONAL )?CORP", "hc", ""),
    ("BOK Financial Corp.", "BOKF", r"^BOK FINANCIAL", "hc", ""),
    ("Cullen/Frost Bankers", "CFR", r"^CULLEN/FROST BANKERS", "hc", ""),
    ("SouthState Corp.", "SSB", r"^SOUTH ?STATE CORP", "hc", "merged with CenterState June 2020"),
    ("UMB Financial Corp.", "UMBF", r"^UMB FINANCIAL", "hc", ""),
    ("Old National Bancorp", "ONB", r"^OLD NATIONAL BANCORP", "hc", ""),
    ("Wintrust Financial Corp.", "WTFC", r"^WINTRUST FINANCIAL", "hc", ""),
    ("Commerce Bancshares", "CBSH", r"^COMMERCE BANCSHARES", "hc", ""),
    ("Prosperity Bancshares", "PB", r"^PROSPERITY BANCSHARES", "hc", ""),
    ("Hancock Whitney Corp.", "HWC", r"^HANCOCK WHITNEY|^HANCOCK HOLDING", "hc", "Hancock Holding renamed 2018"),
    ("Associated Banc-Corp", "ASB", r"^ASSOCIATED BANC-CORP", "hc", ""),
    ("F.N.B. Corp.", "FNB", r"^F\.N\.B\. CORP", "hc", ""),
    ("Bank OZK", "OZK", r"^BANK OZK|^BANK OF THE OZARKS", "bank", "bank is top-tier (no HC) since 2017"),
    ("Popular Inc.", "BPOP", r"^POPULAR, INC", "hc", "Puerto Rico + NY/FL"),
    ("Texas Capital Bancshares", "TCBI", r"^TEXAS CAPITAL BANCSHARES", "hc", ""),
    ("Glacier Bancorp", "GBCI", r"^GLACIER BANCORP", "hc", ""),
    ("First Financial Bankshares", "FFIN", r"^FIRST FINANCIAL BANKSHARES", "hc", "Texas"),
    ("BankUnited Inc.", "BKU", r"^BANKUNITED, INC", "hc", ""),
    ("United Bankshares", "UBSI", r"^UNITED BANKSHARES", "hc", ""),
    ("Cadence Bank", "CADE", r"^CADENCE BANK$", "bank", "BancorpSouth Bank (no HC) merged with Cadence Oct 2021 and kept ticker CADE; yfinance CADE history before Oct 2021 is BancorpSouth's (BXS)"),
    ("Simmons First National", "SFNC", r"^SIMMONS FIRST NATIONAL", "hc", ""),
    ("Home BancShares", "HOMB", r"^HOME BANCSHARES", "hc", ""),
    ("Columbia Banking System", "COLB", r"^COLUMBIA BANKING SYSTEM", "hc", ""),
    ("First Interstate BancSystem", "FIBK", r"^FIRST INTERSTATE BANCSYSTEM", "hc", ""),
    ("Ameris Bancorp", "ABCB", r"^AMERIS BANCORP", "hc", ""),
    ("United Community Banks", "UCB", r"^UNITED COMMUNITY BANKS", "hc", "ticker changed UCBI -> UCB in 2024"),
    ("Seacoast Banking Corp. of Florida", "SBCF", r"^SEACOAST BANKING CORP", "hc", ""),
    ("Trustmark Corp.", "TRMK", r"^TRUSTMARK CORP", "hc", ""),
    ("Renasant Corp.", "RNST", r"^RENASANT CORP", "hc", ""),
    ("ServisFirst Bancshares", "SFBS", r"^SERVISFIRST BANCSHARES", "hc", ""),
    ("Origin Bancorp", "OBK", r"^ORIGIN BANCORP", "hc", ""),
    ("Capital City Bank Group", "CCBG", r"^CAPITAL CITY BANK GROUP", "hc", ""),
    ("Veritex Holdings", "VBTX", r"^VERITEX HOLDINGS", "hc", ""),
    ("Independent Bank Group", "IBTX", r"^INDEPENDENT BANK GROUP", "hc", ""),
    ("Stellar Bancorp", "STEL", r"^STELLAR BANCORP|^CBTX, INC", "hc", "CBTX + Allegiance merged Oct 2022"),
    ("Business First Bancshares", "BFST", r"^BUSINESS FIRST BANCSHARES", "hc", "b1BANK"),
    ("Home Bancorp", "HBCP", r"^HOME BANCORP, INC", "hc", "Louisiana"),
    ("Investar Holding", "ISTR", r"^INVESTAR HOLDING", "hc", ""),
    ("First Bancorp (NC)", "FBNC", r"^FIRST BANCORP$", "hc", "NC; NAMEHCR is just 'FIRST BANCORP' - resolved by rssdhcr 1076431"),
    ("Live Oak Bancshares", "LOB", r"^LIVE OAK BANCSHARES", "hc", ""),
    ("Atlantic Union Bankshares", "AUB", r"^ATLANTIC UNION BANKSHARES|^UNION BANKSHARES CORP", "hc", "renamed 2019"),
    ("TowneBank", "TOWN", r"^TOWNEBANK", "bank", "bank is top-tier (no HC)"),
    ("International Bancshares", "IBOC", r"^INTERNATIONAL BANCSHARES", "hc", "Texas"),
    ("Southside Bancshares", "SBSI", r"^SOUTHSIDE BANCSHARES", "hc", "Texas"),
    ("Amerant Bancorp", "AMTB", r"^AMERANT BANCORP|^MERCANTIL BANK HOLDING", "hc", "Florida; renamed 2019"),
    ("Hilltop Holdings", "HTH", r"^PLAINSCAPITAL BANK$", "bank", "PlainsCapital; SOD lists an intermediate LLC as HC, so matched on bank name"),
    ("Third Coast Bancshares", "TCBX", r"^THIRD COAST BANCSHARES", "hc", "Texas, IPO Nov 2021"),
    ("Guaranty Bancshares", "GNTY", r"^GUARANTY BANCSHARES", "hc", "Texas"),
    ("South Plains Financial", "SPFI", r"^SOUTH PLAINS FINANCIAL", "hc", "Texas"),
    ("Triumph Financial", "TFIN", r"^TRIUMPH FINANCIAL|^TRIUMPH BANCORP", "hc", "Texas"),
    ("Colony Bankcorp", "CBAN", r"^COLONY BANKCORP", "hc", "Georgia"),
    ("MetroCity Bankshares", "MCBS", r"^METROCITY BANKSHARES", "hc", "Georgia"),
    ("FB Financial Corp.", "FBK", r"^FB FINANCIAL CORP", "hc", "Tennessee"),
    ("SmartFinancial", "SMBK", r"^SMARTFINANCIAL", "hc", "Tennessee"),
    ("HomeTrust Bancshares", "HTBI", r"^HOMETRUST BANCSHARES", "hc", "NC"),
    ("Peoples Bancorp of NC", "PEBK", r"^PEOPLES BANCORP OF NORTH CAROLINA", "hc", ""),
    ("First Community Bankshares", "FCBC", r"^FIRST COMMUNITY BANKSHARES", "hc", "VA/NC/TN"),
    ("Southern First Bancshares", "SFST", r"^SOUTHERN FIRST BANCSHARES", "hc", "SC"),
    ("C&F Financial Corp.", "CFFI", r"^C&F FINANCIAL", "hc", "VA"),
    ("Burke & Herbert Financial Services", "BHRB", r"^BURKE & HERBERT", "hc", "VA; listed 2023"),
    ("Primis Financial", "FRST", r"^PRIMIS FINANCIAL|^SOUTHERN NATIONAL BANCORP OF VIRGINIA", "hc", "VA"),
    ("Red River Bancshares", "RRBI", r"^RED RIVER BANCORP", "hc", "Louisiana"),
    ("First Guaranty Bancshares", "FGBI", r"^FIRST GUARANTY BANK$", "bank", "Louisiana; SOD lists an intermediate LLC as HC, so matched on bank name"),
    ("First Bancshares (MS)", "FBMS", r"^FIRST BANCSHARES, INC", "hc", "acquired by Renasant Apr 2025; yfinance history through early 2025 only"),
    ("Toronto-Dominion Bank", "TD", r"^TORONTO-DOMINION BANK", "hc", "NYSE ADR of Canadian parent; TD Bank US branches"),
    ("Bank of Montreal", "BMO", r"^BANK OF MONTREAL", "hc", "NYSE ADR; BMO Harris / Bank of the West"),
    ("Royal Bank of Canada", "RY", r"^ROYAL BANK OF CANADA", "hc", "NYSE ADR; City National"),
    ("New York Community / Flagstar", "FLG", r"^NEW YORK COMMUNITY BANCORP|^FLAGSTAR FINANCIAL", "hc", "NYCB renamed Flagstar Financial, ticker FLG from Oct 2024; FL branches"),
    # ---- acquired / delisted: no yfinance history, kept for transparency (ticker blank) ----
    ("SunTrust Banks", "STI", r"^SUNTRUST BANKS", "hc", "DELISTED: merged into Truist Dec 2019 (was STI)"),
    ("IBERIABANK Corp.", "IBKC", r"^IBERIABANK CORP", "hc", "DELISTED: merged into First Horizon July 2020 (was IBKC)"),
    ("CenterState Bank Corp.", "CSFL", r"^CENTERSTATE BANK", "hc", "DELISTED: merged into SouthState June 2020 (was CSFL)"),
    ("BancorpSouth Bank (pre-2021)", "BXS", r"^BANCORPSOUTH, INC|^BANCORPSOUTH BANK$", "bank", "see Cadence row; BXS ticker retired Oct 2021"),
    ("Cadence Bancorporation (pre-2021)", "", r"^CADENCE BANCORPORATION", "hc", "DELISTED as separate entity Oct 2021; ticker CADE continued under merged bank"),
    ("TCF Financial", "TCF", r"^TCF FINANCIAL", "hc", "DELISTED: merged into Huntington June 2021"),
    ("FCB Financial Holdings", "FCB", r"^FCB FINANCIAL HOLDINGS", "hc", "DELISTED: acquired by Synovus Jan 2019"),
    ("Capital Bank Financial", "CBF", r"^CAPITAL BANK FINANCIAL", "hc", "DELISTED: acquired by First Horizon Nov 2017"),
    ("State Bank Financial", "STBZ", r"^STATE BANK FINANCIAL", "hc", "DELISTED: acquired by Cadence Jan 2019"),
    ("Green Bancorp", "GNBC", r"^GREEN BANCORP", "hc", "DELISTED: acquired by Veritex Jan 2019"),
    ("LegacyTexas Financial", "LTXB", r"^LEGACYTEXAS FINANCIAL", "hc", "DELISTED: acquired by Prosperity Nov 2019"),
    ("Fidelity Southern Corp.", "LION", r"^FIDELITY SOUTHERN", "hc", "DELISTED: acquired by Ameris July 2019"),
    ("Atlantic Capital Bancshares", "ACBI", r"^ATLANTIC CAPITAL BANCSHARES", "hc", "DELISTED: acquired by SouthState Mar 2022"),
    ("Allegiance Bancshares", "ABTX", r"^ALLEGIANCE BANCSHARES", "hc", "DELISTED: merged into Stellar Oct 2022"),
    ("MidSouth Bancorp", "MSL", r"^MIDSOUTH BANCORP", "hc", "DELISTED: acquired by Hancock Whitney Sept 2019"),
    ("Bank of the Ozarks (pre-2017 HC)", "", r"^BANK OF THE OZARKS, INC", "hc", "see Bank OZK row"),
    ("First NBC Bank Holding", "FNBC", r"^FIRST NBC BANK HOLDING", "hc", "DELISTED: bank failed Apr 2017"),
    ("Astoria / others", "", r"^$", "hc", "placeholder-free: pattern never matches"),
]


def main() -> None:
    sod = sod_year(2024)
    hc = sod[sod["rssdhcr"] > 0].groupby(["rssdhcr", "namehcr"])["deposits_k"].sum().reset_index()
    banks = sod.groupby(["cert", "bank_name"])["deposits_k"].sum().reset_index()
    rows = []
    for company, ticker, pat, on, notes in CANDIDATES:
        rx = re.compile(pat, re.I)
        if on == "hc":
            m = hc[hc["namehcr"].str.contains(rx)]
            if company.startswith("First Bancorp (NC)"):
                m = m[m["rssdhcr"] == 1076431]
            m = m.sort_values("deposits_k", ascending=False)
            rssd = int(m["rssdhcr"].iloc[0]) if len(m) else 0
            matched = str(m["namehcr"].iloc[0]) if len(m) else ""
            dep = float(m["deposits_k"].iloc[0]) if len(m) else 0.0
        else:
            m = banks[banks["bank_name"].str.contains(rx)].sort_values("deposits_k", ascending=False)
            rssd = 0
            matched = str(m["bank_name"].iloc[0]) if len(m) else ""
            dep = float(m["deposits_k"].iloc[0]) if len(m) else 0.0
        rows.append({"company": company, "ticker": ticker, "namehcr_pattern": pat, "match_on": on, "rssdhcr": rssd,
                     "matched_name_2024": matched, "deposits_2024_k": dep, "notes": notes})
    df = pd.DataFrame(rows)
    df = df[df["namehcr_pattern"] != r"^$"]

    # yfinance verification
    tickers = [t for t in df["ticker"] if t]
    px = yf.download(tickers, start="2024-01-01", end="2024-12-31", progress=False, auto_adjust=True)["Close"]
    ok, names = {}, {}
    for t in tickers:
        n = int(px[t].notna().sum()) if t in px else 0
        ok[t] = n > 200
        try:
            names[t] = yf.Ticker(t).info.get("longName") or yf.Ticker(t).info.get("shortName") or ""
        except Exception as e:  # noqa: BLE001
            names[t] = f"ERR {e}"[:60]
    df["yf_days_2024"] = df["ticker"].map(lambda t: int(px[t].notna().sum()) if t and t in px else 0)
    df["yf_name"] = df["ticker"].map(lambda t: names.get(t, "") if t else "")
    def name_ok(company: str, yfn: str) -> bool:
        # a reused ticker (e.g. STI, LION) resolves to an unrelated company: require a shared significant word
        stop = {"inc", "corp", "corporation", "the", "of", "co", "group", "financial", "bank", "bancorp", "bancshares", "holdings", "&", "/", "(nc)", "(ms)", "(pre-2021)", "(pre-2017", "hc)"}
        words = [w.strip(".,").lower() for w in re.split(r"[\s/]+", company) if w.strip(".,").lower() not in stop and len(w) > 2]
        return bool(yfn) and (not words or any(w in yfn.lower() for w in words))
    df["yf_name_ok"] = [name_ok(c, names.get(t, "")) if t else False for c, t in zip(df["company"], df["ticker"])]
    df["yf_ok"] = [bool(ok.get(t, False) and nk) if t else False for t, nk in zip(df["ticker"], df["yf_name_ok"])]
    df["status"] = ["active" if o else ("delisted" if t else "none") for t, o in zip(df["ticker"], df["yf_ok"])]
    # rows the 2024-SOD match test applies to: active tickers only (delisted names are absent from 2024 by construction)
    out = config.CONFIG_DIR / "bank_tickers.csv"
    df.to_csv(out, index=False)
    pd.set_option("display.width", 250); pd.set_option("display.max_rows", 300); pd.set_option("display.max_colwidth", 45)
    print(df[["company", "ticker", "rssdhcr", "matched_name_2024", "deposits_2024_k", "yf_days_2024", "yf_name", "yf_ok"]].to_string())
    print("\nUNMATCHED in 2024 SOD (expected for delisted):", df[df["matched_name_2024"] == ""]["company"].tolist())
    print("TICKERS WITHOUT yfinance history (status=delisted):", df[(df["ticker"] != "") & (~df["yf_ok"])]["ticker"].tolist())
    print("ACTIVE rows unmatched in 2024 SOD:", df[(df["status"] == "active") & (df["matched_name_2024"] == "")]["company"].tolist())
    print("wrote", out)


if __name__ == "__main__":
    main()
