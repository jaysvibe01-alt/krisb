# KT_KEY — 크트키 자동 매매 봇

> **운영 위치:** `C:\bots\KT_KEY`
> **전략:** 크트키 (Kris B = 크보나치) — 15분봉 RSI 30/70 + 장대 + 컨펌 + 다음봉 꼬리 진입
> **익절:** 크보나치 1.236 / 1:1 / 2.0 / 2.26 분할 청산
> **참고:** SB_BOT 운영 시스템 구조 차용 (전략 X)
> **연구 베이스:** `C:\전략분석\크트키_분석\krtky_bot` (알림 봇)

---

## 운영 모드

| 모드 | 환경변수 | 설명 |
|---|---|---|
| **ALERT** (기본) | — | 텔레그램 알림만, 주문 X. 안전. |
| **PAPER** | `KT_MODE=paper` | 가상 주문 + P&L 추적. 운영 검증용 (코덱스 권장). |
| **LIVE** | `KT_MODE=live` | Bitget USDT-M Futures 실주문. ⚠️ **DD throttle 자동 작동.** |

---

## 디렉터리 구조

```
C:\bots\KT_KEY\
├── README.md              # 본 문서
├── _secrets.py            # API 키 (TELEGRAM + BITGET, gitignore)
├── src/
│   ├── krtky_alert_bot.py # 알림 봇 (krtky_bot 에서 복사)
│   ├── krbonacci.py       # 크보나치 + 4분할
│   ├── rsi_alert_core.py  # RSI 상태머신
│   ├── bitget_demo_client.py  # paper 클라이언트 (SB_BOT 참고)
│   ├── bitget_live_client.py  # live 클라이언트 (SB_BOT 참고)
│   ├── risk_manager.py    # DD throttle + 안전장치 (신규)
│   ├── trading_engine.py  # 자동 매매 엔진 (신규)
│   ├── telegram_notify.py # 텔레그램 송신
│   ├── position_tracker.py # 포지션/PnL 추적
│   └── main.py            # 봇 메인 진입점
├── data/
│   ├── trade_log.jsonl    # 모든 거래 로그
│   ├── pnl_daily.json     # 일일 PnL
│   └── backtest/          # 백테스트 결과 캐시
├── logs/
│   └── bot.log
├── tests/
└── docs/
    ├── 04_크트키_봇_전략_확정본.md   # 전략 정의
    └── 07_계좌운영_가이드.md          # 운영 가이드
```

---

## 운영 정책 (확정)

### 종목 / 시드 / 리스크 (2026-05-17 코덱스 권장 — 10종 확장)
- **시드:** $300
- **레버리지:** 30x (사용자 청산 감수 명시)
- **거래당 리스크:** 3.5% ($10.5 손절) — 코덱스 권장은 paper 후 1.5-2% 부터
- **종목 (10종):** BTC / ETH / SOL / XRP / DOGE / LINK / ADA / AVAX / SUI / BNB
  - ★★ PREMIUM: Long/ETH, Long/SOL, **Long/DOGE (신규)**, **Long/LINK (신규)**
  - SKIP: Short/SOL
- **동시 포지션 한도:** 최대 2개 (상관 회피)
- **자본 활용:** 동시 2개 × 50% = 100% (포지션 사이즈 동적 결정)
- **진입 우선:** PREMIUM ★★ > BTC 컨펌 > avg R 高 > 상관 회피
- **일 평균 알림:** 약 2.55건 (10종) — 기존 4종 0.71건 대비 3.6배
- **텔레그램:** 기존 `@COSMICRAY_TCR_BOT` 채널 (chat=6892340636)

### DD Throttle (자동 작동, 끌 수 없음)
| 자본 손실 | 조치 |
|---|---|
| 0~-10% | 정상 (리스크 3.5%) |
| -10% ~ -15% | 신규 진입 절반 컷 (리스크 1.75%) |
| -15% ~ -20% | PAPER 모드 강제 전환 |
| **-20% 도달** | **완전 중단** + 텔레그램 알림 |
| 연속 SL 5회 | 24시간 진입 차단 |
| 한 종목 SL 3연속 | 그 종목 1주일 차단 |

### 빈도
- 1년 256 거래 / 일 평균 **0.71건** (4종목) → 3종목 = **0.62건/일**
- 알림+pre-alert 합치면 **일 1-2건**
- 시간대: 18-24시 KST 34%, 06-12시 25% (피크)

---

## Phase 별 진행

| Phase | 상태 | 내용 |
|---|---|---|
| **0** | ✅ | 디렉터리 + README |
| **1** | ⏳ | 알림 봇 복사 (현 `krtky_bot` → `KT_KEY/src/`) |
| **2** | ⏳ | Bitget 클라이언트 (live + demo) + `_secrets.py` |
| **3** | ⏳ | Risk Manager (DD throttle + 안전장치) |
| **4** | ⏳ | Trading Engine (자동 주문 + 분할 청산) |
| **5** | ⏳ | Paper mode 운영 1-2주 (50+ 체결 로그) |
| **6** | ⏳ | Live mode 시작 ($300 micro) |

---

## 안전 원칙

1. **`_secrets.py` 절대 깃에 올리지 않음** (`.gitignore` 처리)
2. **Bitget API 키 권한:** Futures Trading + Read 만. Withdraw 절대 X.
3. **샌드박스 / Isolated 모드** 우선 사용
4. **DD throttle 코드에 박혀있음** — 환경변수로 끌 수 없음
5. **종목별 SL 3연속 = 자동 차단** — 룰 위반 못함

---

## 백테스트 검증 상태 (연구 베이스 동일)

- 실제 청산 엔진 + 모든 비용 반영: +106R/년 (1% 리스크)
- Walk-forward 4분기 분할: 4/4 양수 ✅
- Bybit robustness: 5/7 일치 ✅
- 코덱스 최종 판정: **Conditional Yes**
- 1년 +100% 이상 신뢰도 **55-60%**, +150% 이상 **40-45%**, +200% 이상 **25-30%**

---

> 본 봇은 사용자 본인 매매 보조 도구이며, 청산 위험을 충분히 인지한 상태에서 운영한다.
> Kris B 본인이 슬라이드에서 강조한 "1추세 1회 진입 / OB 단독 진입 금지 / 3번째 테스트 금지" 원칙은 봇 룰 + 사용자 컨펌 모두에 적용된다.
