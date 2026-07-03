#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
인도 무역규제 모니터 — DGTR + DGFT 공식 사이트 직접 모니터링

- DGTR: 공식 반덤핑 조사 페이지 직접 수집
- DGFT: 공식 Notification 페이지 직접 수집
  1) https://www.dgft.gov.in/CP/index.jsp?opt=notification
  2) https://www.dgft.gov.in/CP/?opt=notification
- DGFT 미러 사이트 사용 안 함
- 메일 본문에는 DGTR/DGFT를 항상 표시
- 텔레그램은 설정되어 있을 때만 보조 알림 발송
"""

import os
import re
import sys
import json
import smtplib
import datetime
import subprocess
from urllib.parse import urljoin
from html import escape
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

import requests
from bs4 import BeautifulSoup

# ─────────────────────────────────────────────
# 감시 키워드
# ─────────────────────────────────────────────
KEYWORDS_PRODUCT = [
    "copper",
    "copper tube",
    "copper tubes",
    "copper pipe",
    "copper pipes",
    "brass",
    "bronze",
    "refined copper",
    "chapter 74",
    "cth 74",
    "itc hs 74",
    "nfmims",
    "7407", "7408", "7409", "7410", "7411", "7412",
]

KEYWORDS_REGIME = [
    "qco",
    "quality control order",
    "quality control orders",
    "bis",
    "bis requirement",
    "bis requirements",
    "compulsory registration",
    "import monitoring",
]

# ─────────────────────────────────────────────
# 소스 설정
# ─────────────────────────────────────────────
SOURCES = {
    "DGTR": {
        "urls": ["https://www.dgtr.gov.in/en/anti-dumping-investigation-in-india"],
        "min_items": 10,
        "parser": "parse_dgtr",
    },
    "DGFT": {
        "urls": [
            "https://www.dgft.gov.in/CP/index.jsp?opt=notification",
            "https://www.dgft.gov.in/CP/?opt=notification",
        ],
        "min_items": 10,
        "parser": "parse_dgft_official",
    },
}

STATE_FILE = "state.json"

BASE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9,ko;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}

DGFT_HEADERS = {
    **BASE_HEADERS,
    "Referer": "https://www.dgft.gov.in/CP/",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
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
            if isinstance(data, list):
                log("state.json이 [] 형식 → 전체 초기화로 인식")
                return default
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


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip()


def _headers_for(url: str) -> dict:
    if "dgft.gov.in/CP/" in url:
        return DGFT_HEADERS
    return BASE_HEADERS


def _looks_like_dgft_notification_page(html: str) -> bool:
    low = html.lower()
    return (
        "notification" in low
        and "sl.no" in low
        and "attachment" in low
        and ("content.dgft.gov.in" in low or "chapter" in low)
    )


def _fetch_with_requests(url: str) -> str:
    headers = _headers_for(url)
    with requests.Session() as s:
        # DGFT는 첫 접속 쿠키/세션 영향이 있을 수 있어 CP 루트도 먼저 접속
        if "dgft.gov.in/CP/" in url:
            try:
                s.get("https://www.dgft.gov.in/CP/", headers=headers, timeout=20, allow_redirects=True)
            except Exception:
                pass

        r = s.get(url, headers=headers, timeout=45, allow_redirects=True)
        log(f"STATUS {r.status_code}, LEN {len(r.text)}")
        if r.status_code >= 400:
            raise requests.HTTPError(f"HTTP {r.status_code}", response=r)
        return r.text


def _fetch_with_curl(url: str) -> str:
    """GitHub Actions에서 requests는 실패하지만 curl은 통과하는 경우 대비."""
    log(f"curl retry {url}")
    cmd = [
        "curl",
        "-L",
        "--compressed",
        "--retry", "2",
        "--retry-delay", "2",
        "--connect-timeout", "20",
        "--max-time", "60",
        "-A", BASE_HEADERS["User-Agent"],
        "-H", "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "-H", "Accept-Language: en-US,en;q=0.9,ko;q=0.8",
        "-H", "Referer: https://www.dgft.gov.in/CP/",
        "-H", "Cache-Control: no-cache",
        url,
    ]
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=75)
    html = p.stdout or ""
    if p.returncode != 0:
        raise RuntimeError(f"curl failed rc={p.returncode}: {(p.stderr or '')[:200]}")
    log(f"CURL OK, LEN {len(html)}")
    return html


def fetch(url: str) -> str:
    log(f"GET {url}")
    try:
        html = _fetch_with_requests(url)
    except Exception as e:
        log(f"  ↳ requests 실패: {type(e).__name__}: {e}")
        if "dgft.gov.in/CP/" not in url:
            raise
        html = _fetch_with_curl(url)

    # DGFT는 200이어도 차단/오류 HTML일 수 있으므로 내용 검증
    if "dgft.gov.in/CP/" in url and not _looks_like_dgft_notification_page(html):
        snippet = re.sub(r"\s+", " ", BeautifulSoup(html, "lxml").get_text(" ", strip=True))[:250]
        raise RuntimeError(f"DGFT Notification 표를 찾지 못함: {snippet}")
    return html


def fetch_with_fallback(urls: list) -> tuple:
    for url in urls:
        try:
            html = fetch(url)
            return html, url
        except Exception as e:
            log(f"  ↳ 실패, 다음 URL 시도: {type(e).__name__}: {str(e)[:160]}")
    return None, None


# ─────────────────────────────────────────────
# 파서
# ─────────────────────────────────────────────
def parse_dgtr(html: str) -> list:
    soup = BeautifulSoup(html, "lxml")
    items, seen_local = [], set()

    for a in soup.select('a[href*="/anti-dumping-cases/"]'):
        href = a.get("href", "").strip()
        title = a.get_text(" ", strip=True)
        if not href or not title:
            continue

        slug = href.rstrip("/").split("/")[-1]
        if not slug or slug in seen_local:
            continue

        seen_local.add(slug)
        href = urljoin("https://www.dgtr.gov.in", href)
        items.append({"uid": f"DGTR:{slug}", "title": title, "url": href})

    return items


def _normalize_dgft_url(href: str) -> str:
    """
    DGFT 메일 링크 정규화.
    - 공식 PDF(content.dgft.gov.in) 허용
    - dgft.gov.in 절대/상대 경로 허용
    - 예전 미러 slug 또는 도메인 없는 html 문자열은 버림
    """
    href = _clean(href)
    if not href:
        return ""

    low = href.lower()
    if low.startswith(("javascript:", "#", "mailto:")):
        return ""

    if href.startswith(("http://", "https://")):
        if "content.dgft.gov.in/" in low or "dgft.gov.in/" in low:
            return href
        return ""

    if href.startswith("/"):
        return urljoin("https://www.dgft.gov.in", href)

    # 공식 사이트 내부 상대경로만 보정
    if low.startswith(("website/", "cp/", "dgftprod/")):
        return urljoin("https://www.dgft.gov.in/", href)

    # 예: dgft-public-notice-no-...html 같은 미러/상대 slug는 버림
    return ""


def parse_dgft_official(html: str) -> list:
    """DGFT 공식 Notification 표 직접 파싱."""
    soup = BeautifulSoup(html, "lxml")
    items, seen_local = [], set()

    for tr in soup.find_all("tr"):
        tds = tr.find_all("td")
        if len(tds) < 5:
            continue

        cells = [_clean(td.get_text(" ", strip=True)) for td in tds]

        # 화면 구조:
        # 0 Sl.No, 1 Number, 2 Year, 3 Description, 4 Date, 5 CRT DT, 6 Attachment
        number = cells[1] if len(cells) > 1 else ""
        year = cells[2] if len(cells) > 2 else ""
        desc = cells[3] if len(cells) > 3 else ""
        date = cells[4] if len(cells) > 4 else ""

        if not re.search(r"\d", number):
            continue
        if not desc or len(desc) < 8:
            continue
        if not re.search(r"\d{2}/\d{2}/\d{4}", date):
            continue

        link = ""
        link_candidates = []

        # Attachment 컬럼 우선
        if len(tds) >= 6:
            link_candidates.extend(tds[-1].find_all("a", href=True))
        link_candidates.extend(tr.find_all("a", href=True))

        for a in link_candidates:
            link = _normalize_dgft_url(a.get("href", ""))
            if link:
                break

        uid = f"DGFT:official:{number}:{year}:{date}"
        if uid in seen_local:
            continue

        seen_local.add(uid)
        title = f"Notification {number} ({date}) - {desc}"
        items.append({
            "uid": uid,
            "title": title,
            "url": link,
            "number": number,
            "year": year,
            "date": date,
            "description": desc,
        })

    return items


# ─────────────────────────────────────────────
# 분류
# ─────────────────────────────────────────────
def classify(title: str, source: str) -> list:
    low = title.lower()
    hits = [k for k in KEYWORDS_PRODUCT if k in low]

    # DGFT는 Chapter 74뿐 아니라 QCO/BIS도 감시
    if source == "DGFT":
        hits += [k for k in KEYWORDS_REGIME if k in low]

    # 중복 제거, 순서 유지
    out = []
    for k in hits:
        if k not in out:
            out.append(k)
    return out


# ─────────────────────────────────────────────
# 알림
# ─────────────────────────────────────────────
def send_email(subject: str, body_html: str) -> None:
    if not (GMAIL_USER and GMAIL_APP_PASSWORD and NOTIFY_TO):
        log("⚠️ Gmail 설정 없음 → 이메일 생략 (로컬 테스트)")
        log(f"--- 미리보기 ---\n제목: {subject}\n{body_html[:700]}")
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


def send_telegram_text(text: str) -> None:
    """텔레그램 텍스트 알림. 설정 없거나 실패해도 모니터 실행을 실패시키지 않음."""
    token = os.environ.get("TELEGRAM_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        log("[텔레그램] 토큰/chat_id 없음 → 건너뜀")
        return

    api = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        requests.post(
            api,
            data={
                "chat_id": chat_id,
                "text": text[:3900],
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=30,
        ).raise_for_status()
        log(f"[텔레그램 발송] chat {chat_id}")
    except Exception as e:
        log(f"[텔레그램 전송 실패] {type(e).__name__}: {str(e)[:120]}")


def send_telegram_matches(results_by_source: dict) -> None:
    """현재 DGTR/DGFT item 구조에 맞춘 텔레그램 알림."""
    matched_rows = []
    for src in ("DGTR", "DGFT"):
        result = results_by_source.get(src, {})
        for it in result.get("matched", []):
            matched_rows.append((src, it))

    if not matched_rows:
        return

    lines = [f"🔴 <b>인도 무역규제 — 동관 관련 신규 {len(matched_rows)}건</b>"]
    for src, it in matched_rows[:20]:
        kws = ", ".join(it.get("keywords", []))
        title = escape(it.get("title", "")[:250])
        url = escape(it.get("url", ""), quote=True)
        lines.append(f"\n<b>[{src}]</b> {title}")
        lines.append(f"매칭: <code>{escape(kws)}</code>")
        if url:
            lines.append(url)

    send_telegram_text("\n".join(lines))


def build_email(results_by_source: dict) -> tuple:
    total = sum(len(v.get("matched", [])) for v in results_by_source.values())
    subject = f"🔴 [인도규제] 동관 관련 신규 {total}건 감지"
    parts = ["<h2>🔴 인도 무역규제 — 동관 관련 신규 감지</h2>"]

    label = {"DGTR": "DGTR 반덤핑 조사", "DGFT": "DGFT 공식 Notification"}

    # 항상 DGTR/DGFT 둘 다 표기
    for src in ("DGTR", "DGFT"):
        result = results_by_source.get(src, {})
        matched = result.get("matched", [])
        status = result.get("status", "ok")

        if status != "ok":
            parts.append(
                f"<h3>{label.get(src, src)} — 수집 실패</h3>"
                f"<p>접속 차단, 사이트 구조 변경, 일시 장애 가능성이 있습니다.</p>"
            )
            continue

        parts.append(f"<h3>{label.get(src, src)} — {len(matched)}건</h3>")

        if not matched:
            parts.append("<p>동관 관련 신규 감지 없음</p>")
            continue

        parts.append("<ul>")
        for it in matched:
            kw = ", ".join(it.get("keywords", []))
            title = escape(it.get("title", ""))
            url = it.get("url", "")

            parts.append(f"<li><b>{title}</b><br>매칭: <code>{escape(kw)}</code>")
            if url:
                safe_url = escape(url, quote=True)
                parts.append(f"<br><a href='{safe_url}'>{escape(url)}</a>")
            else:
                parts.append("<br><span style='color:#777'>첨부 링크 없음</span>")
            parts.append("<br><br></li>")
        parts.append("</ul>")

    parts.append(
        f"<hr><small>인도규제 모니터 · "
        f"{datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}</small>"
    )
    return subject, "\n".join(parts)


def send_structure_alert(src: str, count: int, threshold: int, streak: int, url: str) -> None:
    subject = f"⚠️ [{src} 모니터] 구조 깨짐 의심 — {count}건 (연속 {streak}회)"
    body = (
        f"<h2>⚠️ {src} 파서 이상</h2>"
        f"<p>파싱 {count}건 &lt; 임계치 {threshold}. 연속 {streak}회.</p>"
        f"<p>소스 구조/URL 변경 또는 접속 차단 가능성. 점검 필요.</p>"
        f"<p><a href='{escape(url, quote=True)}'>{escape(url)}</a></p>"
    )
    send_email(subject, body)
    send_telegram_text(
        f"⚠️ <b>{escape(src)} 모니터 구조 깨짐 의심</b>\n"
        f"파싱 {count}건 / 임계치 {threshold} / 연속 {streak}회\n"
        f"{escape(url)}"
    )


# ─────────────────────────────────────────────
# 메인
# ─────────────────────────────────────────────
def _run_parser(name: str, html: str) -> list:
    if name == "parse_dgtr":
        return parse_dgtr(html)
    if name == "parse_dgft_official":
        return parse_dgft_official(html)
    raise ValueError(f"unknown parser: {name}")


def process_source(src: str, cfg: dict, state: dict) -> dict:
    seen = set(state.get(src, []))
    html, used_url = fetch_with_fallback(cfg["urls"])

    if html is None:
        log(f"❌ [{src}] 모든 URL 수집 실패")
        streak = state["empty_streak"].get(src, 0) + 1
        state["empty_streak"][src] = streak
        send_structure_alert(src, 0, cfg["min_items"], streak, cfg["urls"][0])
        return {"status": "failed", "matched": [], "items_count": 0}

    items = _run_parser(cfg["parser"], html)
    count = len(items)
    log(f"[{src}] 파싱 {count}건")
    log(f"[{src}] 사용 URL: {used_url}")

    for it in items[:5]:
        log(f"    · {it['title'][:95]}")

    if count < cfg["min_items"]:
        streak = state["empty_streak"].get(src, 0) + 1
        state["empty_streak"][src] = streak
        log(f"⚠️ [{src}] {count}건 < 임계치 {cfg['min_items']} (연속 {streak}회)")
        send_structure_alert(src, count, cfg["min_items"], streak, used_url)
        return {"status": "failed", "matched": [], "items_count": count}

    state["empty_streak"][src] = 0

    new_items = [it for it in items if it["uid"] not in seen]
    if not new_items:
        log(f"[{src}] 변화 없음")
        return {"status": "ok", "matched": [], "items_count": count}

    matched = []
    for it in new_items:
        kws = classify(it["title"], src)
        if kws:
            it["keywords"] = kws
            matched.append(it)

    log(f"[{src}] 🆕 신규 {len(new_items)}건 (🔴 매칭 {len(matched)} / ⚪ 무관 {len(new_items)-len(matched)})")

    for it in new_items:
        seen.add(it["uid"])
    state[src] = sorted(seen)

    return {"status": "ok", "matched": matched, "items_count": count}


def main() -> int:
    state = load_state()
    results_by_source = {}

    for src, cfg in SOURCES.items():
        results_by_source[src] = process_source(src, cfg, state)

    total = sum(len(v.get("matched", [])) for v in results_by_source.values())
    any_failed = any(v.get("status") != "ok" for v in results_by_source.values())

    if total > 0:
        subject, body = build_email(results_by_source)
        send_email(subject, body)
        send_telegram_matches(results_by_source)
    elif any_failed:
        log("동관 관련 신규는 없지만 일부 소스 수집 실패")
        subject, body = build_email(results_by_source)
        subject = subject.replace("신규 0건 감지", "수집 실패/신규 0건")
        send_email(subject, body)
        send_telegram_text("⚠️ 인도 무역규제 모니터: 동관 관련 신규는 없지만 일부 소스 수집 실패")
    else:
        log("동관 관련 신규 없음 — 메일 없음")

    save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
