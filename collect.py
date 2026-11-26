"""에어비앤비 숙소 요금 × 국토부 전월세 실거래가 수집.

실행:
    python collect.py                          설정된 지역 중 아직 없는 곳만 수집
    python collect.py --add "부산광역시 기장군"  시군구를 추가하고 수집 (여러 개 가능)
    python collect.py --refresh                모든 지역을 새로 수집

출력:
    data/areas/<지역>.json   지역별 에어비앤비 검색 결과 (지역 단위 캐시)
    data/molit/*.csv         국토부 실거래가 공개시스템 전월세 CSV (시도별, 최근 6개월)
    data/results.json        지역별 숙소·월세 계약 (report.html이 읽는 데이터)

API 키·로그인 없이 공개 검색 화면, 국토부 공식 CSV 다운로드, OSM 행정구역 경계 조회만 사용한다.
"""
import base64
import csv
import datetime as dt
import http.cookiejar
import io
import json
import math
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"
MOLIT_DIR = DATA / "molit"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")
TODAY = dt.date.today()

# ── 서울: 동 단위. slug는 에어비앤비가 인식하는 영문 지명(한글 검색어는 지역으로 인식되지 않는다),
#    gu·dongs는 월세 비교에 쓸 법정동(앞부분 일치)
SEOUL_DONGS = [
    {"name": "연남동", "slug": "Yeonnam-dong--Mapo-gu--Seoul", "gu": "마포구", "dongs": ["연남동"]},
    {"name": "서교동(홍대)", "slug": "Seogyo-dong--Mapo-gu--Seoul", "gu": "마포구", "dongs": ["서교동", "동교동"]},
    {"name": "망원동", "slug": "Mangwon-dong--Mapo-gu--Seoul", "gu": "마포구", "dongs": ["망원동"]},
    {"name": "합정동", "slug": "Hapjeong-dong--Mapo-gu--Seoul", "gu": "마포구", "dongs": ["합정동"]},
    {"name": "창천동(신촌)", "slug": "Changcheon-dong--Seodaemun-gu--Seoul", "gu": "서대문구", "dongs": ["창천동"]},
    {"name": "이태원동", "slug": "Itaewon-dong--Yongsan-gu--Seoul", "gu": "용산구", "dongs": ["이태원동"]},
    {"name": "후암동(서울역)", "slug": "Huam-dong--Yongsan-gu--Seoul", "gu": "용산구", "dongs": ["후암동"]},
    {"name": "명동·충무로", "slug": "Myeong-dong--Jung-gu--Seoul", "gu": "중구",
     "dongs": ["회현동", "남산동", "필동", "충무로", "예장동", "남창동", "명동"]},
    {"name": "광희동(동대문)", "slug": "Gwanghui-dong--Jung-gu--Seoul", "gu": "중구", "dongs": ["신당동", "광희동"]},
    {"name": "성수동", "slug": "Seongsu-dong--Seongdong-gu--Seoul", "gu": "성동구", "dongs": ["성수동"]},
    {"name": "화양동(건대)", "slug": "Hwayang-dong--Gwangjin-gu--Seoul", "gu": "광진구", "dongs": ["화양동"]},
    {"name": "역삼동(강남)", "slug": "Yeoksam-dong--Gangnam-gu--Seoul", "gu": "강남구", "dongs": ["역삼동"]},
    {"name": "송파동(잠실)", "slug": "Songpa-dong--Songpa-gu--Seoul", "gu": "송파구", "dongs": ["송파동", "방이동"]},
]
# ── 서울 외: 시군구 단위. (시도, 시군구) — 시도 이름은 국토부 실거래가 공개시스템 표기를 따른다.
#    여기에 없는 곳은 python collect.py --add "시도 시군구" 로 추가한다(areas_extra.json에 저장).
SIGUNGU = [
    ("부산광역시", "해운대구"), ("부산광역시", "수영구"), ("부산광역시", "중구"), ("부산광역시", "부산진구"),
    ("인천광역시", "연수구"), ("대구광역시", "중구"),
    ("경기도", "수원시 팔달구"), ("경기도", "고양시 일산동구"), ("경기도", "화성시"),
    ("강원특별자치도", "강릉시"), ("강원특별자치도", "속초시"), ("강원특별자치도", "춘천시"), ("강원특별자치도", "양양군"),
    ("경상북도", "경주시"), ("경상북도", "안동시"), ("경상남도", "통영시"), ("경상남도", "거제시"),
    ("전북특별자치도", "전주시 완산구"),
    ("전남광주통합특별시", "여수시"), ("전남광주통합특별시", "순천시"),
    ("제주특별자치도", "제주시"), ("제주특별자치도", "서귀포시"),
]
EXTRA_FILE = HERE / "areas_extra.json"
AREA_DIR = DATA / "areas"
SIDO_SHORT = {"경상북도": "경북", "경상남도": "경남", "충청북도": "충북", "충청남도": "충남",
              "전남광주통합특별시": "전남광주"}
