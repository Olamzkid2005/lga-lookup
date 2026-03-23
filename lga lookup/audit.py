"""
Audit script - runs entirely offline against the real Excel data.
Identifies exactly what the current logic would miss and why.
"""
import sys, types, argparse, unittest.mock as mock, os, re
import pandas as pd

os.chdir(os.path.dirname(os.path.abspath(__file__)))

# ── Load lookup functions ─────────────────────────────────────────────
_real_parse = argparse.ArgumentParser.parse_args
argparse.ArgumentParser.parse_args = lambda self, *a, **k: argparse.Namespace(
    input="LGA Confirm.xlsx", output=None, sheet=0)
sys.modules["requests"] = mock.MagicMock()

with open("lookup.py", encoding="utf-8") as f:
    source = f.read()
cutoff = source.find("# ── Load data")
mod = types.ModuleType("lookup")
exec(compile(source[:cutoff], "lookup.py", "exec"), mod.__dict__)

kw_lookup        = mod.keyword_lga_lookup
infer_state      = mod.infer_state_from_address
normalise_state  = mod.normalise_state
clean_address    = mod.clean_address
build_variants   = mod.build_query_variants

# ── Load data ─────────────────────────────────────────────────────────
df = pd.read_excel("LGA Confirm.xlsx")
df.columns = df.columns.str.strip()
df.rename(columns={"STATE NAME": "STATE"}, inplace=True)
df["ADDRESS"] = df["ADDRESS"].astype(str).str.strip()
df["STATE"]   = df["STATE"].apply(lambda v: normalise_state(str(v)))

# ── 1. Keyword coverage ───────────────────────────────────────────────
df["kw_lga"]       = df["ADDRESS"].apply(lambda a: kw_lookup(str(a)) if pd.notna(a) else "")
df["inferred_state"] = df["ADDRESS"].apply(lambda a: infer_state(str(a)) if pd.notna(a) else "")
df["state_mismatch"] = (df["inferred_state"] != "") & \
                       (df["inferred_state"] != df["STATE"])

kw_hit  = df["kw_lga"] != ""
kw_miss = ~kw_hit

print("=" * 60)
print("KEYWORD COVERAGE")
print(f"  Hit  : {kw_hit.sum():,} / {len(df):,} ({kw_hit.sum()/len(df)*100:.1f}%)")
print(f"  Miss : {kw_miss.sum():,} ({kw_miss.sum()/len(df)*100:.1f}%)")
print()

# ── 2. State mismatch analysis ────────────────────────────────────────
print("STATE MISMATCH (address implies different state than STATE col)")
mismatch_df = df[df["state_mismatch"]][["ADDRESS","STATE","inferred_state"]].drop_duplicates()
print(f"  {len(mismatch_df)} unique mismatched addresses")
print(mismatch_df.head(15).to_string())
print()

# ── 3. Miss breakdown by state ────────────────────────────────────────
print("KEYWORD MISSES BY STATE (unique addresses)")
miss_unique = df[kw_miss][["ADDRESS","STATE"]].drop_duplicates()
by_state = miss_unique.groupby("STATE").size().sort_values(ascending=False)
print(by_state.to_string())
print()

# ── 4. Sample misses per state ────────────────────────────────────────
print("SAMPLE MISSES PER STATE (top 5 each)")
for state, grp in miss_unique.groupby("STATE"):
    print(f"\n  [{state}]")
    for addr in grp["ADDRESS"].head(5):
        print(f"    {addr}")

# ── 5. Cleaned address quality check ─────────────────────────────────
print()
print("=" * 60)
print("CLEAN ADDRESS QUALITY (sample of what gets sent to Nominatim)")
sample = df[["ADDRESS","STATE"]].drop_duplicates().head(20)
for _, row in sample.iterrows():
    cleaned = clean_address(row["ADDRESS"])
    variants = build_variants(row["ADDRESS"], row["STATE"])
    print(f"  RAW     : {row['ADDRESS']}")
    print(f"  CLEANED : {cleaned}")
    print(f"  VARIANTS: {variants[:3]}")
    print()
