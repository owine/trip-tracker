"""One-shot script: download cities1000 from GeoNames and emit a filtered TSV.

Run once locally (not at build time):
    uv run python scripts/_make_cities_data.py

Produces src/trip_tracker/static/data/cities1000.tsv with 6 columns:
    name <TAB> asciiname <TAB> country_code <TAB> population <TAB> lat <TAB> lon
"""

from __future__ import annotations

import csv
import io
import urllib.request
import zipfile
from pathlib import Path

GEONAMES_URL = "https://download.geonames.org/export/dump/cities1000.zip"
OUT = Path(__file__).parent.parent / "src" / "trip_tracker" / "static" / "data" / "cities1000.tsv"


def main() -> None:
    print(f"Downloading {GEONAMES_URL} ...")
    with urllib.request.urlopen(GEONAMES_URL) as resp:
        zip_bytes = resp.read()
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        raw = zf.read("cities1000.txt").decode("utf-8")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with OUT.open("w", encoding="utf-8") as f:
        writer = csv.writer(f, delimiter="\t", lineterminator="\n")
        writer.writerow(["name", "asciiname", "country_code", "population", "lat", "lon"])
        for line in raw.splitlines():
            cols = line.split("\t")
            if len(cols) < 19:
                continue
            try:
                name = cols[1]
                asciiname = cols[2]
                lat = float(cols[4])
                lon = float(cols[5])
                country_code = cols[8]
                population = int(cols[14] or "0")
            except (ValueError, IndexError):
                continue
            writer.writerow([name, asciiname, country_code, population, lat, lon])
            n += 1
    print(f"Wrote {n} cities to {OUT} ({OUT.stat().st_size / 1024 / 1024:.1f} MB)")


if __name__ == "__main__":
    main()