BOX_LAT, BOX_LNG = 0.0063, 0.0080            # 동 단위: 중심에서 사방 약 700m (1.4km 정사각형) 안의 숙소만 본다
PAGES = 3                                    # 검색 결과 쪽수 (1쪽 = 숙소 18개)
MAX_PAGES, TARGET = 8, 45                    # 시군구 단위: 경계 안 숙소가 45곳 모일 때까지 최대 8쪽


def next_weekday(d, wd):
    return d + dt.timedelta(days=(wd - d.weekday()) % 7)


_base = TODAY + dt.timedelta(days=42)        # 약 6주 뒤: 예약이 아직 덜 찬 시점의 정가에 가깝다
WINDOWS = [
    ("weekday", next_weekday(_base, 1), 2),  # 화~목 2박
    ("weekend", next_weekday(_base, 4), 2),  # 금~일 2박
]
RENT_FROM = TODAY - dt.timedelta(days=183)
RENT_TO = TODAY


def opener(referer=None):
    cj = http.cookiejar.CookieJar()
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
    op.addheaders = [("User-Agent", UA), ("Accept-Language", "ko-KR,ko;q=0.9")]
    if referer:
        op.addheaders.append(("Referer", referer))
    return op


# ---------------------------------------------------------------- 에어비앤비
def won(s):
    d = re.sub(r"[^0-9]", "", s or "")
    return int(d) if d else None


def parse_search(html):
    m = re.search(r'<script id="data-deferred-state-0"[^>]*>(.*?)</script>', html, re.S)
    if not m:
        return None
    pres = json.loads(m.group(1))["niobeClientData"][0][1]["data"]["presentation"]["staysSearch"]
    res = pres["results"]
    poi = ((pres.get("mapResults") or {}).get("mapMetadata") or {}).get("queriedPoi") or {}
    out = []
    for s in res.get("searchResults") or []:
        if s.get("__typename") != "StaySearchResult":
            continue
        dsl = s.get("demandStayListing") or {}
        try:
            lid = base64.b64decode(dsl["id"]).decode().split(":")[-1]
        except Exception:
            continue
        line = (s.get("structuredDisplayPrice") or {}).get("primaryLine") or {}
        total = won(line.get("discountedPrice") or line.get("price"))
        rating, reviews = None, 0
        rm = re.match(r"([\d.]+)\s*\((\d[\d,]*)\)", s.get("avgRatingLocalized") or "")
        if rm:
            rating, reviews = float(rm.group(1)), int(rm.group(2).replace(",", ""))
        info = [x.get("body") or "" for x in (s.get("structuredContent") or {}).get("primaryLine") or []]
        bed = next((int(re.search(r"\d+", x).group()) for x in info if x.startswith("침실")), None)
        coord = (dsl.get("location") or {}).get("coordinate") or {}
        out.append({
            "id": lid,
            "title": s.get("title") or "",
            "name": ((dsl.get("description") or {}).get("name") or {}).get(
                "localizedStringWithTranslationPreference") or "",
            "lat": coord.get("latitude"), "lng": coord.get("longitude"),
            "rating": rating, "reviews": reviews,
            "is_new": (s.get("avgRatingLocalized") or "") == "신규",
            "guest_fav": any((b.get("text") or "") == "게스트 선호" for b in s.get("badges") or []),
            "bedrooms": bed, "info": " · ".join(info),
            "total": total,
        })
    cursors = (res.get("paginationInfo") or {}).get("pageCursors") or []
    center = poi.get("coordinate") or {}
    return {"listings": out, "cursors": cursors, "poi": poi.get("name"),
            "center": [center.get("latitude"), center.get("longitude")]}


