"""
BioPharmaBot — 글로벌 바이오파마 뉴스 자동 수집·분류·요약·텔레그램 전송
소스: RSS 피드 + Gmail 알림
GitHub Actions cron으로 30분마다 실행
"""

import os
import json
import hashlib
import time
import re
import base64
from datetime import datetime, timezone
from pathlib import Path
from email.utils import parsedate_to_datetime

import feedparser
import requests

# ──────────────────────────────────────────────
# 환경변수
# ──────────────────────────────────────────────
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
GMAIL_TOKEN = os.environ.get("GMAIL_TOKEN", "")
GMAIL_CREDENTIALS = os.environ.get("GMAIL_CREDENTIALS", "")

# ──────────────────────────────────────────────
# RSS 피드 목록 (확장 시 여기에 추가)
# ──────────────────────────────────────────────
RSS_FEEDS = [
    {
        "name": "GlobeNewswire Biotech",
        "url": "https://www.globenewswire.com/RssFeed/industry/5011-Biotechnology/feedTitle/GlobeNewswire%20-%20Biotechnology",
    },
    {
        "name": "GlobeNewswire Pharma",
        "url": "https://www.globenewswire.com/RssFeed/industry/5013-Pharmaceuticals/feedTitle/GlobeNewswire%20-%20Pharmaceuticals",
    },
    {
        "name": "GlobeNewswire Clinical",
        "url": "https://www.globenewswire.com/RssFeed/subjectcode/90-Clinical%20Study/feedTitle/GlobeNewswire%20-%20Clinical%20Study",
    },
    {
        "name": "BioPharma Dive",
        "url": "https://www.biopharmadive.com/feeds/news/",
    },
    {
        "name": "Fierce Biotech",
        "url": "https://www.fiercebiotech.com/rss/xml",
    },
    {
        "name": "Fierce Pharma",
        "url": "https://www.fiercepharma.com/rss/xml",
    },
    {
        "name": "PRNewswire Pharma",
        "url": "https://www.prnewswire.com/rss/health-latest-news/pharmaceuticals-list.rss",
    },
]

# ──────────────────────────────────────────────
# 키워드 필터 (느슨하게 — 바이오파마 관련 기사를 넓게 포착)
# Claude가 최종 관련성 판단을 하므로, 여기서는 명백한 비관련만 걸러냄
# ──────────────────────────────────────────────
FILTER_KEYWORDS = [
    # 임상시험
    "phase 1", "phase 2", "phase 3", "phase i", "phase ii", "phase iii",
    "clinical trial", "clinical data", "topline", "primary endpoint",
    "overall survival", "progression-free", "pivotal", "registrational",
    "data readout", "interim analysis", "clinical hold", "dose-escalation",
    # 딜 / M&A
    "acquisition", "acquire", "merger", "license agreement", "partnership",
    "collaboration", "co-development", "option agreement", "royalt",
    "upfront", "milestone", "definitive agreement", "tender offer",
    # 펀딩
    "ipo", "series a", "series b", "series c", "series d", "series e",
    "funding", "financing", "raised", "capital raise", "pipe", "spac",
    "public offering", "convertible",
    # 규제
    "fda", "ema", "pmda", "mhra", "nmpa", "approved", "approval",
    "nda", "bla", "sNDA", "sBLA", "pdufa", "complete response",
    "breakthrough therapy", "fast track", "orphan drug", "priority review",
    "accelerated approval", "advisory committee", "adcom",
    # 정책
    "tariff", "drug pricing", "inflation reduction act", "ira",
    "barda", "executive order", "340b", "medicaid", "medicare",
    "price negotiation", "march-in", "patent dance", "biosimilar",
    # 일반 바이오파마 도메인
    "biotech", "biopharma", "pharmaceutical", "biologic",
    "oncology", "immuno-oncology", "gene therapy", "cell therapy",
    "antibody", "adc", "mrna", "rnai", "crispr", "car-t", "car t",
    "small molecule", "monoclonal", "bispecific", "radioligand",
]

