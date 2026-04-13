"""
BioPharmaBot — 글로벌 바이오파마 뉴스 자동 수집·분류·요약·텔레그램 전송
GitHub Actions cron으로 30분마다 실행
"""

import os
import json
import hashlib
import time
from datetime import datetime, timezone
from pathlib import Path

import feedparser
import requests

# ──────────────────────────────────────────────
# 환경변수
# ──────────────────────────────────────────────
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

# ──────────────────────────────────────────────
# RSS 피드 목록 (확장 시 여기에 추가)
# ──────────────────────────────────────────────
RSS_FEEDS = [
    {
        "name": "GlobeNewswire Healthcare",
        "url": "https://www.globenewswire.com/RSSFeed/subjectcode/14-Healthcare/feedTitle/GlobeNewswire - Healthcare",
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
        "url": "https://www.prnewswire.com/rss/news-releases-list.rss",
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
def fetch_and_filter() -> list[dict]:
    """모든 피드에서 기사 수집 → 키워드 필터링 → 중복 제거"""
    seen = load_seen()
    new_articles = []

    for feed_info in RSS_FEEDS:
        print(f"[RSS] Fetching: {feed_info['name']}")
        feed = feedparser.parse(feed_info["url"])

        for entry in feed.entries:
            aid = article_id(entry)

            # 중복 스킵
            if aid in seen:
                continue

            # 키워드 필터링 (title + summary/description)
            text = (
                entry.get("title", "") + " " + entry.get("summary", "")
            ).lower()

            if any(kw in text for kw in FILTER_KEYWORDS):
                new_articles.append({
                    "id": aid,
                    "title": entry.get("title", ""),
                    "description": entry.get("summary", ""),
                    "link": entry.get("link", ""),
                    "source": feed_info["name"],
                    "published": entry.get("published", ""),
                })
                seen[aid] = datetime.now(timezone.utc).isoformat()

    save_seen(seen)
    print(f"[RSS] 신규 기사 {len(new_articles)}건 감지")
    return new_articles


# ──────────────────────────────────────────────
# Claude API 호출
# ──────────────────────────────────────────────
SYSTEM_PROMPT = """너는 글로벌 바이오파마 뉴스 분석 봇이다. 입력된 영문 기사를 분석하여 아래 JSON 형식으로만 응답하라. JSON 외의 텍스트는 절대 포함하지 마라. 백틱이나 마크다운도 쓰지 마라.

## 출력 JSON 형식
{"category": "카테고리명", "summary": "한국어 요약 텍스트", "relevance": "high/medium/low/none"}

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

    message = (
        f"{emoji} <b>[{cat}]</b>  {rel_label}\n\n"
        f"{analysis['summary']}\n\n"
        f"📌 원문: {article['link']}"
    )

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
    articles = fetch_and_filter()

    if articles:
        # 2) 각 기사에 대해 Claude 분석 + 텔레그램 전송
        for i, article in enumerate(articles):
            print(f"\n[{i+1}/{len(articles)}] 분석 중: {article['title'][:60]}...")

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
            if i < len(articles) - 1:
                time.sleep(1)

        print(f"\n[RSS 완료] {len(articles)}건 처리")
    else:
        print("[RSS 완료] 신규 기사 없음")

    print(f"\n{'='*60}")
    print("[BioPharmaBot] RSS 실행 완료")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
