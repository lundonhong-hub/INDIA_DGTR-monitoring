#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
DGTR/DGFT 인도 무역규제 모니터 (2차 그물)
- DGTR 반덤핑 조사 목록 감시 (신규 케이스 감지)
- 감지 기준: 케이스 슬러그(URL) — 날짜가 없으므로 슬러그로 신규 판단
- KEYWORDS에 매칭된 것만 메일 발송, 무관 항목은 state만 기록
- state.json을 [] 로 비우면 전체 재알림 (PIB 방식 통일)

교훈 반영:
  1. 브라우저 UA 필수 (GitHub Actions IP는 통과)
  2. 조용한 0건 방어: MIN_EXPECTED_ITEMS 미만이면 구조 깨짐 경보
  3. 한국 규격코드(KS) 무의미 → 인도 영문 제품명으로 감시
  4. 파싱 전 제목 로그 출력으로 눈 확인
"""

import os
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
# 인도 반덤핑 공고는 한국 규격코드(KS) 아닌 영문 제품명으로 표기
# ─────────────────────────────────────────────
KEYWORDS = [
    "copper",
    "copper tube",
    "copper pipe",
    "copper alloy",
    "copper rod",
    "copper strip",
    "copper wire",
    "refined copper",
    "brass",
    "bronze",
]

# ─────────────────────────────────────────────
# 설정
# ─────────────────────────────────────────────
DGTR_URL = "https://www.dgtr.gov.in/en/anti-dumping-investigation-in-india"
MIN_EXPECTED_ITEMS = 10
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
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            # [] 로만 입력해도 초기화로 인식 (PIB 방식과 통일)
            if isinstance(data, list):
                log("state.json이 [] 형식 → 초기화로 인식")
                return {"seen_slugs": [], "empty_streak": 0, "last_run": None}
            return data
        except Exception as e:
            log(f"⚠️ state.json 읽기 실패, 새로 시작: {e}")
    return {"seen_slugs": [], "empty_streak": 0, "last_run": None}


def save_state(state: dict) -> None:
    state["last_run"] = datetime.datetime.now().isoformat()
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


# ─────────────────────────────────────────────
# 수집 & 파싱
# ─────────────────────────────────────────────
def fetch_dgtr() -> str:
    log(f"GET {DGTR_URL}")
    r = requests.get(DGTR_URL, headers=HEADERS, timeout=30)
    log(f"STATUS {r.status_code}, LEN {len(r.text)}")
    r.raise_for_status()
    return r.text


def parse_items(html: str) -> list:
    """항목 링크는 모두 /anti-dumping-cases/ 포함. 왼쪽 메뉴 잡링크와 구분."""
    soup = BeautifulSoup(html, "lxml")
    items = []
    seen_local = set()
    for a in soup.select('a[href*="/anti-dumping-cases/"]'):
        href = a.get("href", "").strip()
        title = a.get_text(strip=True)
        if not href or not title:
            continue
        slug = href.rstrip("/").split("/")[-1]
        if not slug or slug in seen_local:
            continue
        seen_local.add(slug)
        # 상대경로(/en/...)이면 도메인 붙여서 절대 URL로 정규화
        if href.startswith("/"):
            href = "https://www.dgtr.gov.in" + href
        items.append({"slug": slug, "title": title, "url": href})
    return items


def classify(title: str) -> list:
    """제목에 걸린 키워드 반환 (없으면 빈 리스트)"""
    low = title.lower()
    return [k for k in KEYWORDS if k in low]


# ─────────────────────────────────────────────
# 알림
# ─────────────────────────────────────────────
def send_email(subject: str, body_html: str) -> None:
    if not (GMAIL_USER and GMAIL_APP_PASSWORD and NOTIFY_TO):
        log("⚠️ Gmail 설정 없음 → 이메일 생략 (로컬 테스트로 간주)")
        log(f"--- 메일 미리보기 ---\n제목: {subject}\n{body_html[:500]}")
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


def build_email(matched: list) -> tuple:
    n = len(matched)
    subject = f"🔴 [DGTR] 동관 관련 반덤핑 조사 {n}건 감지"
    parts = [
        "<h2>🔴 DGTR 반덤핑 조사 — 동관 관련 신규 감지</h2>",
        "<ul>",
    ]
    for it in matched:
        kw = ", ".join(it["keywords"])
        parts.append(
            f"<li><b>{it['title']}</b><br>"
            f"매칭 키워드: <code>{kw}</code><br>"
            f"<a href='{it['url']}'>{it['url']}</a><br><br></li>"
        )
    parts.append("</ul>")
    parts.append(
        f"<hr><small>DGTR/DGFT 모니터 (2차 그물) · "
        f"{datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}</small>"
    )
    return subject, "\n".join(parts)


def send_alert_structure_broken(count: int, streak: int) -> None:
    subject = f"⚠️ [DGTR 모니터] 구조 깨짐 의심 — 파싱 {count}건 (연속 {streak}회)"
    body = (
        f"<h2>⚠️ DGTR 파서 이상 감지</h2>"
        f"<p>파싱된 항목이 <b>{count}건</b>으로 임계치({MIN_EXPECTED_ITEMS})보다 적습니다.</p>"
        f"<p>연속 이상 횟수: <b>{streak}회</b></p>"
        f"<p>사이트 구조/URL 변경 가능성이 높습니다. 파서 점검 필요.</p>"
        f"<p>URL: <a href='{DGTR_URL}'>{DGTR_URL}</a></p>"
    )
    send_email(subject, body)


# ─────────────────────────────────────────────
# 메인
# ─────────────────────────────────────────────
def main() -> int:
    state = load_state()
    seen = set(state.get("seen_slugs", []))

    try:
        html = fetch_dgtr()
    except Exception as e:
        log(f"❌ 수집 실패: {e}")
        return 1

    items = parse_items(html)
    count = len(items)
    log(f"파싱된 항목: {count}건")

    # 교훈 4: 상위 5건 제목 로그 출력
    for it in items[:5]:
        log(f"  · {it['title'][:70]}")

    # 안전장치: 조용한 0건 방어
    if count < MIN_EXPECTED_ITEMS:
        state["empty_streak"] = state.get("empty_streak", 0) + 1
        log(f"⚠️ 파싱 {count}건 < 임계치 {MIN_EXPECTED_ITEMS} "
            f"(연속 {state['empty_streak']}회) → 구조 깨짐 의심")
        send_alert_structure_broken(count, state["empty_streak"])
        save_state(state)
        return 2
    else:
        state["empty_streak"] = 0

    new_items = [it for it in items if it["slug"] not in seen]

    if not new_items:
        log("변화 없음 — 신규 항목 0건")
        save_state(state)
        return 0

    # 키워드 매칭된 것만 메일 발송, 무관은 state만 기록
    matched, unmatched = [], []
    for it in new_items:
        kws = classify(it["title"])
        if kws:
            it["keywords"] = kws
            matched.append(it)
        else:
            unmatched.append(it)

    log(f"🆕 신규 {len(new_items)}건 (🔴 매칭 {len(matched)} / ⚪ 무관 {len(unmatched)} — state만 기록)")

    if matched:
        subject, body = build_email(matched)
        send_email(subject, body)
    else:
        log("키워드 무관 항목만 있음 — 메일 없음, state 갱신만 함")

    # seen 갱신 (매칭·무관 모두 등록)
    for it in new_items:
        seen.add(it["slug"])
    state["seen_slugs"] = list(seen)
    save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
