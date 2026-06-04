"""Check what cities/counties are actually available in MetroGIS dataset."""
import urllib.request, urllib.parse, json

base = 'https://arcgis.metc.state.mn.us/data1/rest/services/parcels/Parcel_Points_2025/FeatureServer/0/query'
params = {
    'where': 'EMV_TOTAL > 300000',
    'outFields': 'CTU_NAME,CO_NAME,CO_CODE',
    'f': 'json',
    'resultRecordCount': 100,
    'orderByFields': 'CTU_NAME',
}
qs = urllib.parse.urlencode(params)
req = urllib.request.Request(f'{base}?{qs}', headers={'User-Agent':'REI/1.0'})
with urllib.request.urlopen(req, timeout=15) as r:
    data = json.loads(r.read())

cities_seen = {}
for f in data.get('features', []):
    a = f['attributes']
    city = a.get('CTU_NAME', '?')
    co   = a.get('CO_NAME', '?')
    key  = f"{city} ({co})"
    cities_seen[key] = cities_seen.get(key, 0) + 1

print(f"Sample of {len(data.get('features',[]))} records:")
print("Cities found:")
for c, cnt in sorted(cities_seen.items()):
    print(f"  {c}: {cnt} records")

# Now test specific cities
test_cities = ["Maple Grove", "Eden Prairie", "Woodbury", "Plymouth", "Eagan"]
print("\nTesting specific city queries:")
for city in test_cities:
    where = f"CTU_NAME='{city.upper()}' AND EMV_TOTAL > 300000"
    p2 = {'where': where, 'outFields': 'CTU_NAME', 'f': 'json', 'resultRecordCount': 5}
    qs2 = urllib.parse.urlencode(p2)
    req2 = urllib.request.Request(f'{base}?{qs2}', headers={'User-Agent':'REI/1.0'})
    with urllib.request.urlopen(req2, timeout=10) as r2:
        d2 = json.loads(r2.read())
    print(f"  '{city.upper()}': {len(d2.get('features',[]))} results")
