#!/usr/bin/env bash
# AI 전화비서 데모 실행 스크립트. 이 파일 하나로 실행됩니다.
set -e
cd "$(dirname "$0")"

if ! command -v python3 >/dev/null 2>&1; then
  echo "python3이 필요합니다." >&2
  exit 1
fi

if ! python3 -c "import flask" 2>/dev/null; then
  echo "Flask를 설치합니다..."
  python3 -m pip install -q flask
fi

# 5007번 포트부터 비어 있는 포트를 찾아 실행 (충돌 시 자동 전환)
PORT="$(python3 - <<'EOF'
import socket
for p in range(5007, 5027):
    s = socket.socket()
    s.settimeout(0.2)
    try:
        s.connect(("127.0.0.1", p))
        s.close()
    except OSError:
        print(p)
        break
EOF
)"

echo "=================================================="
echo " 📞 AI 전화비서 데모"
echo " 👉 http://localhost:${PORT}"
echo "    (5007번이 사용 중이면 자동으로 다음 빈 포트로 실행됩니다)"
echo "    멈추려면 Ctrl+C"
echo "=================================================="
PORT="$PORT" python3 app.py
