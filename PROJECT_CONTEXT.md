# 인도 무역규제 모니터 (2차 그물) — DGTR + DGFT 통합

인도 무역규제 감시 **3중 그물** 구조의 두 번째 그물.
동관(copper) 수출기업 관점에서, 인도의 무역구제·수입정책 변화를 실시간 감시한다.

| 단계 | 소스 | 성격 | 저장소 |
|------|------|------|--------|
| 1차 | PIB (RSS) | 정부 보도자료 | `India_PIB-monitor` |
| **2차** | **DGTR + DGFT** | **무역 실무 공고** | **이 저장소** |
| 3차 | eGazette (관보) | 법적 확정 | `egazette_monitor` |

## 감시 목적 (핵심)
동관을 제조해 인도로 수출한다. **인도가 동관 수입을 막는 규제 = 수출길 차단.**
그래서 아래 두 기관을 동관(copper) 중심으로 감시한다.
- **DGTR** (무역구제총국): 반덤핑 조사. 동관에 반덤핑 관세가 붙으면 가격경쟁력 상실.
- **DGFT** (대외무역총국): 수입정책. 동관(Chapter 74)이 Free→Restricted 되거나
  BIS/QCO 인증이 의무화되면 통관 자체가 막힘.

## 소스별 접근 방식 (중요)

### DGTR — 직접 접근 ✅
- URL: https://www.dgtr.gov.in/en/anti-dumping-investigation-in-india
- 서버사이드 렌더링(Drupal). requests + BeautifulSoup으로 충분. Playwright 불필요.
- 고유 ID: 케이스 슬러그(`/anti-dumping-cases/{슬러그}`). 날짜 없어 슬러그로 신규 판단.
- 항목 링크는 모두 `/anti-dumping-cases/` 포함 → 왼쪽 메뉴 잡링크와 구분.

### DGFT — 폴백 체인(미러 다중화) ⚠️
- **공식 사이트(dgft.gov.in/CP/)는 로그인 대시보드(JS 렌더링)** → requests로 목록 못 긁음.
  공개 JSON 백엔드도 없음. Playwright는 무겁고 셀렉터 취약 → 채택 안 함.
- **대안: 제3자 미러를 폴백 체인으로 다중화.** 앞 소스가 죽으면 다음 소스 자동 시도.
  (배포 첫날 caalley가 GitHub Actions IP에서 타임아웃 → 단일 미러 의존 위험 확인됨)
  1. **stargroup.in** (1순위): 각 공고가 독립 링크(`notification-details-{번호}`) + 요약 내용.
     HS코드·Chapter·MIP 조건까지 텍스트에 포함. uid=상세페이지 번호로 안정적.
  2. **caalley.com** (백업): 전 카테고리 텍스트 나열. uid=카테고리:번호:연도.
  - (APEDA 미러는 농산물 편향이라 탈락.)
- **미러 리스크 관리:** 폴백 체인 + "조용한 0건" 방어(임계치 15). 전 소스 실패 시에만 경보.
  DGTR과 독립적으로 방어(한쪽 깨져도 다른 쪽 정상).
- **개선 여지:** 새 미러 추가는 SOURCES["DGFT"]["urls"] 리스트에 URL만 넣고
  parse_dgft에 파서 분기 추가하면 됨.

## 감시 키워드 (monitor.py 상단에서 수정)
### (A) 동관 직접 — 양쪽 소스 공통
`copper`, `brass`, `bronze`, `refined copper`, `chapter 74`, `nfmims`,
`7407`~`7412` (동관·봉·선·판 HS코드)
- 인도 공고는 한국 규격코드(KS) 아닌 **영문명 + Chapter + HS코드**로 표기.
- `nfmims` = 비철금속 수입모니터링. 이 단어 뜨면 동 수입규제 관련.

### (B) 제도 키워드 — DGFT에서만 보조 신호
`qco`, `quality control order`, `bis requirement`,
`compulsory registration`, `import monitoring`
- 동관에 BIS/QCO가 걸리면 수출 직격탄. 단독으론 노이즈 많아 DGFT에만 적용.

## 알림 로직
- 양쪽 소스를 한 번에 돌려, **동관 매칭된 것만 한 메일로 통합** 발송.
- 무관 항목(농산물·타 화학물질 등)은 state.json에만 기록, 메일 없음.
- 매칭 0건이면 완전 조용.

## state.json 구조
```json
{
  "DGTR": ["DGTR:slug1", ...],
  "DGFT": ["DGFT:Notification:61:2015-2020", ...],
  "empty_streak": {"DGTR": 0, "DGFT": 0},
  "last_run": "..."
}
```
- **[] 로 비우면 전체 초기화** → 다음 실행에 현재 목록 전부 재평가(PIB 방식 통일).

## 기술 스택 (기존 2개와 통일)
Python 3.12 · GitHub Actions(하루 2회, KST 09/18시) · Gmail SMTP(Secrets) ·
state.json 자동 커밋 · 별도 저장소 독립 운영(장애 격리).

## 설치 (GitHub Secrets)
`GMAIL_USER`, `GMAIL_APP_PASSWORD`, `NOTIFY_TO`

## 교훈 반영 (1차 그물)
1. 브라우저 UA 필수 (데이터센터 IP는 403, GitHub Actions IP는 통과).
2. 조용한 0건 = 최대 위험 → 소스별 임계치 미만이면 구조 깨짐 경보, seen 미갱신.
3. 한국 규격코드 무의미 → 인도 영문명·Chapter·HS코드로 감시.
4. 파싱 전 상위 5건 제목 로그 출력으로 눈 확인.

## 배경 지식: 동관 수입규제 현황
- 인도는 2021년부터 동(Chapter 74) 수입에 **NFMIMS 등록 의무** 부과
  (DGFT Notification 61/2015-2020). 앞으로 위협은 이게 강화되거나(등록→제한→금지),
  BIS 인증이 추가되는 방향. 이 모니터가 그 변화를 잡는다.

## TODO (3중 그물 확장 아이디어)
- BIS 사이트 직접 감시 (품질인증 의무화 = 동관 수출 직격탄, 별도 채널 필요)
- DGFT 공식 사이트 Playwright 백업 (caalley 미러 영구 장애 대비)
