"""
DR Engine — runs in GitHub Actions, reads Google Sheet, outputs dr_results.json
"""
import os, json, datetime as dt
import pandas as pd, numpy as np
import gspread
from google.oauth2.service_account import Credentials

# ---- CONFIG ----
SHEET_ID          = "1IExA51bpbzcx_5dsKI3Z1IRPAPNerg5xQ0axm-QYuVc"
PARCELS_PER_TRUCK = 4400
UTILIZATION       = 0.90
TRIPS_FM          = 2.5
TRIPS_LM          = 3.0
OVERPROJECTION    = 1.10
IDEAL_FACTOR      = 0.85
THRESHOLD         = -1000

TAB = dict(
    fm="FM Projection",
    lm="LM Projection",
    sched="schedule_v5",
    active="Active DC",
    units="data_unit_nasional",
    rename="Change Origin due to Multidrop",
)
HEADER_ROW = {"schedule_v5": 3, "worksheet inner": 6, "data_unit_nasional": 2}

SCOL = dict(
    origin="Origin",
    scheme="LH Scheme",
    segment="LH by Distance",
    cap_lm="Cap LM",
    cap_fm="Cap FM",
    dt="DT VALIDATION",
    soc=["FM SOC1", "FM SOC2", "FM SOC3", "FM SOC4"],
)
SEGMENT_INNER = "Inner"

# ---- AUTH ----
creds_json = json.loads(os.environ["GOOGLE_CREDENTIALS"])
creds = Credentials.from_service_account_info(creds_json, scopes=[
    "https://www.googleapis.com/auth/spreadsheets.readonly",
])
gc = gspread.authorize(creds)
ss = gc.open_by_key(SHEET_ID)


def load(tab):
    ws = ss.worksheet(tab)
    rows = ws.get_all_values()
    if not rows:
        return pd.DataFrame()
    hr = HEADER_ROW.get(tab, 1)
    headers = rows[hr - 1]
    seen = {}
    clean = []
    for h in headers:
        h = str(h).strip()
        if h == "":
            h = "_blank"
        if h in seen:
            seen[h] += 1
            clean.append(f"{h}_{seen[h]}")
        else:
            seen[h] = 0
            clean.append(h)
    df = pd.DataFrame(rows[hr:], columns=clean)
    return df


def col(df, *names):
    for n in names:
        if n in df.columns:
            return n
    raise KeyError(f"none of {names} in {list(df.columns)[:8]}...")


# ---- LOAD DATA ----
print("Loading tabs...")
loaded = {}
for key, tab_name in TAB.items():
    try:
        loaded[key] = load(tab_name)
        print(f"  OK {key:8s} -> {tab_name!r:25s} {loaded[key].shape}")
    except gspread.WorksheetNotFound:
        print(f"  !! {key:8s} -> {tab_name!r:25s} NOT FOUND")

fm = loaded.get("fm", pd.DataFrame())
lm = loaded.get("lm", pd.DataFrame())
sched = loaded.get("sched", pd.DataFrame())
active = loaded.get("active", pd.DataFrame())

# ---- DEMAND MATRIX ----
print("\nBuilding demand matrix...")
fm_dst = col(fm, "Destination Station Name", "Destination")
fm_dt = col(fm, "Fcst Date", "Date")
fm_ado = col(fm, "Forecast ADO", "ADO")
lm_org = col(lm, "Start Station Name", "Origin", "Start")
lm_dt = col(lm, "Fcst Date", "Date")
lm_ado = col(lm, "Forecast ADO", "ADO")

fm[fm_ado] = pd.to_numeric(fm[fm_ado].astype(str).str.replace(",", "", regex=False), errors="coerce").fillna(0)
lm[lm_ado] = pd.to_numeric(lm[lm_ado].astype(str).str.replace(",", "", regex=False), errors="coerce").fillna(0)

fm_rt = col(fm, "Route type", "Route Type", "route_type")
lm_rt = col(lm, "Route type", "Route Type", "route_type")
fm = fm[fm[fm_rt].astype(str).str.strip() == "Inner"].copy()
lm = lm[lm[lm_rt].astype(str).str.strip() == "Inner"].copy()
print(f"  After Inner filter: FM {len(fm)} rows, LM {len(lm)} rows")

