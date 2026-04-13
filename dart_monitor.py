"""
DART 공시 모니터링 모듈
코스피/코스닥 제약바이오 업종 전체 기업의 신규 공시를 감지하여 텔레그램으로 전송
"""

import os
import json
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests

DART_API_KEY = os.environ.get("DART_API_KEY", "")
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

# ──────────────────────────────────────────────
# 기업 리스트 관리
# ──────────────────────────────────────────────
CORP_LIST_FILE = Path("dart_corp_list.json")


def load_corp_list() -> list[dict]:
    """제약바이오 기업 리스트 로드"""
    if CORP_LIST_FILE.exists():
        return json.loads(CORP_LIST_FILE.read_text())
    return []


def build_corp_list_from_krx() -> list[dict]:
    """
    KRX에서 제약/바이오 업종 종목을 가져와 DART corp_code와 매칭.

    KRX 업종 분류:
    - 코스피: '의약품' (업종코드 기준)
    - 코스닥: '제약', '바이오' (업종코드 기준)

    이 함수는 초기 1회 또는 리스트 갱신 시에만 실행.
    """
    print("[DART] KRX에서 제약바이오 종목 리스트 수집 중...")

    pharma_bio_stocks = []

    # --- 코스피 의약품 업종 ---
    try:
        resp = requests.post(
            "http://data.krx.co.kr/comm/bldAttend498/getJsonData.cmd",
            data={
                "bld": "dbms/MDC/STAT/standard/MDCSTAT03901",
                "mktId": "STK",  # 코스피
                "trdDd": datetime.now().strftime("%Y%m%d"),
            },
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=15,
        )
        data = resp.json()
        for item in data.get("output", []):
            # 코스피 의약품 업종
            if "의약품" in item.get("IDX_IND_NM", ""):
                pharma_bio_stocks.append({
                    "stock_code": item.get("ISU_SRT_CD", ""),
                    "corp_name": item.get("ISU_ABBRV", ""),
                    "market": "KOSPI",
                })
    except Exception as e:
        print(f"[KRX 코스피 조회 오류] {e}")

    # --- 코스닥 제약/바이오 업종 ---
    try:
        resp = requests.post(
            "http://data.krx.co.kr/comm/bldAttend498/getJsonData.cmd",
            data={
                "bld": "dbms/MDC/STAT/standard/MDCSTAT03901",
                "mktId": "KSQ",  # 코스닥
                "trdDd": datetime.now().strftime("%Y%m%d"),
            },
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=15,
        )
        data = resp.json()
        for item in data.get("output", []):
            ind_name = item.get("IDX_IND_NM", "")
            if any(kw in ind_name for kw in ["제약", "바이오", "의약품"]):
                pharma_bio_stocks.append({
                    "stock_code": item.get("ISU_SRT_CD", ""),
                    "corp_name": item.get("ISU_ABBRV", ""),
                    "market": "KOSDAQ",
                })
    except Exception as e:
        print(f"[KRX 코스닥 조회 오류] {e}")

    print(f"[DART] KRX에서 {len(pharma_bio_stocks)}개 제약바이오 종목 확인")

    # --- DART 고유번호 매칭 ---
    # DART corpCode.xml에서 종목코드 → corp_code 매핑
    if not pharma_bio_stocks:
        print("[DART] KRX에서 종목을 가져오지 못했습니다. 수동 리스트를 사용하세요.")
        return []

    corp_code_map = _download_dart_corp_codes()
    if not corp_code_map:
        print("[DART] DART 고유번호를 가져오지 못했습니다.")
        return pharma_bio_stocks  # corp_code 없이라도 리스트 반환

    result = []
    matched = 0
    for stock in pharma_bio_stocks:
        code = stock["stock_code"]
        if code in corp_code_map:
            stock["corp_code"] = corp_code_map[code]["corp_code"]
            matched += 1
        result.append(stock)

    print(f"[DART] DART 고유번호 매칭 완료: {matched}/{len(result)}")

    # 파일로 저장
    CORP_LIST_FILE.write_text(json.dumps(result, ensure_ascii=False, indent=2))
    return result