# ──────────────────────────────────────────────
# 중복 체크용 파일 (GitHub Actions에서 캐시)
# ──────────────────────────────────────────────
SEEN_FILE = Path("seen_articles.json")
MAX_SEEN = 2000  # 최대 보관 개수 (오래된 것부터 삭제)


def load_seen() -> dict:
    """이미 처리한 기사 ID 로드"""
    if SEEN_FILE.exists():
        return json.loads(SEEN_FILE.read_text())
    return {}


def save_seen(seen: dict):
    """처리한 기사 ID 저장 (오래된 항목 정리)"""
    if len(seen) > MAX_SEEN:
        # timestamp 기준 오래된 것부터 삭제
        sorted_keys = sorted(seen, key=lambda k: seen[k])
        for k in sorted_keys[: len(seen) - MAX_SEEN]:
            del seen[k]
    SEEN_FILE.write_text(json.dumps(seen, ensure_ascii=False, indent=2))


def article_id(entry: dict) -> str:
    """기사 고유 ID 생성 (link 또는 title 기반 해시)"""
    raw = entry.get("link", "") or entry.get("title", "")
    return hashlib.md5(raw.encode()).hexdigest()


# ──────────────────────────────────────────────
# RSS 수집 + 필터링
# ──────────────────────────────────────────────
def _normalize_title(title: str) -> set[str]:
    """제목에서 핵심 단어만 추출 (소문자, 3글자 이상)"""
    import re
    words = re.findall(r'[a-z0-9]{3,}', title.lower())
    return set(words)


def _is_duplicate_title(new_title: str, existing_titles: list[str], threshold: float = 0.5) -> bool:
    """기존 기사 제목들과 비교하여 유사도가 threshold 이상이면 중복 판정"""
    new_words = _normalize_title(new_title)
    if not new_words:
        return False

    for existing in existing_titles:
        existing_words = _normalize_title(existing)
        if not existing_words:
            continue
        # Jaccard 유사도: 교집합 / 합집합
        overlap = len(new_words & existing_words)
        total = len(new_words | existing_words)
        if total > 0 and overlap / total >= threshold:
            return True
    return False


def fetch_and_filter() -> list[dict]:
    """모든 피드에서 기사 수집 → 키워드 필터링 → 중복 제거"""
    seen = load_seen()
    new_articles = []
    collected_titles = []  # 이번 실행에서 수집한 기사 제목 (교차 소스 중복 체크용)

    for feed_info in RSS_FEEDS:
        print(f"[RSS] Fetching: {feed_info['name']}")
        try:
            feed = feedparser.parse(feed_info["url"])
        except Exception as e:
            print(f"[RSS 오류] {feed_info['name']}: {e}")
            continue

        for entry in feed.entries:
            aid = article_id(entry)

            # URL 기반 중복 스킵
            if aid in seen:
                continue

            title = entry.get("title", "")

            # 제목 유사도 기반 교차 소스 중복 스킵
            if _is_duplicate_title(title, collected_titles):
                print(f"  → 중복 스킵: {title[:50]}...")
                seen[aid] = datetime.now(timezone.utc).isoformat()
                continue

            # 키워드 필터링 (title + summary/description)
            text = (title + " " + entry.get("summary", "")).lower()

            if any(kw in text for kw in FILTER_KEYWORDS):
                new_articles.append({
                    "id": aid,
                    "title": title,
                    "description": entry.get("summary", ""),
                    "link": entry.get("link", ""),
                    "source": feed_info["name"],
                    "published": entry.get("published", ""),
                })
                collected_titles.append(title)
                seen[aid] = datetime.now(timezone.utc).isoformat()

    save_seen(seen)
    print(f"[RSS] 신규 기사 {len(new_articles)}건 감지 (중복 제거 후)")
    return new_articles, collected_titles


