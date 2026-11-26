"""국토부 전월세 실거래(연립다세대·단독다가구, 최근 6개월)를 시도별 스냅샷으로 받아 둔다.

실행:
    python update_rents.py                 저장한 지 MAX_AGE일(기본 28일)이 지난 것만 다시 받는다
    python update_rents.py --max-age 0     전부 다시 받는다
    python update_rents.py --limit 4       이번 실행에서 최대 4개만 받는다
    python update_rents.py --fail-after 45 아무것도 못 받았고 가장 오래된 스냅샷이 45일을 넘으면 오류(1)로 끝낸다

출력:
    data/rents/<시도>_<유형>.csv.gz   신규 월세 계약만 추린 것
    data/rents/meta.json             스냅샷별 저장일·기간·건수

받는 시도는 collect.py에 설정된 지역(areas_extra.json 포함)이 속한 시도다. collect.py는 이 폴더만 읽는다.
국토부 사이트는 해외 IP(GitHub 러너)에서 접속이 됐다 안 됐다 하므로, 받을 수 있을 때 받은 것만 저장하고
다음 실행이 남은 것을 이어서 받는다.
종료 코드: 0 = 받았거나 받을 것이 없음, 75 = 접속하지 못해 아무것도 못 받음(다른 러너에서 재시도), 1 = 오류.
"""
import csv
import datetime as dt
import gzip
import http.client
import http.cookiejar
import io
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
RENT_DIR = HERE / "data" / "rents"
META = RENT_DIR / "meta.json"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")
MOLIT = "https://rt.molit.go.kr"
PAGE = MOLIT + "/pt/xls/xls.do?mobileAt="
TODAY = dt.date.today()
RENT_FROM = TODAY - dt.timedelta(days=183)
RENT_TO = TODAY
MAX_AGE = 28
TYPES = {"연립다세대": "B", "단독다가구": "C"}
COLS = ["sgg", "area", "deposit_man", "rent_man", "road", "built", "ym"]
NET_ERRORS = (urllib.error.URLError, TimeoutError, ConnectionError, http.client.HTTPException)


def load_meta():
    return json.loads(META.read_text(encoding="utf-8")) if META.exists() else {}


def age(meta, key):
    m = meta.get(key)
    return None if not m else (TODAY - dt.date.fromisoformat(m["saved_at"])).days


def needed_keys():
    from collect import all_areas                     # 설정된 지역이 속한 시도만 받는다
    return [f"{sido}_{label}" for sido in sorted({a["sido"] for a in all_areas()}) for label in TYPES]


def session():
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    op.addheaders = [("User-Agent", UA), ("Referer", PAGE)]
    op.open(PAGE, timeout=25).read()
    req = urllib.request.Request(MOLIT + "/data/sido.do", data=b"", headers={"X-Requested-With": "XMLHttpRequest"})
    codes = {s["ctprvnNm"]: s["signguCode"] for s in json.loads(op.open(req, timeout=25).read().decode("utf-8"))}
    return op, codes


def download(op, sido, code, label):
    body = urllib.parse.urlencode({
        "srhThingNo": TYPES[label], "srhDelngSecd": "2", "srhAddrGbn": "1", "srhLfstsSecd": "1",
        "sidoNm": sido, "sggNm": "전체", "emdNm": "전체", "loadNm": "전체",
        "areaNm": "전체", "hsmpNm": "전체", "mobileAt": "",
        "srhFromDt": RENT_FROM.isoformat(), "srhToDt": RENT_TO.isoformat(),
        "srhNewRonSecd": "", "srhSidoCd": code, "srhSggCd": "", "srhEmdCd": "",
        "srhRoadNm": "", "srhLoadCd": "", "srhHsmpCd": "", "srhArea": "",
        "srhFromAmount": "", "srhToAmount": "", "srhLrArea": "",
    }).encode("utf-8")
    req = urllib.request.Request(MOLIT + "/pt/xls/ptXlsCSVDown.do", data=body,
                                 headers={"X-Requested-With": "XMLHttpRequest"})
    raw = op.open(req, timeout=240).read()
    if b'"NO"' not in raw[:4000]:
        raise ConnectionError(f"CSV가 아닌 응답 ({len(raw)}바이트)")
    return raw


def parse(raw):
    """신규 월세 계약만 남긴다."""
    text = raw.decode("cp949", errors="replace")
    rows = []
    for row in csv.DictReader(io.StringIO(text[text.find('"NO"'):])):
        if row.get("전월세구분") != "월세" or row.get("계약구분") != "신규":
            continue
        try:
            area = round(float(row.get("전용면적(㎡)") or row.get("계약면적(㎡)")), 1)
            dep = int(row["보증금(만원)"].replace(",", ""))
            rent = int(row["월세금(만원)"].replace(",", ""))
        except Exception:
            continue
        rows.append([row["시군구"].strip(), area, dep, rent, row.get("도로명") or "",
                     row.get("건축년도") or "", row.get("계약년월") or ""])
    rows.sort(key=lambda r: (r[0], r[6], r[1], r[2], r[3], r[4]))
    return rows