for d, c in [(fm, fm_dt), (lm, lm_dt)]:
    d[c] = pd.to_datetime(d[c], dayfirst=False, errors="coerce").dt.strftime("%Y-%m-%d")

fm_d = fm.groupby([fm_dst, fm_dt])[fm_ado].sum().rename_axis(["dc", "date"]).rename("fm")
lm_d = lm.groupby([lm_org, lm_dt])[lm_ado].sum().rename_axis(["dc", "date"]).rename("lm")
demand = pd.concat([fm_d, lm_d], axis=1).fillna(0)
demand["fm"] = demand["fm"] * OVERPROJECTION
demand["lm"] = demand["lm"] * OVERPROJECTION
demand["proj"] = demand["fm"] + demand["lm"]
demand = demand.reset_index()
dates = sorted(demand["date"].unique())
print(f"  days: {len(dates)} | {dates[0]} -> {dates[-1]} | DC*day rows: {len(demand)}")
print(f"  Total FM: {demand['fm'].sum():,.0f} ({100*demand['fm'].sum()/demand['proj'].sum():.1f}%)")
print(f"  Total LM: {demand['lm'].sum():,.0f} ({100*demand['lm'].sum()/demand['proj'].sum():.1f}%)")

# ---- SCHEDULED CAPACITY ----
print("\nComputing scheduled capacity...")
s = sched.copy()
sc_org = SCOL["origin"]
sc_scheme = SCOL["scheme"]
sc_seg = SCOL["segment"]
sc_clm = SCOL["cap_lm"]
sc_cfm = SCOL["cap_fm"]
sc_dt = SCOL["dt"]

for c in [sc_clm, sc_cfm]:
    s[c] = pd.to_numeric(s[c].astype(str).str.replace(",", "", regex=False), errors="coerce").fillna(0)

mask = (
    (s[sc_scheme].astype(str).str.strip() != "X")
    & (s[sc_seg].astype(str).str.strip() == SEGMENT_INNER)
    & (s[sc_dt].astype(str).str.upper() != "DT")
)
s = s[mask]
print(f"  Filtered schedule: {len(s)} inner non-DT routes")

prim = s.groupby(sc_org)[[sc_clm, sc_cfm]].sum()
prim = (prim[sc_clm] + prim[sc_cfm]).rename("cap")

drop = pd.Series(dtype=float)
for socc in SCOL["soc"]:
    if socc in s.columns:
        s[socc] = s[socc].astype(str).str.strip()
        drop = drop.add(s[s[socc] != ""].groupby(socc)[sc_cfm].sum(), fill_value=0)

cap_v5 = prim.add(drop, fill_value=0).rename_axis("dc").rename("cap_v5").reset_index()
print(f"  DCs with scheduled capacity: {len(cap_v5)}")

# ---- DEBUG: check DC name matching ----
print("\n  Top 5 cap_v5:")
print(cap_v5.nlargest(5, "cap_v5").to_string())
print("\n  Top 5 demand DCs:")
print(demand.groupby("dc")["proj"].max().nlargest(5).to_string())
demand_dcs = set(demand["dc"].unique())
cap_dcs = set(cap_v5["dc"].unique())
matched = demand_dcs & cap_dcs
print(f"\n  Demand DCs: {len(demand_dcs)} | Cap DCs: {len(cap_dcs)} | Matched: {len(matched)}")
if len(matched) < 5:
    print("  !! LOW MATCH — sample demand DCs:", list(demand_dcs)[:5])
    print("  !! sample cap DCs:", list(cap_dcs)[:5])

# ---- IDEAL CAPACITY ----
print("\nComputing ideal capacity...")
u = load("data_unit_nasional")
if "Facility name" not in u.columns:
    for idx in range(min(5, len(u))):
        if "Facility name" in u.iloc[idx].values:
            u.columns = u.iloc[idx].values
            u = u.iloc[idx + 1 :].reset_index(drop=True)
            break
    u.columns = [
        str(c).strip() if str(c).strip() != "" else f"_col{i}"
        for i, c in enumerate(u.columns)
    ]

u_dc = "Facility name"
u = u[u[u_dc].astype(str).str.strip() != ""]
u = u[u[u_dc].astype(str).str.strip() != "#REF!"]
u = u[u["Unit Trip"].astype(str).str.strip() == "Inner"]
if "Health" in u.columns:
    u = u[u["Health"].astype(str).str.upper() != "DT"]

