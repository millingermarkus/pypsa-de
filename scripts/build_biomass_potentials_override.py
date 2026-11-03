import io
import os
import tempfile
import urllib.request
from pathlib import Path

import pandas as pd
from _helpers import configure_logging

# BioMIP harmonisation spreadsheet, sheet "Biomass Feedstock" (shared by link)
SPREADSHEET_ID = "1dx2WixxgJp0Ic0BJ0vqgW-ktEG937LXtKs2022lW97Q"
SHEET_GID = 0


def retrieve(fn, spreadsheet_id=SPREADSHEET_ID, gid=SHEET_GID):
    """Download the sheet as is (no header, all cells) to the CSV file `fn`."""
    url = (
        f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/export"
        f"?format=csv&gid={gid}"
    )
    print(f"Downloading {url} to {fn}")
    with urllib.request.urlopen(url) as response:
        if "text/csv" not in response.headers.get("Content-Type", ""):
            raise RuntimeError(
                f"Expected CSV from {url}, got {response.headers.get('Content-Type')}. "
                "Is the spreadsheet shared with anyone with the link?"
            )
        text = response.read().decode("utf-8")
    df = pd.read_csv(io.StringIO(text), header=None, dtype=str)

    # write atomically, as the rule runs for several horizons in parallel
    fn = Path(fn)
    fn.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", dir=fn.parent, suffix=".csv", delete=False) as f:
        df.to_csv(f, header=False, index=False)
    os.replace(f.name, fn)


if "snakemake" not in globals():
    from _helpers import mock_snakemake
    snakemake = mock_snakemake(
        "build_biomass_potentials",
        scenario="stable",
    )

#configure_logging(snakemake)

# read via params so per-scenario overrides from scenarios.biomip.yaml are applied
scenario = snakemake.params.biomip_scenario.lower()

# Map config scenario -> scenario block in the BioMIP "Biomass Feedstock" sheet, in
# sheet order ("Current Trends", "High Biomass", "Low", "Frozen World")
scenario_blocks = ["bau", "high", "low", "frozen"]

if scenario not in scenario_blocks:
    raise ValueError(
        f"Unknown biomass scenario '{scenario}'. "
        f"Choose one of {scenario_blocks}"
    )

# -------------------------------
# Read BioMIP scenario data
# -------------------------------

# raw sheet, downloaded if not yet saved (delete the file to update it): scenario
# names in column B, feedstock in column L and potentials in PJ for the years in
# columns M to P
feedstock_fn = snakemake.params.biomip_feedstock
if not Path(feedstock_fn).exists():
    retrieve(feedstock_fn)
biomip = pd.read_csv(feedstock_fn, header=None, dtype=str)
SCENARIO_COL, FEEDSTOCK_COL, PJ_COLS = 1, 11, slice(12, 16)

header = biomip.index[biomip[PJ_COLS.start] == "2020"][0]
years = biomip.iloc[header, PJ_COLS].astype(int)
starts = biomip.index[(biomip.index > header) & biomip[SCENARIO_COL].notna()]
if len(starts) != len(scenario_blocks):
    raise ValueError(
        f"Expected {len(scenario_blocks)} scenarios in {feedstock_fn}, "
        f"found {biomip.loc[starts, SCENARIO_COL].tolist()}"
    )

i = scenario_blocks.index(scenario)
end = starts[i + 1] if i + 1 < len(starts) else len(biomip)
block = biomip.iloc[starts[i] : end].dropna(subset=[FEEDSTOCK_COL])
print(f"BioMIP biomass scenario '{scenario}': {biomip.at[starts[i], SCENARIO_COL]!r}")

subset = block.iloc[:, PJ_COLS].apply(pd.to_numeric)
subset.index = block[FEEDSTOCK_COL].str.strip().rename("PJ")
subset.columns = years.values
subset = subset.reindex(columns=[2020, 2025, 2030, 2035, 2040, 2045, 2050])
subset = subset.interpolate(axis=1) #Interpolate for intermediate years
print(subset)

# PJ -> MWh
PJ_TO_MWH = 1e6 / 3.6
subset *= PJ_TO_MWH

year = int(snakemake.wildcards.horizon)

# -------------------------------
# Read PyPSA-Eur biomass potentials
# -------------------------------

df = pd.read_csv(
    snakemake.input.biomass_potentials,
    index_col=0,
)

de = df.index.str.startswith("DE")

# Distribute national BioMIP potentials by the regional solid biomass potential.
# Use the raw ENSPRESO classes, since the "solid biomass" column above is scaled by
# share_sustainable_potential_available, which is 0 in early years (0/0 -> NaN,
# which PyPSA reads as an unlimited potential).
df_all = pd.read_csv(
    snakemake.input.biomass_potentials_all,
    index_col=0,
)
solid_biomass_classes = snakemake.params.biomass_classes["solid biomass"]
solid_biomass_all = df_all.loc[df.index[de], solid_biomass_classes].sum(axis=1)

weights = solid_biomass_all / solid_biomass_all.sum()

if weights.isna().any() or not abs(weights.sum() - 1) < 1e-6:
    raise ValueError(
        f"Invalid regional weights for BioMIP biomass potentials in {year}: {weights}"
    )

# -------------------------------
# BioMIP → PyPSA-Eur mapping
# -------------------------------

df.loc[de, "unsustainable bioliquids"] = (
    subset.loc["1st gen for biodiesel/bioethanol", year]
    * weights
)

#Add new column for oily residues
df["oily residues"] = 0.0
df.loc[de, "oily residues"] = (
    subset.loc["Oily", year]
    * weights
)


df.loc[de, "unsustainable biogas"] = (
    subset.loc["1st gen digestibles", year]
    * weights
)

df.loc[de, "unsustainable solid biomass"] = (
    subset.loc["SRC", year]
    * weights
)

df.loc[de, "biogas"] = (
    subset.loc["Digestible", year]
    * weights
)

df.loc[de, "solid biomass"] = (
    subset.loc["Woody", year]
    * weights
)

df.to_csv(snakemake.output[0])

