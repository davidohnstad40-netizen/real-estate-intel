"""Quick export of all 52 owners to BatchSkipTracing upload CSV."""
import pandas as pd, csv, re, os

XLSX = r"C:\Users\d.ohnstad\OneDrive - Veeam Software Corporation\Documents\House search.xlsx"
OUT  = os.path.join(os.path.dirname(__file__), "skip_trace_upload.csv")

def parse_name(s):
    s = str(s or "")
    s = re.sub(r"\b(Trust|LLC|LLP|Trustee|single name.*?)\b.*", "", s, flags=re.I).strip()
    s = re.sub(r"\(.*?\)", "", s).strip()
    if "&" in s: s = s.split("&")[0].strip()
    parts = s.split()
    return (parts[0], parts[-1]) if len(parts) >= 2 else (s, "")

df = pd.read_excel(XLSX, sheet_name="Realtor Summary", header=1, dtype=str)

n = 0
with open(OUT, "w", newline="", encoding="utf-8") as f:
    w = csv.writer(f)
    w.writerow(["FirstName","LastName","PropertyAddress","PropertyCity",
                "PropertyState","PropertyZip","KnockTier","Score","OwnerFull"])
    for _, row in df.iterrows():
        addr = str(row.iloc[1] or "").strip()
        if not addr or addr == "nan": continue
        owner = str(row.iloc[4] or "")
        tier  = str(row.iloc[18] or "TBD") if len(row) > 18 else "TBD"
        score = str(row.iloc[19] or "0")   if len(row) > 19 else "0"
        first, last = parse_name(owner)
        street = addr.split(",")[0].strip()
        w.writerow([first, last, street, "Blaine", "MN", "55449", tier, score, owner])
        n += 1

print(f"Exported {n} rows -> {OUT}")
print("Sort by KnockTier (T1/T2 first). Upload at: https://www.batchskiptracing.com")
print("Cost: ~$0.18/record. For 52 records = ~$9.36 total.")
