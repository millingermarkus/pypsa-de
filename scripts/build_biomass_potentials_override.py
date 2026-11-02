import pandas as pd
from _helpers import configure_logging

if "snakemake" not in globals():
    from _helpers import mock_snakemake
    snakemake = mock_snakemake(
        "build_biomass_potentials",
        scenario="stable",
    )

#configure_logging(snakemake)

# read via params so per-scenario overrides from scenarios.biomip.yaml are applied
scenario = snakemake.params.biomip_scenario.lower()

# Map config scenario -> row block in BioMIP sheet
scenario_rows = {
    "frozen": slice(0, 9),
    "high": slice(10, 19),
    "low": slice(20, 29),
    "bau": slice(30, 39),
}

if scenario not in scenario_rows:
    raise ValueError(
        f"Unknown biomass scenario '{scenario}'. "
        f"Choose one of {list(scenario_rows)}"
    )

# -------------------------------
# Read BioMIP scenario data
# -------------------------------

biomip = pd.read_csv(
    snakemake.input.biomip,
    header=0,
)

subset = biomip.iloc[scenario_rows[scenario], 1:6].copy()

subset.set_index("PJ", inplace=True)
subset.columns = subset.columns.astype(int)
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