def search(op, slug, checkin, nights, extra=None):
    q = {"checkin": checkin.isoformat(),
         "checkout": (checkin + dt.timedelta(days=nights)).isoformat(),
         "adults": 2, "currency": "KRW", "room_types[]": "Entire home/apt"}
    q.update(extra or {})
    url = f"https://www.airbnb.co.kr/s/{urllib.parse.quote(slug)}/homes?" + urllib.parse.urlencode(q)
    return parse_search(op.open(url, timeout=60).read().decode("utf-8", errors="replace"))


def all_areas():
    out = [dict(a, key=a["name"], unit="동", sido="서울특별시") for a in SEOUL_DONGS]
    extra = json.loads(EXTRA_FILE.read_text(encoding="utf-8")) if EXTRA_FILE.exists() else []
    seen = set()
    for sido, sgg in list(SIGUNGU) + [tuple(x) for x in extra]:
        if (sido, sgg) in seen:
            continue
        seen.add((sido, sgg))
        out.append({"key": f"{sido} {sgg}", "name": f"{SIDO_SHORT.get(sido, sido[:2])} {sgg}",
                    "unit": "시군구", "sido": sido, "gu": sgg, "dongs": []})
    return out


def cache_path(a):
    return AREA_DIR / (re.sub(r"[^0-9A-Za-z가-힣·()]+", "_", a["key"]) + ".json")


def boundary(sido, sgg):
    """OSM(Nominatim)에서 시군구 경계를 얻는다. 반환: (중심, [남,북,서,동], 다각형 목록, 표시명)"""
    parts = sgg.split(" ")
    queries = [f"{parts[-1]}, {parts[0]}" if len(parts) > 1 else f"{sgg}, {sido}", sgg]
    for q in queries:
        url = "https://nominatim.openstreetmap.org/search?" + urllib.parse.urlencode({
            "q": q, "format": "jsonv2", "countrycodes": "kr", "limit": 5, "accept-language": "ko",
            "polygon_geojson": 1, "polygon_threshold": 0.0005})
        req = urllib.request.Request(url, headers={"User-Agent": "lodging-area-report/1.0"})
        rs = json.loads(urllib.request.urlopen(req, timeout=60).read())
        time.sleep(1.1)                                   # Nominatim 이용 정책: 초당 1회 이하
        for r in rs:
            g = r.get("geojson") or {}
            if r.get("category") == "boundary" and g.get("type") in ("Polygon", "MultiPolygon"):
                polys = [g["coordinates"]] if g["type"] == "Polygon" else g["coordinates"]
                s_, n_, w_, e_ = map(float, r["boundingbox"])
                return [float(r["lat"]), float(r["lon"])], [s_, n_, w_, e_], polys, r.get("display_name")
    return None


def inside(lat, lng, polys):
    """점이 다각형(구멍 포함) 안에 있는지 — 반직선 교차 판정."""
    def ring(pts):
        hit = False
        for (x1, y1), (x2, y2) in zip(pts, pts[1:] + pts[:1]):
            if (y1 > lat) != (y2 > lat) and lng < (x2 - x1) * (lat - y1) / (y2 - y1) + x1:
                hit = not hit
        return hit
    return any(ring(p[0]) and not any(ring(h) for h in p[1:]) for p in polys)