# ──────────────────────────────────────────────
# Gmail 수집
# ──────────────────────────────────────────────
def _get_gmail_service():
    """Gmail API 서비스 객체 생성"""
    try:
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build

        if not GMAIL_TOKEN:
            print("[Gmail] GMAIL_TOKEN이 설정되지 않았습니다.")
            return None

        # 환경변수에서 token.json, credentials.json 복원
        token_data = json.loads(GMAIL_TOKEN)

        # credentials.json에서 client_id, client_secret 가져오기
        if GMAIL_CREDENTIALS:
            cred_data = json.loads(GMAIL_CREDENTIALS)
            installed = cred_data.get("installed", cred_data.get("web", {}))
            token_data["client_id"] = installed.get("client_id", token_data.get("client_id"))
            token_data["client_secret"] = installed.get("client_secret", token_data.get("client_secret"))

        creds = Credentials.from_authorized_user_info(token_data)

        # 토큰 만료 시 갱신
        if creds.expired and creds.refresh_token:
            from google.auth.transport.requests import Request
            creds.refresh(Request())

        return build("gmail", "v1", credentials=creds)

    except Exception as e:
        print(f"[Gmail 인증 오류] {e}")
        return None


def _extract_email_text(payload: dict) -> str:
    """Gmail 메시지 payload에서 텍스트 추출"""
    text = ""

    if payload.get("mimeType", "").startswith("text/plain"):
        data = payload.get("body", {}).get("data", "")
        if data:
            text = base64.urlsafe_b64decode(data).decode("utf-8", errors="ignore")
    elif payload.get("mimeType", "").startswith("text/html"):
        data = payload.get("body", {}).get("data", "")
        if data:
            html = base64.urlsafe_b64decode(data).decode("utf-8", errors="ignore")
            # 간단한 HTML 태그 제거
            text = re.sub(r'<[^>]+>', ' ', html)
            text = re.sub(r'\s+', ' ', text).strip()

    # 멀티파트인 경우 재귀
    for part in payload.get("parts", []):
        part_text = _extract_email_text(part)
        if part_text:
            text = part_text
            if payload.get("mimeType", "") != "text/html":
                break  # plain text 우선

    return text[:3000]  # Claude 입력 제한 고려


def fetch_gmail_articles(seen: dict, collected_titles: list[str]) -> list[dict]:
    """Gmail에서 최근 30분 내 바이오파마 관련 메일 수집"""
    service = _get_gmail_service()
    if not service:
        return []

    new_articles = []

    try:
        # 최근 1시간 내 메일 검색 (여유있게)
        query = "newer_than:20m"
        results = service.users().messages().list(
            userId="me", q=query, maxResults=20
        ).execute()

        messages = results.get("messages", [])
        if not messages:
            print("[Gmail] 최근 메일 없음")
            return []

        print(f"[Gmail] 최근 메일 {len(messages)}건 확인 중...")

        for msg_info in messages:
            msg_id = msg_info["id"]

            # 중복 체크
            if f"gmail_{msg_id}" in seen:
                continue

            # 메일 상세 조회
            msg = service.users().messages().get(
                userId="me", id=msg_id, format="full"
            ).execute()

            headers = {h["name"]: h["value"] for h in msg["payload"].get("headers", [])}
            subject = headers.get("Subject", "")
            sender = headers.get("From", "")
            date = headers.get("Date", "")

            # 본문 추출
            body = _extract_email_text(msg["payload"])

            # 키워드 필터링 (RSS와 동일한 키워드)
            text = (subject + " " + body).lower()
            if not any(kw in text for kw in FILTER_KEYWORDS):
                seen[f"gmail_{msg_id}"] = datetime.now(timezone.utc).isoformat()
                continue

            # 제목 유사도 기반 중복 체크 (RSS 기사와도 비교)
            if _is_duplicate_title(subject, collected_titles):
                print(f"  → Gmail 중복 스킵: {subject[:50]}...")
                seen[f"gmail_{msg_id}"] = datetime.now(timezone.utc).isoformat()
                continue

            new_articles.append({
                "id": f"gmail_{msg_id}",
                "title": subject,
                "description": body[:2000],
                "link": f"https://mail.google.com/mail/u/0/#inbox/{msg_id}",
                "source": f"Gmail ({sender[:30]})",
                "published": date,
            })
            collected_titles.append(subject)
            seen[f"gmail_{msg_id}"] = datetime.now(timezone.utc).isoformat()

        print(f"[Gmail] 신규 {len(new_articles)}건 감지 (키워드+중복 필터 후)")

    except Exception as e:
        print(f"[Gmail 오류] {e}")

    return new_articles


