#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
인도 무역규제 모니터 (2차 그물) — DGTR + DGFT 공식 직접 통합
- DGTR: 반덤핑 조사 목록 (dgtr.gov.in, 직접 접근)
- DGFT: 공식 Notification 페이지(dgft.gov.in/CP/?opt=notification) 직접 접근
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
from urllib.parse import urljoin

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
DGFT_NOTIFICATION_URL = "https://www.dgft.gov.in/CP/?opt=notification"

SOURCES = {
    "DGTR": {
        # 단일 소스 (공식 사이트 직접 접근)
        "urls": ["https://www.dgtr.gov.in/en/anti-dumping-investigation-in-india"],
        "min_items": 10,   # 항상 15건 차 있음
        "parser": "parse_dgtr",
    },
    "DGFT": {
        # 공식 DGFT Notification 화면을 직접 감시합니다.
        # 화면에서 보이는 표(Number / Year / Description / Date / Attachment)를 그대로 파싱합니다.
        # 예: 22/2026-27 "Amendment in Import Policy under Chapter 74 ..."
        "urls": [DGFT_NOTIFICATION_URL],
        "min_items": 15,
        "parser": "parse_dgft",
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
    """DGFT 공식 Notification 표를 직접 파싱합니다.

    대상 화면:
      https://www.dgft.gov.in/CP/?opt=notification

    화면 컬럼:
      Sl.No. / Number / Year / Description / Date / CRT DT / Attachment

    기존 stargroup/caalley 미러는 사용하지 않습니다.
    """
    return _parse_dgft_official_notification(html, source_url or DGFT_NOTIFICATION_URL)


def _extract_first_link(row, base_url: str) -> str:
    """행 안의 첫 다운로드 링크를 절대 URL로 반환합니다."""
    a = row.find("a", href=True)
    if a and a.get("href"):
        return urljoin(base_url, a["href"].strip())

    # 일부 사이트는 onclick 안에 URL을 넣는 경우가 있어 방어적으로 처리
    for tag in row.find_all(True):
        onclick = tag.get("onclick", "") or ""
        m = re.search(r"['\"](https?://[^'\"]+|/[^'\"]+\.pdf[^'\"]*)['\"]", onclick, re.I)
        if m:
            return urljoin(base_url, m.group(1))

    return base_url


def _parse_dgft_official_notification(html: str, source_url: str = DGFT_NOTIFICATION_URL) -> list:
    """공식 DGFT Notification 페이지의 HTML 표를 파싱합니다."""
    soup = BeautifulSoup(html, "lxml")
    items, seen_local = [], set()

    for tr in soup.select("table tr"):
        tds = tr.find_all("td")
        if len(tds) < 6:
            continue

        cols = [td.get_text(" ", strip=True) for td in tds]
        # 기대 컬럼: 0 Sl.No. / 1 Number / 2 Year / 3 Description / 4 Date / 5 CRT DT / 6 Attachment
        sl_no = cols[0]
        number = cols[1] if len(cols) > 1 else ""
        year = cols[2] if len(cols) > 2 else ""
        desc = cols[3] if len(cols) > 3 else ""
        date = cols[4] if len(cols) > 4 else ""
        crt_dt = cols[5] if len(cols) > 5 else ""

        # 헤더/잡음 행 제거
        if not re.search(r"\d", sl_no):
            continue
        if not number or not desc:
            continue
        if "description" in desc.lower():
            continue

        pdf_url = _extract_first_link(tr, source_url)

        uid_base = f"{number}:{year}:{date}".strip(":")
        uid_base = re.sub(r"\s+", "", uid_base)
        uid = f"DGFT:official:{uid_base}"

        if uid in seen_local:
            continue
        seen_local.add(uid)

        title_bits = [
            f"Notification {number}",
            f"Year {year}" if year else "",
            desc,
            f"Date {date}" if date else "",
        ]
        title = " | ".join([x for x in title_bits if x])

        items.append({
            "uid": uid,
            "title": title,
            "url": pdf_url,
            "number": number,
            "year": year,
            "date": date,
            "crt_dt": crt_dt,
            "description": desc,
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
