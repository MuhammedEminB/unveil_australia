#!/usr/bin/env python3
"""
Unveil - OSM Overpass hasat script'i (Avustralya bölgeleri, ilk hedef: Greater Brisbane).

Turkiye hasadindan cikan dersler bu script'e gomulu:
 1) Overpass, tek sorguda kategori basina >10 alt-etiket varsa bazen SESSIZCE bos
    sonuc doner (hata vermez, sadece [] gelir). Cozum: CHUNK = 5 alt-etiket/sorgu.
 2) 429 (Too Many Requests) / 504 (Gateway Timeout) -> exponential backoff ile
    tekrar dene, birden fazla ayna (mirror) sunucu arasinda gec.
 3) Degisken adi carpismasi (ör. 'e' hem exception hem eleman icin kullanilmis)
    gecmiste hataya yol acti -> burada tum degiskenler acik ve benzersiz adlandirildi.
 4) LLM/markdown kirletmesi bu script'te yok (dogrudan Overpass JSON parse ediliyor),
    yine de savunma amacli JSON.loads etrafinda try/except var.

Kullanim:
    python3 unveil_harvest.py --region greater_brisbane --regions-file regions.json --out raw_greater_brisbane.json

Cikti formati (ham, kirpma/tekillestirme asamasindan ONCE):
    {
      "region": "Greater Brisbane",
      "bbox": {...},
      "raw_count": N,
      "items": [ {"name":..., "cat":..., "lat":..., "lon":..., "tags":{...}}, ... ]
    }
"""
import argparse
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

OVERPASS_MIRRORS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.openstreetmap.ru/api/interpreter",
]

CHUNK_SIZE = 5          # bir Overpass sorgusundaki en fazla alt-etiket sayisi (Turkiye dersi)
MAX_RETRY_PER_CHUNK = 6
BASE_BACKOFF_SECONDS = 5
REQUEST_TIMEOUT = 180

# 20 kategori x OSM etiket eslesmeleri. Her giris "k=v" ya da "k=v;k2=v2" (bilesik filtre).
# Bazi kategoriler (castles_forts, caravanserai_bridges, tombs_necropolis, underground_rockcut)
# Turkiye'ye ozgu oldugu icin Avustralya'da az/sifir sonuc verebilir - bu BEKLENEN bir durum,
# hata degildir (Turkiye'deki underground_rockcut ile ayni durum).
CATEGORY_TAGS = {
    "parks_gardens": ["leisure=park", "leisure=garden"],
    # + reef_marine_life ve lighthouses_coastal (Avustralya'ya ozgu, Turkiye'de yoktu)
    # en mantikli ana baslik altina eklendi: ikisi de kiyi/deniz temali -> beaches_coast
    "beaches_coast": ["natural=beach", "leisure=beach_resort", "natural=reef", "man_made=lighthouse"],
    "mountains_viewpoints": ["natural=peak", "tourism=viewpoint", "natural=saddle"],
    "water_wetlands": ["natural=water", "waterway=waterfall", "natural=wetland", "natural=spring"],
    "caves_karst": ["natural=cave_entrance"],
    "geological_wonders": ["natural=rock", "natural=stone"],
    "forests_nature_reserves": ["boundary=national_park", "leisure=nature_reserve", "boundary=protected_area"],
    "valleys_canyons": ["natural=valley", "natural=cliff", "natural=gorge"],
    "museums_heritage": ["tourism=museum", "historic=heritage"],
    "castles_forts": ["historic=castle", "historic=fort", "historic=fortification"],
    "archaeological_ruins": ["historic=archaeological_site", "historic=ruins"],
    "tombs_necropolis": ["historic=tomb", "historic=memorial", "amenity=grave_yard"],
    "religious_heritage": ["amenity=place_of_worship", "historic=wayside_shrine"],
    "caravanserai_bridges": ["historic=caravanserai", "man_made=bridge;historic=yes"],
    "traditional_architecture": ["historic=building", "building=heritage"],
    "rural_heritage": ["historic=farm", "man_made=windmill", "historic=mill"],
    "underground_rockcut": ["historic=rock_cut_tomb"],
    "trails_hiking": ["route=hiking"],
    # + wildlife_sanctuaries (canli yaban hayati gozlem, Avustralya'ya ozgu) -> mevcut
    # wildlife_heritage_trees baslig altina eklendi, zaten "wildlife" temasini tasiyan tek kategori bu
    "wildlife_heritage_trees": ["natural=tree;historic=yes", "leisure=wildlife_hide", "tourism=zoo"],
    "landmarks_panorama": ["tourism=attraction", "man_made=tower;tourism=viewpoint"],
}