def collect_area(op, a):
    """한 지역의 검색 결과를 모아 캐시 파일로 저장한다."""
    polys = None
    if a["unit"] == "동":
        first = search(op, a["slug"], WINDOWS[0][1], WINDOWS[0][2])
        time.sleep(1.5)
        if not first or None in first["center"]:
            print(f"  [건너뜀] {a['name']}: 에어비앤비가 장소를 인식하지 못함")
            return None
        center, place = first["center"], first["poi"]
        bbox = [center[0] - BOX_LAT, center[0] + BOX_LAT, center[1] - BOX_LNG, center[1] + BOX_LNG]
    else:
        b = boundary(a["sido"], a["gu"])
        if not b:
            print(f"  [건너뜀] {a['name']}: 행정구역 경계를 찾지 못함 (이름을 확인하세요)")
            return None
        center, bbox, polys, place = b
    box = {"ne_lat": bbox[1], "ne_lng": bbox[3], "sw_lat": bbox[0], "sw_lng": bbox[2],
           "search_by_map": "true", "zoom": 13, "zoom_level": 13}
    raw, counts = [], []
    for label, checkin, nights in WINDOWS:
        cursor, got = None, 0
        for page in range(PAGES if not polys else MAX_PAGES):
            if page >= PAGES and got >= TARGET:           # 시군구: 경계 안 숙소가 충분히 모이면 멈춘다
                break
            try:
                parsed = search(op, "Seoul", checkin, nights, dict(box, **({"cursor": cursor} if cursor else {})))
            except Exception as e:
                print(f"  [실패] {a['name']} {label} {page + 1}쪽: {e}")
                break
            time.sleep(1.5)
            if not parsed:
                break
            ls = parsed["listings"]
            if polys:                                     # 경계 밖(이웃 시군구) 숙소는 버린다
                ls = [l for l in ls if l["lat"] is not None and inside(l["lat"], l["lng"], polys)]
            for l in ls:
                l.update(window=label, nights=nights)
            raw += ls
            got += len(ls)
            if not parsed["listings"] or page + 1 >= len(parsed["cursors"]):
                break
            cursor = parsed["cursors"][page + 1]
        counts.append(f"{label} {got}건")
    AREA_DIR.mkdir(parents=True, exist_ok=True)
    cache = {"key": a["key"], "place": place, "center": center, "bbox": bbox, "raw": raw,
             "collected_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
             "windows": [{"label": w[0], "checkin": w[1].isoformat(), "nights": w[2]} for w in WINDOWS]}
    cache_path(a).write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
    print(f"  {a['name']} (인식된 장소: {place}): {' · '.join(counts)}")
    return cache


def fetch_airbnb(areas, refresh=False):
    op = opener()
    raw = []
    for a in areas:
        f = cache_path(a)
        cache = None if refresh or not f.exists() else json.loads(f.read_text(encoding="utf-8"))
        if cache is None:
            try:
                cache = collect_area(op, a)
            except Exception as e:
                print(f"  [실패] {a['name']}: {e}")
        if not cache:
            continue
        a.update(center=cache["center"], bbox=cache["bbox"], place=cache.get("place"),
                 collected_at=cache.get("collected_at"), windows=cache.get("windows"))
        for l in cache["raw"]:
            raw.append(dict(l, area=a["key"]))
    return raw


def dist_km(a, b):
    if None in a or None in b:
        return 1e9
    x = math.radians(b[1] - a[1]) * math.cos(math.radians((a[0] + b[0]) / 2))
    y = math.radians(b[0] - a[0])
    return 6371 * math.hypot(x, y)


def merge_listings(raw, areas):
    """같은 숙소가 여러 지역·날짜에 나오면 하나로 합치고 가장 가까운 지역에 배정한다."""
    by = {}
    for r in raw:
        it = by.setdefault(r["id"], {k: r[k] for k in (
            "id", "title", "name", "lat", "lng", "rating", "reviews", "is_new",
            "guest_fav", "bedrooms", "info")} | {"prices": {}, "seen": set()})
        it["seen"].add(r["area"])
        if r["total"]:
            it["prices"][r["window"]] = round(r["total"] / r["nights"])
    centers = {a["key"]: a["center"] for a in areas if a.get("center")}
    out = []
    for it in by.values():
        if not it["prices"]:
            continue
        it["area"] = min(it["seen"], key=lambda n: dist_km((it["lat"], it["lng"]), centers[n]))
        it["nightly"] = round(sum(it["prices"].values()) / len(it["prices"]))
        it["url"] = f"https://www.airbnb.co.kr/rooms/{it['id']}"
        del it["seen"]
        out.append(it)
    return out