def write_snapshot(key, rows, meta, saved_at=None):
    RENT_DIR.mkdir(parents=True, exist_ok=True)
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(COLS)
    w.writerows(rows)
    with open(RENT_DIR / f"{key}.csv.gz", "wb") as f:        # mtime=0: 내용이 같으면 파일도 같게
        with gzip.GzipFile(fileobj=f, mode="wb", mtime=0, filename="") as g:
            g.write(buf.getvalue().encode("utf-8"))
    meta[key] = {"saved_at": saved_at or TODAY.isoformat(), "rent_from": RENT_FROM.isoformat(),
                 "rent_to": RENT_TO.isoformat(), "rows": len(rows)}
    META.write_text(json.dumps(meta, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")


def load_snapshot_rows(sido):
    """한 시도의 신규 월세 계약: (시군구 문자열, 면적, 보증금, 월세, 유형, 도로명, 건축년도, 계약년월)"""
    for label in TYPES:
        f = RENT_DIR / f"{sido}_{label}.csv.gz"
        if not f.exists():
            continue
        with gzip.open(f, "rt", encoding="utf-8", newline="") as g:
            for r in csv.DictReader(g):
                yield r["sgg"], float(r["area"]), int(r["deposit_man"]), int(r["rent_man"]), label, r["road"], r["built"], r["ym"]


def opt(args, name, default):
    return int(args[args.index(name) + 1]) if name in args else default


def main():
    args = sys.argv[1:]
    max_age, limit, fail_after = opt(args, "--max-age", MAX_AGE), opt(args, "--limit", 99), opt(args, "--fail-after", None)
    meta, keys = load_meta(), needed_keys()
    stale = [k for k in keys if age(meta, k) is None or age(meta, k) >= max_age]
    if not stale:
        print(f"필요한 스냅샷 {len(keys)}개가 모두 {max_age}일 이내입니다. 받을 것이 없습니다.")
        return

    def give_up(why):
        ages = [age(meta, k) for k in keys]
        oldest = None if None in ages else max(ages)
        print(f"국토부 전월세 실거래를 받지 못했습니다: {why}", flush=True)
        print(f"기존 스냅샷: 필요한 {len(keys)}개 중 {sum(a is not None for a in ages)}개 있음, "
              f"가장 오래된 것 {oldest if oldest is not None else '없는 것이 있음'}", flush=True)
        if fail_after is not None and oldest is not None and oldest > fail_after:
            print(f"스냅샷이 {fail_after}일보다 오래됐습니다. PC에서 python update_rents.py 를 실행해 푸시하세요.", flush=True)
            sys.exit(1)
        sys.exit(75)

    try:
        op, codes = session()
    except NET_ERRORS as e:
        give_up(e)
    stale.sort(key=lambda k: -(age(meta, k) if age(meta, k) is not None else 10 ** 6))      # 없는 것, 오래된 것부터
    stale = stale[:limit]
    print(f"받을 스냅샷 {len(stale)}개: {', '.join(stale)}", flush=True)
    done, fails = 0, 0
    for key in stale:
        sido, label = key.rsplit("_", 1)
        if sido not in codes:
            print(f"  [건너뜀] 국토부 시도 목록에 없는 이름: {sido} (가능한 이름: {', '.join(codes)})", flush=True)
            continue
        raw = None
        for attempt in range(3):                      # 러너에서는 두 번째 요청부터 연결이 끊기는 일이 잦다
            try:
                raw = download(op, sido, codes[sido], label)
                break
            except NET_ERRORS as e:
                print(f"  {key}: 실패 ({e})", flush=True)
                time.sleep(10 + 15 * attempt)
                try:
                    op, codes = session()             # 끊긴 뒤에는 세션을 새로 연다
                except NET_ERRORS:
                    break
        if raw is None:
            fails += 1
            if fails >= 3:
                print("  연속 실패로 중단합니다. 남은 것은 다음 실행에서 받습니다.", flush=True)
                break
            continue
        fails = 0
        rows = parse(raw)
        write_snapshot(key, rows, meta)
        done += 1
        print(f"  {key}: 신규 월세 {len(rows):,}건 저장 ({len(raw) / 1e6:.1f}MB 수신)", flush=True)
        time.sleep(3)
    left = [k for k in keys if age(meta, k) is None or age(meta, k) >= max_age]
    print(f"이번 실행: {done}개 갱신 · 아직 오래됐거나 없는 것 {len(left)}개", flush=True)
    if not done:
        give_up("받을 것이 있었지만 하나도 받지 못함")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