def _download_dart_corp_codes() -> dict:
    """DART에서 전체 기업 고유번호 다운로드 → {종목코드: {corp_code, corp_name}} 매핑"""
    import zipfile
    import io
    import xml.etree.ElementTree as ET

    try:
        resp = requests.get(
            "https://opendart.fss.or.kr/api/corpCode.xml",
            params={"crtfc_key": DART_API_KEY},
            timeout=30,
        )
        resp.raise_for_status()

        # zip 파일 해제
        z = zipfile.ZipFile(io.BytesIO(resp.content))
        xml_data = z.read(z.namelist()[0])
        root = ET.fromstring(xml_data)

        result = {}
        for corp in root.findall("list"):
            stock_code = corp.findtext("stock_code", "").strip()
            if stock_code:  # 상장사만
                result[stock_code] = {
                    "corp_code": corp.findtext("corp_code", "").strip(),
                    "corp_name": corp.findtext("corp_name", "").strip(),
                }
        print(f"[DART] 전체 상장사 {len(result)}개 고유번호 로드")
        return result

    except Exception as e:
        print(f"[DART 고유번호 다운로드 오류] {e}")
        return {}


# ──────────────────────────────────────────────
# 공시 조회
# ──────────────────────────────────────────────
DART_SEEN_FILE = Path("dart_seen_filings.json")
MAX_DART_SEEN = 5000


def load_dart_seen() -> dict:
    if DART_SEEN_FILE.exists():
        return json.loads(DART_SEEN_FILE.read_text())
    return {}


def save_dart_seen(seen: dict):
    if len(seen) > MAX_DART_SEEN:
        sorted_keys = sorted(seen, key=lambda k: seen[k])
        for k in sorted_keys[: len(seen) - MAX_DART_SEEN]:
            del seen[k]
    DART_SEEN_FILE.write_text(json.dumps(seen, ensure_ascii=False, indent=2))


def fetch_dart_filings(corp_list: list[dict]) -> list[dict]:
    """
    DART API로 제약바이오 기업들의 신규 공시 조회.

    API 호출 최소화 전략:
    - corp_code 없이 전체 공시 검색 (날짜 범위로 제한)
    - 결과에서 우리 기업 리스트에 있는 것만 필터
    """
    seen = load_dart_seen()
    new_filings = []

    # 오늘 날짜 (KST 기준)
    kst = timezone(timedelta(hours=9))
    today = datetime.now(kst)
    # 검색 범위: 오늘 ~ 어제 (놓친 것 방지)
    bgn_de = (today - timedelta(days=1)).strftime("%Y%m%d")
    end_de = today.strftime("%Y%m%d")

    # corp_code 기반 빠른 조회를 위한 set
    our_corp_codes = {
        c["corp_code"] for c in corp_list if c.get("corp_code")
    }

    if not our_corp_codes:
        print("[DART] corp_code가 매칭된 기업이 없습니다.")
        return []

    # DART API는 corp_code 없이 조회 시 최대 3개월, 페이지당 100건
    # 전체 공시를 날짜로 조회 → 우리 기업 필터
    page_no = 1
    total_checked = 0
    MAX_PAGES = 20  # 안전장치

    print(f"[DART] {bgn_de}~{end_de} 전체 공시에서 제약바이오 필터링 중...")

    while page_no <= MAX_PAGES:
        try:
            resp = requests.get(
                "https://opendart.fss.or.kr/api/list.json",
                params={
                    "crtfc_key": DART_API_KEY,
                    "bgn_de": bgn_de,
                    "end_de": end_de,
                    "page_no": page_no,
                    "page_count": 100,
                    "sort": "date",
                    "sort_mth": "desc",
                },
                timeout=15,
            )
            data = resp.json()

            if data.get("status") != "000":
                print(f"[DART API] status={data.get('status')}, message={data.get('message')}")
                break

            items = data.get("list", [])
            if not items:
                break

            total_count = int(data.get("total_count", 0))

            for item in items:
                rcept_no = item.get("rcept_no", "")

                # 중복 스킵
                if rcept_no in seen:
                    continue

                # 우리 기업 리스트에 있는지 확인
                corp_code = item.get("corp_code", "")
                if corp_code not in our_corp_codes:
                    continue

                new_filings.append({
                    "rcept_no": rcept_no,
                    "corp_code": corp_code,
                    "corp_name": item.get("corp_name", ""),
                    "corp_cls": item.get("corp_cls", ""),  # Y=코스피, K=코스닥
                    "report_nm": item.get("report_nm", ""),
                    "rcept_dt": item.get("rcept_dt", ""),
                    "flr_nm": item.get("flr_nm", ""),  # 공시 제출인
                })
                seen[rcept_no] = datetime.now(timezone.utc).isoformat()

            total_checked += len(items)
            print(f"  페이지 {page_no}: {len(items)}건 확인 (누적 {total_checked}/{total_count})")

            # 모든 페이지 조회 완료 체크
            if total_checked >= total_count:
                break

            page_no += 1
            time.sleep(0.5)  # rate limit 방지

        except Exception as e:
            print(f"[DART API 오류] {e}")
            break

    save_dart_seen(seen)
    print(f"[DART] 제약바이오 신규 공시 {len(new_filings)}건 감지")
    return new_filings


