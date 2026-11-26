# 에어비앤비 수익성 검토

지역과 임차 예산을 넣으면 월 순이익, 기대 임차 횟수, 초기 세팅비 회수 일정을 보여 주는 단일 페이지 리포트입니다.
`report.html`을 더블클릭하면 열립니다(지도 배경은 인터넷 연결 필요).

## 화면 구성
맨 위의 조건(범위·지역·보증금 한도·월세 한도·집 크기)과 가정(가동률·평균 숙박일·운영비·초기 세팅비)을 바꾸면 아래 전체가 함께 바뀝니다.

1. **핵심 숫자**: 월 순이익, 기대 임차 횟수, 세팅비 회수 시점, 연 수익률, 손익분기 가동률, 최대 인원, 1박 요금
2. **투자금 회수 일정**: 가동률을 보수적·기본·낙관적으로 나눈 36개월 누적 현금 흐름
3. **같은 조건으로 본 지역 순위**: 지역을 누르면 그 지역으로 전체가 바뀝니다
4. **근거**: 예산 안에서 실제 계약된 집, 집 크기별 비교, 비교 숙소 목록
5. **유의사항과 특이사항**: 숙소 상세 페이지의 항목별 평점·후기 주제 집계·주의 고지
6. **지도**와 **계산 방법·한계**

수치는 가정에 따라 크게 달라지는 1차 검토용이며 투자 권유가 아닙니다. 계산식과 한계는 페이지 맨 아래에 있습니다.

## 파일
- `update_rents.py` 국토부 전월세 실거래(연립다세대·단독다가구 신규 월세, 최근 6개월)를 시도별 스냅샷 `data/rents/`로 받아 둡니다. 저장한 지 28일이 지난 것만 다시 받습니다.
- `collect.py` 에어비앤비 공개 검색 결과를 수집하고 `data/rents/` 스냅샷과 합쳐 `data/results.json`을 만듭니다. 국토부에는 접속하지 않습니다.
- `collect_details.py` 지역마다 후기가 많은 숙소의 상세 페이지에서 최대 인원·평점·후기 주제·주의 고지를 모아 `data/details.json`을 만듭니다.
- `build_report.py` 위 결과를 `template.html`에 넣어 `report.html`을 만듭니다.

API 키와 로그인, LLM API는 쓰지 않습니다. 파이썬 3.11 표준 라이브러리만 사용합니다.

## 지역 추가 (서울 외 시군구)
시도 이름은 국토부 표기를 그대로 씁니다(예: 부산광역시, 강원특별자치도, 전북특별자치도, 전남광주통합특별시).

```
python collect.py --add "부산광역시 기장군" --add "충청남도 공주시"
python update_rents.py
python collect.py
python collect_details.py
python build_report.py
```

추가한 지역은 `areas_extra.json`에 저장됩니다. 새 시도가 추가되면 `update_rents.py`가 그 시도의 월세 스냅샷부터 받습니다.
GitHub에서는 Refresh data를 수동 실행할 때 "추가할 시군구"에 입력하면 됩니다.

## PC에서 새로 고치기
```
git pull
python update_rents.py
python collect.py --refresh
python collect_details.py
python build_report.py
```

`--refresh` 없이 실행하면 이미 받은 지역은 건너뜁니다. 수집 캐시(`data/areas`, `data/details`)는 저장소에 올리지 않습니다.

## GitHub Actions
| 워크플로 | 언제 | 하는 일 |
|---|---|---|
| **Update rents** | 매일 02:00 (한국 시간) | 28일 지난 월세 스냅샷을 받아 `data/rents/`에 커밋합니다. 모두 최신이면 접속하지 않고 끝납니다. |
| **Refresh data** | 매주 월요일 03:00 | 에어비앤비 요금과 숙소 상세를 새로 수집해 `data/`와 `report.html`을 커밋합니다. |
| **Build and publish report** | 푸시할 때, Refresh data가 끝났을 때 | 리포트를 다시 만들어 검사하고, GitHub Pages가 켜져 있으면 배포합니다. |

세 워크플로 모두 Actions 탭에서 수동으로도 실행할 수 있습니다.

GitHub 러너는 해외 IP라 국토부 사이트 접속이 러너에 따라 되기도 하고 안 되기도 합니다. 그래서 접속하지 못하면 새 러너에서 최대 3번까지 다시 시도하고(`*-once.yml`), 받을 수 있는 것만 받아 커밋한 뒤 다음 실행이 남은 것을 이어서 받습니다. 스냅샷이 45일 넘게 갱신되지 않으면 Update rents가 실패로 표시됩니다. 그때는 PC에서 `python update_rents.py`를 실행해 푸시하면 됩니다.

### GitHub Pages
저장소 Settings → Pages → Source에서 **GitHub Actions**를 선택하면, 리포트가 `https://<계정>.github.io/airbnb-location-report/`에 배포되고 Refresh data가 끝날 때마다 갱신됩니다. Pages가 꺼져 있으면 배포 단계만 건너뜁니다. Pages 사이트는 주소를 아는 누구나 볼 수 있고, 무료 요금제에서는 공개 저장소에서만 켤 수 있습니다.
