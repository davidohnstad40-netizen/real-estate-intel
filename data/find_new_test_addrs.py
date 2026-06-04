"""Find 10 fresh test addresses from MetroGIS — distinct streets, quality data."""
import sys, json, urllib.request, urllib.parse
from datetime import datetime, timezone
sys.path.insert(0, __file__.replace('data/find_new_test_addrs.py',''))

base = ('https://arcgis.metc.state.mn.us/data1/rest/services/'
        'parcels/Parcel_Points_2025/FeatureServer/0/query')

EXCLUDE = {
    'ASPEN LAKE','117TH','NAPLES','128TH','131ST','132ND',
    'FRAIZER','112TH','120TH','MIDWAY','123RD','MARMON',
    'STUTZ','FLANDERS','130TH','ZEST','GUADALCANAL','CORAL SEA',
    'ERSKIN','FILLMORE','124TH','121ST','125TH',
}

where = ("CTU_NAME='BLAINE' AND USECLASS1 LIKE '1a%' AND "
         "SALE_VALUE>=420000 AND SALE_DATE>=DATE '2023-01-01' AND EMV_TOTAL>=300000")
params = {
    'where': where,
    'outFields': 'ANUMBER,ST_NAME,ST_POS_TYP,ST_POS_DIR,EMV_TOTAL,SALE_VALUE,SALE_DATE,HOMESTEAD,FIN_SQ_FT,YEAR_BUILT',
    'f': 'json', 'outSR': '4326', 'returnGeometry': 'false',
    'resultRecordCount': 300, 'orderByFields': 'SALE_DATE DESC',
}
qs  = urllib.parse.urlencode(params)
req = urllib.request.Request(f'{base}?{qs}', headers={'User-Agent': 'REI/1.0'})
with urllib.request.urlopen(req, timeout=20) as r:
    data = json.loads(r.read())

features = data.get('features', [])
print(f"Raw results from MetroGIS: {len(features)}")

seen_streets = set()
results = []

for f in features:
    a   = f['attributes']
    st  = (a.get('ST_NAME','') or '').strip().upper()
    num = str(a.get('ANUMBER','') or '')

    # Skip excluded streets
    if any(ex in st for ex in EXCLUDE):
        continue
    # Skip streets we've already picked a property from
    if st in seen_streets:
        continue

    emv  = a.get('EMV_TOTAL') or 0
    sale = a.get('SALE_VALUE') or 0
    if sale == 0 or emv == 0:
        continue

    # Skip new construction EMV lag
    if emv / sale < 0.40:
        continue

    sale_ms = a.get('SALE_DATE')
    sale_yr = datetime.fromtimestamp(sale_ms/1000, tz=timezone.utc).year if sale_ms else None
    hmst    = (a.get('HOMESTEAD','') or '').strip()
    typ     = (a.get('ST_POS_TYP','') or '').strip().title()
    dire    = (a.get('ST_POS_DIR','') or '').strip()
    addr    = ' '.join(x for x in [num, st.title(), typ, dire] if x)

    seen_streets.add(st)
    results.append((addr, sale_yr, sale, emv, hmst))
    if len(results) >= 10:
        break

print()
print("10 FRESH TEST ADDRESSES:")
for addr, yr, sale, emv, hmst in results:
    yr_held = (2026 - yr) if yr else '?'
    ratio   = emv/sale if sale else 0
    print(f"  '{addr}', 'sold {yr}',  # emv=${emv:,.0f} sale=${sale:,.0f} "
          f"ratio={ratio:.2f} hmst={hmst} held={yr_held}yr")
