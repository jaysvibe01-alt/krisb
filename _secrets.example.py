"""크트키 봇 자격증명 템플릿.

⚠️ 이 파일을 복사해서 `_secrets.py` 로 만들고 실제 키 입력.
   `_secrets.py` 는 .gitignore 처리되어 커밋되지 않음.
"""

# ─── 텔레그램 (필수) ───
TELEGRAM_BOT_TOKEN = "YOUR_BOT_TOKEN_HERE"
TELEGRAM_CHAT_IDS = ["YOUR_CHAT_ID"]

# ─── Bitget USDT-M Futures API (자동매매용, Phase 2 이후) ───
# 권한: Futures Trading + Read. Withdraw 절대 X.
BITGET_API_KEY = ""
BITGET_API_SECRET = ""
BITGET_API_PASSPHRASE = ""

# ─── 운영 모드 ───
# "alert" : 알림만 (기본, 안전)
# "paper" : 가상 주문 + P&L 추적 (운영 검증용)
# "live"  : Bitget 실주문 (Phase 6 이후)
KT_MODE = "alert"
