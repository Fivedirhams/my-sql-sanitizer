#!/bin/bash
# CloakDB — MySQL SQL Dump Sanitizer
# Usage:
#   ./start.sh                        # Интерактивный мастер (по умолчанию)
#   ./start.sh file.sql               # Быстрый режим (пропускает мастер)
#   ./start.sh file.sql -o out.sql    # С указанием вывода

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# ── Проверка API ключа ────────────────────────────────────────
if [[ ! -f ".env" ]] || ! grep -qE "^(LLM_API_KEY|KODIK_API_KEY)=" .env; then
    echo "⚠️  ERROR: LLM API key not configured (LLM_API_KEY или KODIK_API_KEY)!"
    echo "Run: cp .env.example .env && vim .env"
    exit 1
fi

mkdir -p "$(dirname "${OUTPUT_FILE:-output/sanitized.sql}")"

# ── Определяем режим запуска ──────────────────────────────────
if [[ $# -eq 0 ]]; then
    # === ИНТЕРАКТИВНЫЙ МАСТЕР (по умолчанию) ===
    echo "============================================================"
    echo "CloakDB — Интерактивный мастер анонимизации"
    echo "============================================================"
    echo ""
    echo "Запускается пошаговый мастер: выбор БД → проверка конфига → запуск"
    echo "Для быстрого режима: ./start.sh examples/chinook_test.sql"
    echo ""
    exec python3 -m cloaker
else
    # === БЫСТРЫЙ РЕЖИМ (для CI/CD и повторных прогонов) ===
    INPUT_DUMP="$1"
    OUTPUT_FILE="${2:-output/sanitized.sql}"

    if [[ ! -f "$INPUT_DUMP" ]]; then
        echo "❌ Input dump not found: $INPUT_DUMP"
        echo "Use: ./start.sh examples/chinook_test.sql"
        exit 1
    fi

    echo "============================================================"
    echo "CloakDB — Quick Mode (skip wizard)"
    echo "============================================================"
    echo ""
    echo "Input:   $INPUT_DUMP"
    echo "Output:  $OUTPUT_FILE"
    echo ""
    exec python3 -m cloaker --batch "$INPUT_DUMP" -o "$OUTPUT_FILE"
fi
