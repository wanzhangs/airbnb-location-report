"""숙소 상세 페이지에서 최대 인원·항목별 평점·후기 주제·주의 고지를 모은다.

실행:  python collect_details.py          (collect.py 다음에 실행, 이미 받은 숙소는 건너뜀)
출력:  data/details/<숙소 id>.json   숙소별 캐시
       data/details.json            build_report.py가 읽는 묶음

지역마다 침실 수(1개 / 2개 / 3개 이상)별로 후기가 많은 숙소 PER_BUCKET곳씩만 본다.
후기 원문은 공개 페이지에 들어 있지 않아 읽지 않는다(에어비앤비가 집계한 주제·평점만 사용).
"""
import http.cookiejar
import json
import re
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"
DETAIL_DIR = DATA / "details"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")
PER_BUCKET = 4
WORKERS = 3


def bucket(b):
    return None if b is None else (1 if b <= 1 else 2 if b == 2 else 3)


def pick(areas):
    ids = []
    for a in areas:
        for k in (1, 2, 3):
            ls = [l for l in a["listings"] if bucket(l["bedrooms"]) == k]
            ls.sort(key=lambda l: l["reviews"], reverse=True)
            ids += [l["id"] for l in ls[:PER_BUCKET]]
    return list(dict.fromkeys(ids))


def walk(o):
    if isinstance(o, dict):
        yield o
        for v in o.values():
            yield from walk(v)
    elif isinstance(o, list):
        for v in o:
            yield from walk(v)


def parse(html):
    m = re.search(r'<script id="data-deferred-state-0"[^>]*>(.*?)</script>', html, re.S)
    if not m:
        return None
    out = {"cap": None, "cat": {}, "low": None, "n": None, "tags": {}, "safety": [], "elev": False, "parking": False}
    for d in walk(json.loads(m.group(1))):
        if out["cap"] is None and isinstance(d.get("personCapacity"), int):
            out["cap"] = d["personCapacity"]
        if isinstance(d.get("categoryRatings"), list) and not out["cat"]:
            for c in d["categoryRatings"]:
                try:
                    out["cat"][c["label"]] = float(c["localizedRating"])
                except Exception:
                    pass
        if isinstance(d.get("ratingDistribution"), list) and out["low"] is None:
            try:
                out["low"] = round(sum(c["percentage"] for c in d["ratingDistribution"] if c["label"] in "123"), 4)
            except Exception:
                pass
        if isinstance(d.get("reviewTags"), list) and not out["tags"]:
            out["tags"] = {t["localizedName"]: t["count"] for t in d["reviewTags"]
                           if t.get("name") != "HAS_PHOTO" and t.get("count")}
        if d.get("__typename") == "DemandReviewRatingStats" and out["n"] is None:
            try:
                out["n"] = int(d.get("ratingCount") or 0)
            except Exception:
                pass
        if d.get("__typename") == "StaysPdpSafetyAndPropertyDetails":
            for g in d.get("groupItems") or []:
                for it in g.get("items") or []:
                    if it.get("title") and it["title"] not in out["safety"]:
                        out["safety"].append(it["title"])
        if d.get("__typename") == "AmenityItem" and d.get("available"):
            t = d.get("title") or ""
            out["elev"] = out["elev"] or "엘리베이터" in t
            out["parking"] = out["parking"] or "주차" in t
    return out


def main():
    res = json.loads((DATA / "results.json").read_text(encoding="utf-8"))
    ids = pick(res["areas"])
    DETAIL_DIR.mkdir(parents=True, exist_ok=True)
    todo = [i for i in ids if not (DETAIL_DIR / f"{i}.json").exists()]
    print(f"상세 대상 {len(ids)}곳 · 새로 받을 곳 {len(todo)}곳", flush=True)
    done = [0]

    def work(chunk):                       # 상세 페이지 응답이 느려(곳당 약 8초) 동시 WORKERS개로 나눠 받는다
        op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        op.addheaders = [("User-Agent", UA), ("Accept-Language", "ko-KR,ko;q=0.9")]
        for i in chunk:
            try:
                d = parse(op.open(f"https://www.airbnb.co.kr/rooms/{i}", timeout=60).read().decode("utf-8", errors="replace"))
            except Exception as e:
                print(f"  [실패] {i}: {e}", flush=True)
                d = None
            if d:
                (DETAIL_DIR / f"{i}.json").write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
            done[0] += 1
            if done[0] % 25 == 0:
                print(f"  {done[0]}/{len(todo)}", flush=True)
            time.sleep(1.0)

    with ThreadPoolExecutor(WORKERS) as ex:
        list(ex.map(work, [todo[k::WORKERS] for k in range(WORKERS)]))
    out = {}
    for i in ids:
        f = DETAIL_DIR / f"{i}.json"
        if f.exists():
            out[i] = json.loads(f.read_text(encoding="utf-8"))
    (DATA / "details.json").write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    print(f"완료: data/details.json ({len(out)}곳)")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
