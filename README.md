# BioPharmaBot 🧬

글로벌 바이오파마 뉴스 자동 수집 → Claude AI 분류/요약 → 텔레그램 전송

## 아키텍처

```
GitHub Actions (30분 cron)
    │
    ▼
Python 스크립트
    ├─ RSS 피드 체크 (GlobeNewswire, BioPharma Dive)
    ├─ 키워드 필터링
    ├─ 중복 체크 (seen_articles.json 캐시)
    ├─ Claude API 호출 (카테고리 분류 + 한국어 요약)
    └─ Telegram Bot API 전송
```

## 카테고리

| 이모지 | 카테고리 | 내용 |
|--------|----------|------|
| 🧬 | 임상시험 | Phase 1/2/3 결과, topline data |
| 🤝 | 딜 | M&A, 라이선스, 파트너십 |
| 💰 | 펀딩 | IPO, Series 라운드, PIPE |
| 🏛️ | 규제 | FDA 승인/거절, NDA/BLA |
| 📋 | 정책 | 약가, 관세, BARDA |

## 설정

### GitHub Secrets (필수)

| Secret | 설명 |
|--------|------|
| `ANTHROPIC_API_KEY` | Anthropic API 키 (`sk-ant-...`) |
| `TELEGRAM_BOT_TOKEN` | 텔레그램 봇 토큰 |
| `TELEGRAM_CHAT_ID` | 텔레그램 채널/채팅 ID |

### RSS 피드 추가

`bot.py`의 `RSS_FEEDS` 리스트에 추가:

```python
{
    "name": "피드 이름",
    "url": "RSS 피드 URL",
}
```

### 키워드 필터 수정

`bot.py`의 `FILTER_KEYWORDS` 리스트에서 추가/삭제

## 수동 실행

GitHub repo → Actions 탭 → "BioPharmaBot News Monitor" → "Run workflow"

## 향후 확장 예정

- [ ] DART Open API 연동 (국내 바이오 공시)
- [ ] SEC EDGAR 연동 (미국 filing 모니터링)
- [ ] 피드 소스 확대 (Fierce Biotech, STAT News 등)
- [ ] 일일 브리핑 요약 자동 생성