# ──────────────────────────────────────────────
# 텔레그램 전송
# ──────────────────────────────────────────────
MARKET_LABEL = {
    "Y": "코스피",
    "K": "코스닥",
    "N": "코넥스",
    "E": "기타",
}


def send_dart_telegram(filing: dict):
    """DART 공시를 텔레그램으로 전송"""
    market = MARKET_LABEL.get(filing.get("corp_cls", ""), "")
    dart_url = f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={filing['rcept_no']}"

    message = (
        f"📢 <b>[DART 공시]</b>\n\n"
        f"<b>{filing['corp_name']}</b> ({market})\n"
        f"📄 {filing['report_nm']}\n"
        f"📅 {filing['rcept_dt']}\n\n"
        f"🔗 {dart_url}"
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
        print(f"[DART Telegram] 전송: {filing['corp_name']} - {filing['report_nm'][:30]}")
    except Exception as e:
        print(f"[DART Telegram 오류] {e}")


# ──────────────────────────────────────────────
# 메인 실행
# ──────────────────────────────────────────────
def is_market_hours() -> bool:
    """평일 장중(KST 08:30~18:00)인지 확인"""
    kst = timezone(timedelta(hours=9))
    now = datetime.now(kst)

    # 주말 체크 (0=월, 6=일)
    if now.weekday() >= 5:
        return False

    # 시간 체크 (08:30 ~ 18:00)
    current_minutes = now.hour * 60 + now.minute
    if current_minutes < 510 or current_minutes > 1080:  # 8:30=510, 18:00=1080
        return False

    return True


def run_dart_monitor():
    """DART 공시 모니터링 메인 함수"""
    if not DART_API_KEY:
        print("[DART] DART_API_KEY가 설정되지 않았습니다. DART 모니터링 스킵.")
        return

    # 평일 장중만 실행 (GitHub Actions 비용 절약)
    if not is_market_hours():
        kst = timezone(timedelta(hours=9))
        print(f"[DART] 장외 시간 — 스킵 (KST {datetime.now(kst).strftime('%Y-%m-%d %H:%M %A')})")
        return

    print(f"\n{'='*60}")
    print(f"[DART] 공시 모니터링 시작: {datetime.now(timezone.utc).isoformat()}")
    print(f"{'='*60}")

    # 1) 기업 리스트 로드 (없으면 KRX에서 빌드)
    corp_list = load_corp_list()
    if not corp_list:
        print("[DART] 기업 리스트 없음 → KRX에서 자동 생성")
        corp_list = build_corp_list_from_krx()
        if not corp_list:
            print("[DART] 기업 리스트 생성 실패. 수동으로 dart_corp_list.json을 추가하세요.")
            return

    corp_with_code = [c for c in corp_list if c.get("corp_code")]
    print(f"[DART] 모니터링 대상: {len(corp_with_code)}개 기업")

    # 2) 신규 공시 조회
    filings = fetch_dart_filings(corp_list)

    if not filings:
        print("[DART] 신규 공시 없음")
        return

    # 3) 텔레그램 전송
    for i, filing in enumerate(filings):
        send_dart_telegram(filing)
        if i < len(filings) - 1:
            time.sleep(0.3)

    print(f"[DART] {len(filings)}건 전송 완료")


if __name__ == "__main__":
    run_dart_monitor()
