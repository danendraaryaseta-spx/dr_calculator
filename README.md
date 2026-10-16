# dr_calculator

Daily Rent (DR) sizing for shorthaul FM/LM: how many extra rental trucks each DC needs for a date window.

## Pipeline

1. Colab (link in the sheet's `read this!` tab) copies the projection for the `set date` window into `FM Projection` / `LM Projection` of **[LH] Daily Rent Calculator Raw**.
2. `dr_engine.py` (GitHub Actions, every 6 hours or "Run workflow") reads the sheet and writes `dr_results.json`.
3. `index.html` (GitHub Pages) turns it into the recommendation and the VM Excel export.

## How the recommendation is calculated

Per DC, for the window selected on the page:

- **Demand** = FM + LM projection (inner routes only) × 1.10.
- **Cap v5** = scheduled capacity in `schedule_v5` (Cap LM + Cap FM, inner, non-DT, plus FM drops at SOCs).
- **Cap Ideal** = (in-house inner units in `data_unit_nasional` + **regular Daily Rent**) × 3 trips × 4,400 × 0.85.
  Regular DR comes from **[LH] Active Daily Rent → `Summary CDDL | DC Level`, column "Regular"** (latest day).
  Regular DR trucks run every day but are not registered units, so without them a DR-heavy DC looks short
  and the calculator recommends DR to replace trucks that are already there. If that sheet cannot be read,
  the engine falls back to in-house units only and the page says so in the note.
- Worst daily gap (capacity − demand) inside the window → trucks = floor(−gap ÷ (4,400 × 0.9 × 3)), only when the gap is worse than −1,000 parcels.
- **Nopol** = trucks vs Cap Ideal, **v5** = trucks vs Cap v5, **Min** / **P75** derived from those; **Rec (P80)** = 80th percentile of the four.
- Rec is split into **DR FM / DR LM** by the DC's FM/LM demand share. **Ov. FM / Ov. LM** are the editable final numbers.
- **Jabodetabek Transit Points** have no capacity data in the engine, so a TP with no calculated need gets the
  **TP min / day** (default 2). A TP with a calculated need keeps it.

Backtest on 10.10 (10–13 Oct 2026, ~300 extra DR actually onsite at DCs): in-house-only Rec 367, v5 only 103,
Rec with regular DR 268 — the closest, which is why regular DR is part of Cap Ideal.

## Excel export (contract rows)

Contract start + contract days (default 4):

- **FM**: from the start date, Sundays skipped, 13:00.
- **LM**: from start date + the DC's offset (`mapping` tab), 08:00 in Java, mapping time elsewhere.
- Where FM and LM days overlap, one shared pool sized to the bigger leg covers both, so some rows are shorter or longer than the contract days.
- **Transit Points**: one row, 13:00, 24h, every day of the contract (no Sunday skip, no offset), quantity = max(Ov. FM, Ov. LM).
- **Grand Total** = per DC max(Ov. FM, Ov. LM) — the trucks to procure.
