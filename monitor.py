#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
인도 무역규제 모니터 (2차 그물) — DGTR + DGFT 통합
- DGTR: 반덤핑 조사 목록 (dgtr.gov.in, 직접 접근)
- DGFT: 수입정책 Notification/Public Notice (caalley 미러 경유, 공식은 로그인벽)
- 동관(copper) 키워드 매칭된 것만 메일 발송, 무관은 state만 기록
- state.json을 [] 로 비우면 전체 재알림 (PIB 방식 통일)

교훈 반영:
  1. 브라우저 UA 필수 (GitHub Actions IP는 통과)
  2. 조용한 0건 방어: 소스별 임계치 미만이면 구조 깨짐 경보
  3. 한국 규격코드(KS) 무의미 → 인도 영문명·Chapter·HS코드로 감시
  4. 파싱 전 제목 로그 출력으로 눈 확인
"""

import os
import re
import sys
import json
import smtplib
import datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

import requests
from bs4 import BeautifulSoup

# ─────────────────────────────────────────────
# 감시 키워드 — 여기만 수정하면 됩니다
# ─────────────────────────────────────────────
# (A) 동관 직접 키워드 — 인도 공고는 영문명 + Chapter + HS코드로 표기
KEYWORDS_PRODUCT = [
    "copper",
    "brass",
    "bronze",
    "refined copper",
    "chapter 74",       # 동과 그 제품 (ITC HS)
    "nfmims",           # 비철금속 수입모니터링 (동 수입 시스템)
    "7407", "7408", "7409", "7410", "7411", "7412",  # 동관·봉·선·판 HS코드
]

# (B) 제도 키워드 — 동관에 걸리면 수출 직격탄인 규제 유형
#     단독으론 노이즈 많아, DGFT에서만 보조 신호로 사용
KEYWORDS_REGIME = [
    "qco",              # 품질관리명령 (Quality Control Order)
    "quality control order",
    "bis requirement",
    "compulsory registration",
    "import monitoring",
]

# ─────────────────────────────────────────────
# 소스 설정
# ─────────────────────────────────────────────
SOURCES = {
    "DGTR": {
        # 단일 소스 (공식 사이트 직접 접근)
        "urls": ["https://www.dgtr.gov.in/en/anti-dumping-investigation-in-india"],
        "min_items": 10,   # 항상 15건 차 있음
        "parser": "parse_dgtr",
    },
    "DGFT": {
        # 폴백 체인: 앞 소스가 죽으면 다음 소스 자동 시도 (미러 리스크 분산)
        # stargroup은 각 공고가 독립 링크+요약 → 파싱 안정적. caalley는 백업.
        "urls": [
            "https://stargroup.in/dgft_notifications_view.html",
            "https://caalley.com/legal-updates/corporate-laws/dgft",
        ],
        "min_items": 15,
        "parser": "parse_dgft",   # URL로 stargroup/caalley 자동 구분
    },
}

STATE_FILE = "state.json"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

GMAIL_USER = os.environ.get("GMAIL_USER", "")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "")
NOTIFY_TO = os.environ.get("NOTIFY_TO", GMAIL_USER)


# ─────────────────────────────────────────────
# 유틸
# ─────────────────────────────────────────────
def log(msg: str) -> None:
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def load_state() -> dict:
    default = {"DGTR": [], "DGFT": [], "empty_streak": {}, "last_run": None}
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            # [] 로 비우면 전체 초기화 (PIB 방식)
            if isinstance(data, list):
                log("state.json이 [] 형식 → 전체 초기화로 인식")
                return default
            # 누락 키 보정
            for k in ("DGTR", "DGFT"):
                data.setdefault(k, [])
            data.setdefault("empty_streak", {})
            return data
        except Exception as e:
            log(f"⚠️ state.json 읽기 실패, 새로 시작: {e}")
    return default


def save_state(state: dict) -> None:
    state["last_run"] = datetime.datetime.now().isoformat()
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def fetch(url: str) -> str:
    log(f"GET {url}")
    r = requests.get(url, headers=HEADERS, timeout=30)
    log(f"STATUS {r.status_code}, LEN {len(r.text)}")
    r.raise_for_status()
    return r.text


def fetch_with_fallback(urls: list) -> tuple:
    """urls를 순서대로 시도. 첫 성공(html, url) 반환. 전부 실패면 (None, None)."""
    for url in urls:
        try:
            html = fetch(url)
            return html, url
        except Exception as e:
            log(f"  ↳ 실패, 다음 소스 시도: {type(e).__name__}")
    return None, None


# ─────────────────────────────────────────────
# 파서 — 소스별
# ─────────────────────────────────────────────
def parse_dgtr(html: str) -> list:
    """DGTR: /anti-dumping-cases/ 링크. 슬러그가 고유 ID."""
    soup = BeautifulSoup(html, "lxml")
    items, seen_local = [], set()
    for a in soup.select('a[href*="/anti-dumping-cases/"]'):
        href = a.get("href", "").strip()
        title = a.get_text(strip=True)
        if not href or not title:
            continue
        slug = href.rstrip("/").split("/")[-1]
        if not slug or slug in seen_local:
            continue
        seen_local.add(slug)
        if href.startswith("/"):
            href = "https://www.dgtr.gov.in" + href
        items.append({"uid": f"DGTR:{slug}", "title": title, "url": href})
    return items


def parse_dgft(html: str, source_url: str = "") -> list:
    """DGFT: URL로 소스를 구분해 파싱.
    - stargroup: 각 공고가 독립 링크(notification-details-{번호}) + 요약. uid=상세페이지 번호.
    - caalley: '제목 [Notification No.XX]' 텍스트 나열. uid=카테고리:번호:연도."""
    if "stargroup" in source_url:
        return _parse_stargroup(html)
    return _parse_caalley(html)


def _parse_stargroup(html: str) -> list:
    soup = BeautifulSoup(html, "lxml")
    items, seen_local = [], set()
    for a in soup.select('a[href*="notification-details-"]'):
        href = a.get("href", "").strip()
        text = a.get_text(" ", strip=True)
        if not text.upper().startswith("DGFT"):   # Custom/GST 제외
            continue
        m = re.search(r'notification-details-(\d+)', href)
        if not m:
            continue
        uid = f"DGFT:sg:{m.group(1)}"
        if uid in seen_local:
            continue
        seen_local.add(uid)
        title = re.sub(r'^(DGFT\s*)+[\u2013-]\s*', '', text).strip()
        if href.startswith("/"):
            href = "https://stargroup.in" + href
        items.append({"uid": uid, "title": title, "url": href})
    return items


def _parse_caalley(html: str) -> list:
    text = BeautifulSoup(html, "lxml").get_text(" ", strip=True)
    pattern = re.compile(
        r'(.+?)\[(Notification|Public Notice|Circular|Trade Notice)\s+No\.?\s*(\d+)\]',
        re.I,
    )
    items, seen_local = [], set()
    for m in pattern.finditer(text):
        title = m.group(1).strip()
        if len(title) > 220:
            title = title[-220:]
        cat, num = m.group(2).strip(), m.group(3).strip()
        fy = re.search(r'20\d{2}[-/]\d{2,4}', title)
        year_tag = fy.group(0).replace("/", "-") if fy else ""
        uid = f"DGFT:ca:{cat}:{num}:{year_tag}"
        if uid in seen_local:
            continue
        seen_local.add(uid)
        items.append({
            "uid": uid, "title": title,
            "url": "https://caalley.com/legal-updates/corporate-laws/dgft",
        })
    return items


# ─────────────────────────────────────────────
# 분류
# ─────────────────────────────────────────────
def classify(title: str, source: str) -> list:
    """매칭 키워드 반환. DGFT는 제도 키워드도 보조 신호로 사용."""
    low = title.lower()
    hits = [k for k in KEYWORDS_PRODUCT if k in low]
    if source == "DGFT":
        hits += [k for k in KEYWORDS_REGIME if k in low]
    return hits


# ─────────────────────────────────────────────
# 알림
# ─────────────────────────────────────────────
def send_email(subject: str, body_html: str) -> None:
    if not (GMAIL_USER and GMAIL_APP_PASSWORD and NOTIFY_TO):
        log("⚠️ Gmail 설정 없음 → 이메일 생략 (로컬 테스트)")
        log(f"--- 미리보기 ---\n제목: {subject}\n{body_html[:400]}")
        return
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = GMAIL_USER
    msg["To"] = NOTIFY_TO
    msg.attach(MIMEText(body_html, "html", "utf-8"))
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(GMAIL_USER, GMAIL_APP_PASSWORD)
        server.sendmail(GMAIL_USER, NOTIFY_TO.split(","), msg.as_string())
    log(f"✅ 메일 발송 완료 → {NOTIFY_TO}")


def build_email(hits_by_source: dict) -> tuple:
    total = sum(len(v) for v in hits_by_source.values())
    subject = f"🔴 [인도규제] 동관 관련 신규 {total}건 감지"
    parts = ["<h2>🔴 인도 무역규제 — 동관 관련 신규 감지</h2>"]
    label = {"DGTR": "DGTR 반덤핑 조사", "DGFT": "DGFT 수입정책 공고"}
    for src, matched in hits_by_source.items():
        if not matched:
            continue
        parts.append(f"<h3>{label.get(src, src)} — {len(matched)}건</h3><ul>")
        for it in matched:
            kw = ", ".join(it["keywords"])
            parts.append(
                f"<li><b>{it['title']}</b><br>"
                f"매칭: <code>{kw}</code><br>"
                f"<a href='{it['url']}'>{it['url']}</a><br><br></li>"
            )
        parts.append("</ul>")
    parts.append(
        f"<hr><small>인도규제 모니터 (2차 그물: DGTR+DGFT) · "
        f"{datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}</small>"
    )
    return subject, "\n".join(parts)


def send_structure_alert(src: str, count: int, threshold: int, streak: int, url: str) -> None:
    subject = f"⚠️ [{src} 모니터] 구조 깨짐 의심 — {count}건 (연속 {streak}회)"
    body = (
        f"<h2>⚠️ {src} 파서 이상</h2>"
        f"<p>파싱 {count}건 &lt; 임계치 {threshold}. 연속 {streak}회.</p>"
        f"<p>소스 구조/URL 변경 가능성. 점검 필요.</p>"
        f"<p><a href='{url}'>{url}</a></p>"
    )
    send_email(subject, body)


# ─────────────────────────────────────────────
# 메인
# ─────────────────────────────────────────────
def _run_parser(name: str, html: str, source_url: str) -> list:
    if name == "parse_dgtr":
        return parse_dgtr(html)
    return parse_dgft(html, source_url)


def process_source(src: str, cfg: dict, state: dict) -> list:
    """한 소스 처리(폴백 체인). 매칭된 신규 항목 리스트 반환."""
    seen = set(state.get(src, []))
    html, used_url = fetch_with_fallback(cfg["urls"])
    if html is None:
        log(f"❌ [{src}] 모든 소스 수집 실패")
        streak = state["empty_streak"].get(src, 0) + 1
        state["empty_streak"][src] = streak
        send_structure_alert(src, 0, cfg["min_items"], streak, cfg["urls"][0])
        return []

    items = _run_parser(cfg["parser"], html, used_url)
    count = len(items)
    log(f"[{src}] 파싱 {count}건")
    for it in items[:5]:
        log(f"    · {it['title'][:65]}")

    # 조용한 0건 방어
    if count < cfg["min_items"]:
        streak = state["empty_streak"].get(src, 0) + 1
        state["empty_streak"][src] = streak
        log(f"⚠️ [{src}] {count}건 < 임계치 {cfg['min_items']} (연속 {streak}회) → 구조 깨짐 의심")
        send_structure_alert(src, count, cfg["min_items"], streak, used_url)
        return []   # seen 미갱신 (오염 방지)
    state["empty_streak"][src] = 0

    new_items = [it for it in items if it["uid"] not in seen]
    if not new_items:
        log(f"[{src}] 변화 없음")
        return []

    matched = []
    for it in new_items:
        kws = classify(it["title"], src)
        if kws:
            it["keywords"] = kws
            matched.append(it)

    log(f"[{src}] 🆕 신규 {len(new_items)}건 (🔴 매칭 {len(matched)} / ⚪ 무관 {len(new_items)-len(matched)})")

    # seen 갱신 (매칭·무관 모두)
    for it in new_items:
        seen.add(it["uid"])
    state[src] = list(seen)
    return matched


def main() -> int:
    state = load_state()
    hits_by_source = {}

    for src, cfg in SOURCES.items():
        matched = process_source(src, cfg, state)
        hits_by_source[src] = matched

    total = sum(len(v) for v in hits_by_source.values())
    if total > 0:
        subject, body = build_email(hits_by_source)
        send_email(subject, body)
    else:
        log("동관 관련 신규 없음 — 메일 없음")

    save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