# ──────────────────────────────────────────────
# Claude API 호출
# ──────────────────────────────────────────────
SYSTEM_PROMPT = """너는 글로벌 바이오파마 뉴스 분석 봇이다. 입력된 영문 기사를 분석하여 아래 JSON 형식으로만 응답하라. JSON 외의 텍스트는 절대 포함하지 마라. 백틱이나 마크다운도 쓰지 마라.

## 출력 JSON 형식
{"category": "카테고리명", "headline": "핵심 요약 제목 (한국어, 1줄)", "summary": "한국어 요약 텍스트", "relevance": "high/medium/low/none"}

## 카테고리 분류 기준 (6개 중 1개 선택)
- "임상시험": Phase 1/2/3 결과, topline data, primary endpoint 달성/미달, 중간분석, 임상 중단
- "딜": M&A, 라이선스 계약, 공동개발, 파트너십, 옵션 계약 (upfront/milestone/royalty 금액 포함)
- "펀딩": IPO, Series A/B/C/D, PIPE, 공모, 전환사채, SPAC
- "규제": FDA 승인/거절/CRL, NDA·BLA 제출·수리, PDUFA 일정, 우선심사·혁신신약·희귀의약품 지정, EMA/PMDA 관련
- "정책": 약가 정책, 관세, 통상, 정부 지원(BARDA 등), 입법, 행정명령
- "무관": 바이오파마 업계와 직접 관련 없는 기사 (의료기기 단독, 병원 경영, 건강식품, 디지털헬스 단독, 인사 이동, 컨퍼런스 공지, 실적발표 단순 일정 등)

## relevance 판단 기준
- high: 대형 M&A($500M+), Phase 3 topline, FDA 승인/CRL, 주요 정책 변경
- medium: 중규모 딜($50M~500M), Phase 2 결과, FDA 지정, 시리즈 B+ 펀딩
- low: 소규모 초기 단계, 전임상, 소규모 펀딩, 비핵심 업데이트
- none: "무관" 카테고리인 경우 반드시 none으로 설정

## 요약 작성 규칙

### 필수 포함 정보 (카테고리별)
- 임상시험: 질환, 약물명, 임상 단계, 주요 endpoint 결과 수치, 통계적 유의성
- 딜: 당사자, 대상 약물/기술, 계약 금액(upfront + milestone + royalty), 계약 구조
- 펀딩: 기업명, 라운드, 조달 금액, 리드 투자자, 자금 용도
- 규제: 기업명, 약물명, 적응증, FDA 조치 내용, 일정
- 정책: 정책 주체, 내용, 영향받는 영역

### 표기 규칙
- 약물명(파이프라인): 성분명(코드명, 타겟/MOA)
- 약물명(승인/출시): 성분명(상품명, 타겟/MOA)
- 기업명 첫 등장: 영문 풀네임(거래소:티커)
- 기업명 재등장: (티커)
- 비상장 기업: 영문 풀네임(비상장, 국가)
- 금액: 원문 통화 그대로 (예: $1.2B, €500M)
- 주가 변동: 원문에 명시된 경우에만 포함

### 문체
- 사실 기반, 객관적 서술. 전망/의견/추측 배제
- 한국어 작성, 고유명사는 영문 유지
- 2~4문장으로 핵심만 요약"""


def call_claude(article: dict) -> dict | None:
    """Claude API로 기사 분류 + 요약"""
    user_message = (
        f"다음 바이오파마 뉴스 기사를 분석해주세요.\n\n"
        f"---\n"
        f"제목: {article['title']}\n"
        f"본문: {article['description']}\n"
        f"출처: {article['source']}\n"
        f"날짜: {article['published']}\n"
        f"---"
    )

    try:
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": "claude-sonnet-4-20250514",
                "max_tokens": 1024,
                "system": SYSTEM_PROMPT,
                "messages": [{"role": "user", "content": user_message}],
            },
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()

        # 응답 텍스트 추출
        text = data["content"][0]["text"].strip()
        # 혹시 백틱 감싸져 있으면 제거
        text = text.removeprefix("```json").removeprefix("```").removesuffix("```").strip()

        result = json.loads(text)
        return result

    except Exception as e:
        print(f"[Claude API 오류] {article['title'][:50]}... → {e}")
        return None