# ---------------------------------------------------------------- 국토부 전월세
def fetch_rent_csv(sidos):
    """시도별 연립다세대·단독다가구 전월세 CSV. 반환: {(시도, 유형): 파일}"""
    MOLIT_DIR.mkdir(parents=True, exist_ok=True)
    page = "https://rt.molit.go.kr/pt/xls/xls.do?mobileAt="
    op = opener(page)
    op.open(page, timeout=25).read()
    codes = {x["ctprvnNm"]: x["signguCode"] for x in json.loads(op.open(urllib.request.Request(
        "https://rt.molit.go.kr/data/sido.do", data=b"", headers={"X-Requested-With": "XMLHttpRequest"}),
        timeout=60).read().decode("utf-8"))}
    files = {}
    for sido in sorted(sidos):
        if sido not in codes:
            print(f"  [건너뜀] 국토부 시도 목록에 없는 이름: {sido} (가능한 이름: {', '.join(codes)})")
            continue
        for code, label in (("B", "연립다세대"), ("C", "단독다가구")):
            f = MOLIT_DIR / f"{sido}_{label}_전월세_{RENT_FROM:%Y%m%d}_{RENT_TO:%Y%m%d}.csv"
            if not f.exists():
                body = urllib.parse.urlencode({
                    "srhThingNo": code, "srhDelngSecd": "2", "srhAddrGbn": "1", "srhLfstsSecd": "1",
                    "sidoNm": sido, "sggNm": "전체", "emdNm": "전체", "loadNm": "전체",
                    "areaNm": "전체", "hsmpNm": "전체", "mobileAt": "",
                    "srhFromDt": RENT_FROM.isoformat(), "srhToDt": RENT_TO.isoformat(),
                    "srhNewRonSecd": "", "srhSidoCd": codes[sido], "srhSggCd": "", "srhEmdCd": "",
                    "srhRoadNm": "", "srhLoadCd": "", "srhHsmpCd": "", "srhArea": "",
                    "srhFromAmount": "", "srhToAmount": "", "srhLrArea": "",
                }).encode()
                raw = op.open(urllib.request.Request(
                    "https://rt.molit.go.kr/pt/xls/ptXlsCSVDown.do", data=body,
                    headers={"X-Requested-With": "XMLHttpRequest"}), timeout=300).read()
                if b'"NO"' not in raw[:4000]:
                    # 일부 시도만 빠지면 그 지역 월세가 조용히 비게 된다: 실패로 처리해 직전 수집분으로 넘어가게 한다
                    raise ConnectionError(f"국토부 {sido} {label}: CSV가 아닌 응답")
                f.write_bytes(raw)
                print(f"  국토부 {sido} {label} 전월세: {len(raw) / 1e6:.1f}MB 저장")
                time.sleep(2)
            files[(sido, label)] = f
    return files


def load_rents(files, areas):
    """지역별 신규 월세 계약 [면적, 보증금(만원), 월세(만원), 주택유형, 시군구·동, 도로명, 건축년도, 계약년월]"""
    out = {a["key"]: [] for a in areas}
    by_sido = {}
    for a in areas:
        by_sido.setdefault(a["sido"], []).append((f"{a['sido']} {a['gu']} ", a))
    for (sido, label), f in files.items():
        text = f.read_bytes().decode("cp949", errors="replace")
        for row in csv.DictReader(io.StringIO(text[text.find('"NO"'):])):
            if row.get("전월세구분") != "월세" or row.get("계약구분") != "신규":
                continue
            addr = row["시군구"].strip() + " "
            hits = [a for pre, a in by_sido.get(sido, []) if addr.startswith(pre)]
            if not hits:
                continue
            try:
                area = float(row.get("전용면적(㎡)") or row.get("계약면적(㎡)"))
                dep = int(row["보증금(만원)"].replace(",", ""))
                rent = int(row["월세금(만원)"].replace(",", ""))
            except Exception:
                continue
            dong = addr.split(" ")[-2]
            for a in hits:
                if not a["dongs"] or any(dong.startswith(d) for d in a["dongs"]):
                    out[a["key"]].append([round(area, 1), dep, rent, label, " ".join(addr.split(" ")[2:]).strip(),
                                          row.get("도로명") or "", row.get("건축년도") or "",
                                          row.get("계약년월") or ""])
    return out


