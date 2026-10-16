import sys
import time

import requests

url = "https://download.geofabrik.de/asia/india/western-zone-latest.osm.pbf"
out = r"C:\Users\Administrator\Desktop\reserachPaper\data\western-zone-latest.osm.pbf"

t = time.time()
with requests.get(url, stream=True, timeout=60, headers={"User-Agent": "pune-traffic-demo/1.0"}) as r:
    r.raise_for_status()
    total = int(r.headers.get("Content-Length", 0))
    done = 0
    with open(out, "wb") as f:
        for chunk in r.iter_content(chunk_size=1024 * 1024):
            f.write(chunk)
            done += len(chunk)
            if total:
                pct = done / total * 100
                print(f"\r{done/1e6:.1f} MB / {total/1e6:.1f} MB ({pct:.1f}%)", end="")
    print()
print(f"done in {time.time()-t:.1f}s")