def chunked(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def tagspec_to_filters(tagspec):
    """'k=v;k2=v2' -> ['k'='v']['k2'='v2'] Overpass bracket dizisi."""
    parts = tagspec.split(";")
    out = []
    for part in parts:
        k, v = part.split("=", 1)
        if v == "*":
            out.append("[\"%s\"]" % k)
        else:
            out.append("[\"%s\"=\"%s\"]" % (k, v))
    return "".join(out)


def build_query(bbox, tagspecs):
    """bbox = {south,west,north,east}. tagspecs = kategori icin en fazla CHUNK_SIZE etiket."""
    bb = "%s,%s,%s,%s" % (bbox["south"], bbox["west"], bbox["north"], bbox["east"])
    lines = ["[out:json][timeout:120];", "("]
    for tagspec in tagspecs:
        filt = tagspec_to_filters(tagspec)
        lines.append("  node%s(%s);" % (filt, bb))
        lines.append("  way%s(%s);" % (filt, bb))
    lines.append(");")
    lines.append("out center tags;")
    return "\n".join(lines)


def fetch_overpass(query_text):
    """Ayna sunucular + retry/backoff ile Overpass sorgusu calistirir."""
    last_err = None
    for attempt in range(MAX_RETRY_PER_CHUNK):
        mirror_url = OVERPASS_MIRRORS[attempt % len(OVERPASS_MIRRORS)]
        try:
            req = urllib.request.Request(
                mirror_url,
                data=("data=" + urllib.parse.quote(query_text)).encode("utf-8"),
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
                raw_bytes = resp.read()
                return json.loads(raw_bytes.decode("utf-8"))
        except urllib.error.HTTPError as http_err:
            last_err = http_err
            if http_err.code in (429, 504, 502, 503):
                backoff = BASE_BACKOFF_SECONDS * (2 ** attempt)
                print("  [uyari] HTTP %s, %ss bekleyip tekrar denenecek (deneme %d/%d, ayna=%s)"
                      % (http_err.code, backoff, attempt + 1, MAX_RETRY_PER_CHUNK, mirror_url), file=sys.stderr)
                time.sleep(backoff)
                continue
            raise
        except (urllib.error.URLError, json.JSONDecodeError) as generic_err:
            last_err = generic_err
            backoff = BASE_BACKOFF_SECONDS * (2 ** attempt)
            print("  [uyari] baglanti/parse hatasi (%s), %ss bekleyip tekrar denenecek" % (generic_err, backoff),
                  file=sys.stderr)
            time.sleep(backoff)
            continue
    raise RuntimeError("Overpass sorgusu %d denemede basarisiz oldu: %s" % (MAX_RETRY_PER_CHUNK, last_err))


def element_center(element):
    if element.get("type") == "node":
        return element.get("lat"), element.get("lon")
    center = element.get("center")
    if center:
        return center.get("lat"), center.get("lon")
    return None, None


def score_element(tags):
    score = 50
    if tags.get("wikidata"):
        score += 20
    if tags.get("wikipedia"):
        score += 15
    if tags.get("website") or tags.get("contact:website"):
        score += 5
    if tags.get("name:en"):
        score += 5
    return min(score, 100)


def harvest_region(region_name, bbox):
    items = []
    seen_ids = set()
    total_categories = len(CATEGORY_TAGS)
    for idx, (cat, tagspecs) in enumerate(CATEGORY_TAGS.items(), start=1):
        print("[%d/%d] kategori: %s" % (idx, total_categories, cat), file=sys.stderr)
        cat_count = 0
        for chunk in chunked(tagspecs, CHUNK_SIZE):
            query_text = build_query(bbox, chunk)
            try:
                result = fetch_overpass(query_text)
            except RuntimeError as fatal_err:
                print("  [HATA] kategori '%s' chunk atlandi: %s" % (cat, fatal_err), file=sys.stderr)
                continue
            for element in result.get("elements", []):
                uid = "%s/%s" % (element.get("type"), element.get("id"))
                if uid in seen_ids:
                    continue
                tags = element.get("tags", {})
                name = tags.get("name") or tags.get("name:en")
                if not name:
                    continue
                lat, lon = element_center(element)
                if lat is None or lon is None:
                    continue
                seen_ids.add(uid)
                items.append({
                    "name": name,
                    "cat": cat,
                    "lat": lat,
                    "lon": lon,
                    "score": score_element(tags),
                    "osm": uid,
                })
                cat_count += 1
        print("  -> %d yer bulundu" % cat_count, file=sys.stderr)
        time.sleep(1)  # Overpass'i yormamak icin kategoriler arasi kucuk bekleme
    return items


def main():
    parser = argparse.ArgumentParser(description="Unveil Overpass hasat script'i")
    parser.add_argument("--region", required=True, help="regions.json icindeki bolge anahtari")
    parser.add_argument("--regions-file", default="regions.json")
    parser.add_argument("--out", required=True, help="cikti ham JSON dosyasi")
    args = parser.parse_args()

    with open(args.regions_file, "r", encoding="utf-8") as f:
        regions = json.load(f)

    if args.region not in regions:
        print("HATA: '%s' regions.json icinde yok. Mevcut: %s" % (args.region, list(regions.keys())),
              file=sys.stderr)
        sys.exit(1)

    region_def = regions[args.region]
    bbox = region_def["bbox"]
    display_name = region_def.get("name", args.region)

    print("Hasat basliyor: %s  bbox=%s" % (display_name, bbox), file=sys.stderr)
    items = harvest_region(display_name, bbox)

    out_data = {
        "region": display_name,
        "region_key": args.region,
        "bbox": bbox,
        "raw_count": len(items),
        "items": items,
    }
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out_data, f, ensure_ascii=False)

    print("Bitti: %d ham yer -> %s" % (len(items), args.out), file=sys.stderr)


if __name__ == "__main__":
    main()
