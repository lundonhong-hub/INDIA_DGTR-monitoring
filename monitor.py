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
        # 공식 사이트만 사용. GitHub IP 차단 → ScraperAPI(인도 IP) 경유로 우회.
        "urls": ["https://www.dgft.gov.in/CP/?opt=notification"],
        "min_items": 5,   # 공식 1페이지 기본 10건, 최소 5건은 나와야 정상
        "parser": "parse_dgft_official",
        "via_scraperapi": True,   # 이 소스만 프록시 경유
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
    """
    state.json 로드.

    중요:
    - state.json이 없거나 []이면 "현재 화면을 기준선으로만 저장"해야 합니다.
      그렇지 않으면 GitHub Actions를 수동 실행할 때마다 현재 목록 전체가 신규로 재발송됩니다.
    - 예전 DGFT 미러 UID(DGFT:sg:...)만 남아 있으면 공식 DGFT UID로 1회 마이그레이션합니다.
    """
    default = {
        "DGTR": [],
        "DGFT": [],
        "empty_streak": {},
        "alert_state": {},
        "last_run": None,
        "_needs_baseline": True,
    }

    if not os.path.exists(STATE_FILE):
        log("state.json 없음 → 첫 실행 기준선 모드")
        return default

    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)

        # 아주 오래된 PIB식 [] state가 들어온 경우
        if isinstance(data, list):
            log("state.json이 []/list 형식 → 첫 실행 기준선 모드")
            return default

        if not isinstance(data, dict):
            log("state.json 형식 이상 → 첫 실행 기준선 모드")
            return default

        for k in ("DGTR", "DGFT"):
            data.setdefault(k, [])
            if not isinstance(data[k], list):
                data[k] = []

        data.setdefault("empty_streak", {})
        data.setdefault("alert_state", {})
        data.setdefault("last_run", None)
        data["_needs_baseline"] = False
        return data

    except Exception as e:
        log(f"⚠️ state.json 읽기 실패 → 첫 실행 기준선 모드: {e}")
        return default


def save_state(state: dict) -> None:
    # 내부 플래그는 파일에 저장하지 않음
    data = dict(state)
    data.pop("_needs_baseline", None)
    data.setdefault("DGTR", [])
    data.setdefault("DGFT", [])
    data.setdefault("empty_streak", {})
    data.setdefault("alert_state", {})
    data["last_run"] = datetime.datetime.now().isoformat()

    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    log("state.json 저장 완료")


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


SCRAPERAPI_KEY = os.environ.get("SCRAPERAPI_KEY", "")


def _fetch_direct(url: str) -> str:
    """DGTR 등 직접 접속 가능한 소스용."""
    headers = _headers_for(url)
    r = requests.get(url, headers=headers, timeout=45, allow_redirects=True)
    log(f"STATUS {r.status_code}, LEN {len(r.text)}")
    if r.status_code >= 400:
        raise requests.HTTPError(f"HTTP {r.status_code}", response=r)
    return r.text


def _fetch_via_scraperapi(url: str) -> str:
    """DGFT 공식 사이트용. 인도 IP로 대신 요청해 GitHub IP 차단을 우회."""
    if not SCRAPERAPI_KEY:
        raise RuntimeError("SCRAPERAPI_KEY 미설정 — GitHub Secrets에 등록 필요")
    log(f"ScraperAPI 경유 GET {url}")
    r = requests.get(
        "https://api.scraperapi.com/",
        params={
            "api_key": SCRAPERAPI_KEY,
            "url": url,
            "country_code": "in",   # 인도 IP
            "keep_headers": "true",
        },
        headers=DGFT_HEADERS,
        timeout=90,   # 프록시 경유라 여유있게
    )
    log(f"ScraperAPI STATUS {r.status_code}, LEN {len(r.text)}")
    if r.status_code >= 400:
        raise requests.HTTPError(f"ScraperAPI HTTP {r.status_code}: {r.text[:200]}", response=r)
    return r.text


def fetch(url: str, via_scraperapi: bool = False) -> str:
    log(f"GET {url}" + ("  [via ScraperAPI]" if via_scraperapi else ""))
    if via_scraperapi:
        html = _fetch_via_scraperapi(url)
    else:
        html = _fetch_direct(url)

    # DGFT는 200이어도 차단/오류 HTML일 수 있으므로 내용 검증
    if "dgft.gov.in/CP/" in url and not _looks_like_dgft_notification_page(html):
        snippet = re.sub(r"\s+", " ", BeautifulSoup(html, "lxml").get_text(" ", strip=True))[:250]
        raise RuntimeError(f"DGFT Notification 표를 찾지 못함: {snippet}")
    return html


def fetch_with_fallback(urls: list, via_scraperapi: bool = False) -> tuple:
    for url in urls:
        try:
            html = fetch(url, via_scraperapi=via_scraperapi)
            return html, url
        except Exception as e:
            log(f"  ↳ 실패: {type(e).__name__}: {str(e)[:200]}")
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

    try:
        send_email(subject, body)
    except Exception as e:
        log(f"⚠️ 구조 경고 메일 발송 실패: {type(e).__name__}: {str(e)[:120]}")

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