cnt = u.groupby(u_dc).size().rename("units")
ideal = (
    (cnt * 3 * PARCELS_PER_TRUCK * IDEAL_FACTOR)
    .rename("cap_ideal")
    .reset_index()
    .rename(columns={u_dc: "dc"})
)
print(f"  DCs with ideal capacity: {len(ideal)}")

# ---- ASSEMBLE ----
print("\nAssembling records...")
ac = active.copy()
ac.columns = [str(c).strip() for c in ac.columns]
reg_col, dc_col = ac.columns[0], ac.columns[1]
region = ac.set_index(dc_col)[reg_col].to_dict()

df = demand.merge(cap_v5, on="dc", how="left").merge(ideal, on="dc", how="left")
df[["cap_v5", "cap_ideal"]] = df[["cap_v5", "cap_ideal"]].fillna(0)
df["region"] = df["dc"].map(region).fillna("")
df = df[(df["proj"] > 0) | (df["cap_v5"] > 0) | (df["cap_ideal"] > 0)]
records = df[["dc", "region", "date", "proj", "fm", "lm", "cap_v5", "cap_ideal"]].round(1).to_dict("records")

# ---- DEBUG: spot check a big DC ----
check_dc = "Transit Point Kalideres DC"
check = df[df.dc == check_dc]
if len(check):
    print(f"\n  Spot check {check_dc}:")
    print(f"    cap_v5={check['cap_v5'].iloc[0]:,.0f}  cap_ideal={check['cap_ideal'].iloc[0]:,.0f}")
    print(f"    peak_proj={check['proj'].max():,.0f}  worst_gap_v5={(check['cap_v5']-check['proj']).min():,.0f}")

# ---- MAPPING (for export offsets) ----
print("\nLoading mapping...")
try:
    mp = load("mapping")
    mp_dc = "Setting"
    mp_days = "days"
    mp_time = "time"
    mapping_count = 0
    for _, row in mp.iterrows():
        dc = str(row.get(mp_dc, "")).strip()
        if dc and dc != "Setting":
            offset = int(float(row.get(mp_days, 0) or 0))
            time_val = str(row.get(mp_time, "13:00")).strip()
            for r in records:
                if r["dc"] == dc:
                    r["offset"] = offset
                    r["time"] = time_val
            mapping_count += 1
    print(f"  Mapping applied to {mapping_count} DCs")
except Exception as e:
    print(f"  Mapping skipped: {e}")

# ---- CROSS-CHECK ----
def trucks(gap, trips):
    return 0 if gap > THRESHOLD else int(np.floor(max(1, (-gap) / (PARCELS_PER_TRUCK * UTILIZATION * trips))))

tot = 0
for dc, sub in df.groupby("dc"):
    wv5 = (sub.cap_v5 - sub.proj).min()
    wid = (sub.cap_ideal - sub.proj).min()
    a0 = trucks(wid, TRIPS_LM)
    a1 = trucks(wv5, TRIPS_LM)
    nz = [x for x in (a0, a1) if x > 0]
    amin = min(nz) if nz else 0
    ap75 = round(np.percentile([a0, a1, amin], 75))
    rec = round(np.percentile([a0, a1, amin, ap75], 80))
    tot += rec
print(f"\nRecommended additional DR ({dates[0]} -> {dates[-1]}): {tot} trucks")

# ---- OUTPUT ----
payload = {
    "meta": {
        "generated": (dt.datetime.utcnow() + dt.timedelta(hours=7)).strftime("%Y-%m-%d %H:%M WIB"),
        "params": {
            "parcels_per_truck": PARCELS_PER_TRUCK,
            "utilization": UTILIZATION,
            "trips_default": TRIPS_LM,
            "threshold": THRESHOLD,
            "overprojection": OVERPROJECTION,
        },
        "dates": dates,
    },
    "records": records,
}
blob = json.dumps(payload, separators=(",", ":"))
with open("dr_results.json", "w") as f:
    f.write(blob)
print(f"Written dr_results.json ({len(blob)} bytes, {len(records)} records, {df['dc'].nunique()} DCs)")