def main():
    args = sys.argv[1:]
    refresh = "--refresh" in args
    adds = [args[i + 1] for i, x in enumerate(args) if x == "--add" and i + 1 < len(args)]
    if adds:
        extra = json.loads(EXTRA_FILE.read_text(encoding="utf-8")) if EXTRA_FILE.exists() else []
        for x in adds:
            sido, _, sgg = x.strip().partition(" ")
            if not sgg:
                sys.exit(f'--add 형식: "시도 시군구" (예: "부산광역시 기장군"), 받은 값: {x}')
            if [sido, sgg] not in extra:
                extra.append([sido, sgg])
        EXTRA_FILE.write_text(json.dumps(extra, ensure_ascii=False, indent=1), encoding="utf-8")

    DATA.mkdir(exist_ok=True)
    areas = all_areas()
    print(f"1) 에어비앤비 검색 결과 수집 (지역 {len(areas)}곳, 이미 받은 곳은 건너뜀)")
    raw = fetch_airbnb(areas, refresh)
    areas = [a for a in areas if a.get("center")]
    listings = merge_listings(raw, areas)
    print(f"  검색 노출 {len(raw)}건 → 숙소 {len(listings)}곳")
    if len(areas) < 5 or len(listings) < 300:
        # 차단되었거나 접속하지 못해 거의 못 받은 경우: 기존 결과를 덮어쓰지 않고 끝낸다
        print(f"수집량이 비정상적으로 적습니다(지역 {len(areas)}곳, 숙소 {len(listings)}곳). 결과를 저장하지 않습니다.", flush=True)
        sys.exit(75)

    print("2) 국토부 전월세 실거래 수집")
    rent_meta = {"rent_from": RENT_FROM.isoformat(), "rent_to": RENT_TO.isoformat(), "rent_reused": False}
    try:
        rents = load_rents(fetch_rent_csv({a["sido"] for a in areas}), areas)
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        # 국토부 사이트는 해외 IP(GitHub 러너)에서 접속되지 않는다: 직전 results.json의 월세 계약을 그대로 쓴다
        f = DATA / "results.json"
        if not f.exists():
            raise
        prev = json.loads(f.read_text(encoding="utf-8"))
        old = {a["name"]: a.get("rents") or [] for a in prev["areas"]}
        rents = {a["key"]: old.get(a["name"], []) for a in areas}
        rent_meta = {"rent_from": prev["meta"]["rent_from"], "rent_to": prev["meta"]["rent_to"], "rent_reused": True}
        print(f"  국토부 사이트에 접속하지 못했습니다({e}).")
        print(f"  직전 수집분의 월세 계약({rent_meta['rent_from']} ~ {rent_meta['rent_to']})을 그대로 씁니다.")

    out = []
    for a in areas:
        ls = [l for l in listings if l["area"] == a["key"]]
        for l in ls:
            l["area"] = a["name"]
        out.append({"name": a["name"], "unit": a["unit"], "sido": a["sido"], "gu": a["gu"],
                    "dongs": a["dongs"], "center": a["center"], "bbox": a["bbox"],
                    "collected_at": a.get("collected_at"), "windows": a.get("windows"),
                    "listings": ls, "rents": rents[a["key"]]})
        print(f"  {a['name']}: 숙소 {len(ls)}곳 · 신규 월세 계약 {len(rents[a['key']])}건")
    meta = {
        "collected_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
        "windows": [{"label": w[0], "checkin": w[1].isoformat(), "nights": w[2]} for w in WINDOWS],
        **rent_meta,
        "pages": PAGES, "guests": 2,
    }
    (DATA / "results.json").write_text(
        json.dumps({"meta": meta, "areas": out}, ensure_ascii=False), encoding="utf-8")
    print("완료: data/results.json  →  python build_report.py 로 report.html을 갱신하세요")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    try:
        main()
    except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
        # 종료 코드 75: 대상 사이트에 접속하지 못함(코드 오류가 아님). GitHub Actions가 다른 러너에서 다시 시도한다.
        print(f"수집 대상 사이트에 접속하지 못했습니다: {e}", flush=True)
        sys.exit(75)