# ──────────────────────────────────────────────
# 텔레그램 전송
# ──────────────────────────────────────────────
CATEGORY_EMOJI = {
    "임상시험": "🧬",
    "딜": "🤝",
    "펀딩": "💰",
    "규제": "🏛️",
    "정책": "📋",
    "기타": "📰",
}

RELEVANCE_LABEL = {
    "high": "🔴 HIGH",
    "medium": "🟡 MID",
    "low": "⚪ LOW",
}


def send_telegram(article: dict, analysis: dict):
    """분석 결과를 텔레그램으로 전송"""
    cat = analysis.get("category", "기타")
    emoji = CATEGORY_EMOJI.get(cat, "📰")
    rel = analysis.get("relevance", "medium")
    rel_label = RELEVANCE_LABEL.get(rel, "⚪ LOW")

    # Gmail 소스면 Claude 생성 headline 사용, RSS면 원본 제목
    is_gmail = article.get("source", "").startswith("Gmail")
    title = analysis.get("headline", article["title"]) if is_gmail else article["title"]

    message = (
        f"<b>{title}</b>\n\n"
        f"{emoji} {cat}  |  {rel_label}\n\n"
        f"{analysis['summary']}"
    )

    if is_gmail:
        # 메일 본문에서 링크 추출 (http/https)
        urls = re.findall(r'https?://[^\s<>"\')\]]+', article.get("description", ""))
        # 불필요한 링크 제외 (gmail, google, unsubscribe 등)
        urls = [u for u in urls if not any(skip in u.lower() for skip in
                ["google.com", "gmail.com", "unsubscribe", "mailto", "list-manage",
                 "mailchimp", "click.notification", "tracking", "pixel"])]
        if urls:
            message += f"\n\n📌 원문: {urls[0]}"
    else:
        message += f"\n\n📌 원문: {article['link']}"

    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": message,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=10,
        )
        resp.raise_for_status()
        print(f"[Telegram] 전송 완료: {cat} / {article['title'][:40]}...")
    except Exception as e:
        print(f"[Telegram 오류] {e}")


# ──────────────────────────────────────────────
# 메인 실행
# ──────────────────────────────────────────────
def main():
    print(f"{'='*60}")
    print(f"[BioPharmaBot] 실행 시각: {datetime.now(timezone.utc).isoformat()}")
    print(f"{'='*60}")

    # 1) RSS 수집 + 필터링
    rss_articles, collected_titles = fetch_and_filter()

    # 2) Gmail 수집 + 필터링 (RSS와 중복 제거 공유)
    seen = load_seen()
    gmail_articles = fetch_gmail_articles(seen, collected_titles)
    save_seen(seen)

    # 3) 합치기
    all_articles = rss_articles + gmail_articles

    if all_articles:
        # 4) 각 기사에 대해 Claude 분석 + 텔레그램 전송
        for i, article in enumerate(all_articles):
            source_tag = "📧" if article["source"].startswith("Gmail") else "📡"
            print(f"\n{source_tag} [{i+1}/{len(all_articles)}] 분석 중: {article['title'][:60]}...")

            analysis = call_claude(article)
            if analysis is None:
                continue

            # relevance 필터: 무관(none) 기사는 드롭
            rel = analysis.get("relevance", "medium")
            cat = analysis.get("category", "기타")
            if rel == "none" or cat == "무관":
                print(f"  → 무관 기사, 스킵")
                continue

            send_telegram(article, analysis)

            # API rate limit 방지 (기사 간 1초 간격)
            if i < len(all_articles) - 1:
                time.sleep(1)

        print(f"\n[완료] RSS {len(rss_articles)}건 + Gmail {len(gmail_articles)}건 처리")
    else:
        print("[완료] 신규 기사 없음")

    print(f"\n{'='*60}")
    print("[BioPharmaBot] 실행 완료")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
