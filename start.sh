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
if [[ ! -f ".env" ]] || ! grep -qE "^(LLM_API_KEY|LLM_API_TOKEN)=" .env; then
    echo "⚠️  ERROR: LLM API key not configured! Укажи LLM_API_KEY в .env"
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
    shift

    if [[ ! -f "$INPUT_DUMP" ]]; then
        echo "❌ Input dump not found: $INPUT_DUMP"
        echo "Use: ./start.sh examples/chinook_test.sql"
        exit 1
    fi

    # Хвост аргументов уходит в cloaker как есть. Раньше второй аргумент подставлялся
    # в `-o` буквально, и документированный запуск `./start.sh dump.sql -o out.sql`
    # превращался в `-o "-o"` — имя выходного файла терялось, а `out.sql` уезжал
    # неизвестным аргументом. Одинокий путь без флага по-прежнему значит `-o`.
    if [[ $# -eq 1 && "$1" != -* ]]; then
        set -- -o "$1"
    elif [[ $# -eq 0 ]]; then
        set -- -o output/sanitized.sql
    fi

    echo "============================================================"
    echo "CloakDB — Quick Mode (skip wizard)"
    echo "============================================================"
    echo ""
    echo "Input:   $INPUT_DUMP"
    echo "Output:  ${2:-output/sanitized.sql}"
    echo ""
    exec python3 -m cloaker --batch "$INPUT_DUMP" "$@"
fi