def _should_baseline_source(src: str, seen: set, state: dict) -> bool:
    """
    현재 수집된 목록을 '신규'로 보내지 않고 기준선으로만 저장해야 하는지 판단.
    - 첫 실행 / state.json 초기화
    - DGFT 미러 UID(DGFT:sg:...) → 공식 UID(DGFT:official:...) 전환 직후
    """
    if state.get("_needs_baseline"):
        return True
    if not seen:
        return True
    if src == "DGFT" and not any(str(x).startswith("DGFT:official:") for x in seen):
        log("[DGFT] 기존 state가 미러 UID만 보유 → 공식 UID 기준선 마이그레이션")
        return True
    if src == "DGTR" and not any(str(x).startswith("DGTR:") for x in seen):
        return True
    return False


def _should_send_structure_alert(state: dict, src: str, count: int, threshold: int, url: str) -> bool:
    """
    구조 깨짐 메일 반복 방지.
    같은 소스/같은 URL/같은 count에 대한 경고는 상태가 저장된 뒤에는 재발송하지 않습니다.
    """
    state.setdefault("alert_state", {})
    key = f"{src}|count={count}|threshold={threshold}|url={url}"
    prev = state["alert_state"].get(src)
    state["alert_state"][src] = key
    return prev != key


def process_source(src: str, cfg: dict, state: dict) -> dict:
    seen = set(state.get(src, []))
    html, used_url = fetch_with_fallback(cfg["urls"], via_scraperapi=cfg.get("via_scraperapi", False))

    if html is None:
        log(f"❌ [{src}] 모든 URL 수집 실패")
        streak = state.setdefault("empty_streak", {}).get(src, 0) + 1
        state["empty_streak"][src] = streak

        if _should_send_structure_alert(state, src, 0, cfg["min_items"], cfg["urls"][0]):
            send_structure_alert(src, 0, cfg["min_items"], streak, cfg["urls"][0])
        else:
            log(f"[{src}] 동일 구조 경고는 이미 발송됨 → 중복 메일 억제")

        return {"status": "failed", "matched": [], "items_count": 0}

    items = _run_parser(cfg["parser"], html)
    count = len(items)
    log(f"[{src}] 파싱 {count}건")
    log(f"[{src}] 사용 URL: {used_url}")

    for it in items[:5]:
        log(f"    · {it['title'][:95]}")

    if count < cfg["min_items"]:
        streak = state.setdefault("empty_streak", {}).get(src, 0) + 1
        state["empty_streak"][src] = streak
        log(f"⚠️ [{src}] {count}건 < 임계치 {cfg['min_items']} (연속 {streak}회)")

        if _should_send_structure_alert(state, src, count, cfg["min_items"], used_url):
            send_structure_alert(src, count, cfg["min_items"], streak, used_url)
        else:
            log(f"[{src}] 동일 구조 경고는 이미 발송됨 → 중복 메일 억제")

        return {"status": "failed", "matched": [], "items_count": count}

    # 정상 수집으로 회복되면 구조 경고 상태 초기화
    state.setdefault("empty_streak", {})[src] = 0
    state.setdefault("alert_state", {}).pop(src, None)

    # 첫 실행 또는 DGFT UID 체계 전환 직후에는 현재 목록을 기준선으로만 저장한다.
    # 이 단계에서 메일을 보내면 현재 표에 이미 있는 공고가 매 실행마다 신규처럼 보일 수 있다.
    if _should_baseline_source(src, seen, state):
        for it in items:
            seen.add(it["uid"])
        state[src] = sorted(seen)
        log(f"[{src}] 기준선 저장 {len(items)}건 → 이번 실행은 신규 알림 없음")
        return {"status": "ok", "matched": [], "items_count": count}

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

    # 매칭 여부와 무관하게 본 신규는 모두 seen에 넣는다.
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

    # 중요: 알림 발송보다 state 저장을 먼저 수행.
    # 메일/텔레그램 전송 중 예외가 나도 다음 실행에서 같은 항목을 다시 보내지 않기 위함.
    save_state(state)

    if total > 0:
        subject, body = build_email(results_by_source)
        try:
            send_email(subject, body)
        except Exception as e:
            log(f"⚠️ 신규 알림 메일 발송 실패: {type(e).__name__}: {str(e)[:120]}")
        send_telegram_matches(results_by_source)

    elif any_failed:
        # 구조 경고는 process_source()에서 전환 시점에만 보냄.
        # 여기서 추가 요약 메일을 보내면 같은 장애에 대해 메일이 2번씩 나간다.
        log("동관 관련 신규는 없지만 일부 소스 수집 실패 — 구조 경고 중복 발송은 억제")

    else:
        log("동관 관련 신규 없음 — 메일 없음")

    return 0


if __name__ == "__main__":
    sys.exit(main())
