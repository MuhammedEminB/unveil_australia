#!/usr/bin/env python3
"""
Unveil - Bolge kirpma + tekillestirme script'i (Turkiye Asama 2 ile ayni mantik).

unveil_harvest.py'nin urettigi ham JSON'u alir, gercek bolge sinirinin (GeoJSON
MultiPolygon/Polygon) DISINDA kalan noktalari atar, birbirine cok yakin ayni-kategori
tekrarlarini (OSM'de node+way ayni yeri iki kez donduren durumlar) tekillestirir ve
uygulamanin bekledigi paket formatinda ({"il":..,"count":..,"d":[[name,cat,lat,lon,score],...]})
cikti uretir.

Kullanim:
    python3 clip_region.py --raw raw_greater_brisbane.json \
        --boundary boundaries/greater_brisbane_boundary.geojson \
        --out packs/greater_brisbane.json

Not: shapely/geopandas YOK varsayimiyla yazildi (Turkiye asamasindaki sandbox kisitlamasi
gecmiste de gecerliydi) - saf Python ray-casting point-in-polygon kullanilir.
"""
import argparse
import json
import math


def point_in_ring(lon, lat, ring):
    inside = False
    n = len(ring)
    j = n - 1
    for i in range(n):
        xi, yi = ring[i]
        xj, yj = ring[j]
        if ((yi > lat) != (yj > lat)) and (
            lon < (xj - xi) * (lat - yi) / (yj - yi + 1e-15) + xi
        ):
            inside = not inside
        j = i
    return inside


def point_in_multipolygon(lon, lat, multipoly_coords):
    """multipoly_coords: GeoJSON MultiPolygon 'coordinates' -> [ [ [ [lon,lat],... ] , [hole...] ], ... ]"""
    for polygon in multipoly_coords:
        exterior = polygon[0]
        if not point_in_ring(lon, lat, exterior):
            continue
        in_hole = False
        for hole in polygon[1:]:
            if point_in_ring(lon, lat, hole):
                in_hole = True
                break
        if not in_hole:
            return True
    return False


def load_boundary(path):
    with open(path, "r", encoding="utf-8") as f:
        gj = json.load(f)
    feature = gj["features"][0]
    geom = feature["geometry"]
    if geom["type"] == "MultiPolygon":
        return geom["coordinates"]
    elif geom["type"] == "Polygon":
        return [geom["coordinates"]]
    raise ValueError("Desteklenmeyen geometry tipi: %s" % geom["type"])


def haversine_m(lat1, lon1, lat2, lon2):
    R = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


DEDUP_RADIUS_M = 40  # bu mesafenin altinda + ayni kategori + benzer isim -> ayni yer sayilir


def normalize_name(name):
    return "".join(ch.lower() for ch in name if ch.isalnum())


def dedup_items(items):
    """Basit uzamsal + isim tabanli tekillestirme (grid-bucket ile O(n) yakinlik)."""
    bucket_size_deg = 0.0006  # ~60-70m enlem/boylamda
    buckets = {}
    kept = []
    dropped = 0

    def bucket_key(lat, lon):
        return (round(lat / bucket_size_deg), round(lon / bucket_size_deg))

    for item in items:
        lat, lon = item["lat"], item["lon"]
        bk = bucket_key(lat, lon)
        candidate_keys = [
            (bk[0] + dx, bk[1] + dy) for dx in (-1, 0, 1) for dy in (-1, 0, 1)
        ]
        norm_name = normalize_name(item["name"])
        is_dup = False
        for ck in candidate_keys:
            for other in buckets.get(ck, []):
                if other["cat"] != item["cat"]:
                    continue
                dist = haversine_m(lat, lon, other["lat"], other["lon"])
                if dist <= DEDUP_RADIUS_M and normalize_name(other["name"]) == norm_name:
                    is_dup = True
                    if item.get("score", 0) > other.get("score", 0):
                        other.update(item)
                    break
            if is_dup:
                break
        if is_dup:
            dropped += 1
            continue
        buckets.setdefault(bk, []).append(item)
        kept.append(item)

    return kept, dropped


def main():
    parser = argparse.ArgumentParser(description="Unveil bolge kirpma + tekillestirme")
    parser.add_argument("--raw", required=True)
    parser.add_argument("--boundary", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--region-label", default=None, help="cikti 'il' alani icin isim (yoksa raw'daki region kullanilir)")
    args = parser.parse_args()

    with open(args.raw, "r", encoding="utf-8") as f:
        raw = json.load(f)

    boundary_coords = load_boundary(args.boundary)

    inside_items = []
    outside_count = 0
    for item in raw["items"]:
        if point_in_multipolygon(item["lon"], item["lat"], boundary_coords):
            inside_items.append(item)
        else:
            outside_count += 1

    deduped, dropped_count = dedup_items(inside_items)

    label = args.region_label or raw.get("region", "Unknown")
    pack_data = [
        [it["name"], it["cat"], it["lat"], it["lon"], it.get("score", 50)]
        for it in deduped
    ]

    out_pack = {
        "il": label,
        "count": len(pack_data),
        "d": pack_data,
    }
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out_pack, f, ensure_ascii=False)

    print(
        "Ham: %d | sinir disi atilan: %d | tekrar (dedup) atilan: %d | final: %d -> %s"
        % (raw["raw_count"], outside_count, dropped_count, len(pack_data), args.out)
    )


if __name__ == "__main__":
    main()
