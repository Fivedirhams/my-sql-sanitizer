"""CloakDB — Интерактивный мастер анонимизации SQL дампов

Usage:
    python -m cloaker              # Интерактивный мастер (по умолчанию)
    python -m cloaker --wizard     # Явный запуск мастера
    python -m cloaker --batch file.sql [-o out.sql]  # Batch (без вопросов)
    python -m cloaker --help       # Справка
"""

import sys
import time
from pathlib import Path
from typing import Dict, Any, Optional

from rich.console import Console
from rich.prompt import Prompt
from rich.table import Table
from rich.panel import Panel
from rich.text import Text
from rich.progress import (
    Progress, SpinnerColumn, TimeElapsedColumn, BarColumn,
    TextColumn, TaskID
)
from rich.live import Live

from cloaker.config import load_config
from cloaker.main import SQLProcessor, detect_sql_encoding, DumpNotAnonymizable

console = Console()


# ── Типы трансформеров для справки пользователя ────────────────
TRANSFORMER_INFO = {
    "name":        "👤 Имена людей (LLM — реалистичные замены)",
    "email":       "📧 Email адреса (сохранение доменов)",
    "phone":       "📱 Телефонные номера (сохранение формата)",
    "address":     "🏠 Адреса (улицы, города)",
    "date_shuffle_year":   "📅 Даты (сохранение распределения)",
    "date_shuffle_month":  "📅 Даты с месяцами",
    "title":       "💼 Должности/типулы",
    "company":     "🏢 Названия компаний",
    "genre":       "🎭 Категории/жанры (циклическая замена)",
    "composer":    "🎵 Композиторы (LLM)",
    "postal_code": "📮 Почтовые индексы (формат сохранён)",
    "skip":        "⏭ Пропуск (PK/FK, метрики)",
}


# Типы, чьи трансформеры обращаются к API (см. generate_* в cloaker/transformers/).
# Остальные (email, phone, postal_code, genre, date_shuffle_*, title-локальные,
# skip) детерминированные и сети не требуют.
LLM_BACKED_TYPES = ("name", "title", "company", "composer", "address")


# ── Меню выбора БД ──────────────────────────────────────────────
def show_dump_menu(examples_dir: Path) -> Optional[Path]:
    """Показать доступные SQL дампы и дать выбрать."""
    dumps = sorted(examples_dir.glob("*.sql"))

    if not dumps:
        console.print("[red]❌ Нет .sql файлов в examples/[/red]")
        console.print("   Скопируйте дамп в папку examples/ или укажите путь вручную.")
        return None

    table = Table(title="📁 Доступные SQL дампы")
    table.add_column("#", style="dim", width=3, justify="center")
    table.add_column("Файл", style="cyan")
    table.add_column("Размер", justify="right", style="green")

    for i, dump in enumerate(dumps, 1):
        size_kb = dump.stat().st_size / 1024
        size_str = f"{size_kb:.0f} KB" if size_kb < 1024 else f"{size_kb/1024:.1f} MB"
        table.add_row(str(i), dump.name, size_str)

    console.print(table)
    if len(dumps) == 1:
        # Демо-база одна — спрашивать не о чем, а лишний ввод в визарде
        # это лишняя точка падения (кривая консоль, EOF, не-UTF-8).
        return dumps[0]
    choices = [str(i) for i in range(1, len(dumps) + 1)]
    choice = Prompt.ask("Выберите базу для обработки", choices=choices, default="1")
    return dumps[int(choice) - 1]


# ── Экран классификации полей ──────────────────────────────────
def show_classification_table(selected_fields: Dict[str, dict]) -> None:
    """Показать таблицу авто-обнаруженных типов полей."""
    table = Table(title="🔍 Авто-обнаруженные типы полей")
    table.add_column("Поле", style="cyan", max_width=35)
    table.add_column("Тип", style="green", max_width=18)
    table.add_column("Сэмплов", justify="right", style="dim", width=8)
    table.add_column("Примечание", style="yellow")

    for field_key, info in sorted(selected_fields.items()):
        n = info.get("samples", 0) or 0
        note = info.get("note", "")

        if info["type"] == "skip":
            note = "🚫 PK/FK / метрика"

        table.add_row(field_key, info["type"], str(n), note)

    console.print(table)


def preview_generated_config(selected_fields: Dict[str, dict]) -> str:
    """Сгенерировать превью config.yaml на основе авто-детекта (формат Table.Column)."""
    # Группируем по таблице
    from collections import defaultdict
    by_table = defaultdict(dict)
    for field_key, info in selected_fields.items():
        parts = field_key.split('_', 1)
        if len(parts) != 2:
            continue
        table, column = parts
        by_table[table][column] = info['type']
    
    lines = [
        "# === Авто-сгенерированная конфигурация CloakDB ===",
        "# Проверьте типы перед запуском!",
        "",
        "transforms:",
    ]
    
    for table in sorted(by_table.keys()):
        cols = by_table[table]
        lines.append(f"  # ── {table} ──")
        for col in sorted(cols.keys()):
            t = cols[col]
            lines.append(f"  {table}.{col}: {t}")
    
    lines.extend(["", "processing: {}", "output: {}"])
    return "\n".join(lines)


def apply_generated_config(selected_fields: Dict[str, dict], config_path: Path) -> None:
    """Записать авто-конфиг в файл config.yaml."""
    content = preview_generated_config(selected_fields)
    config_path.write_text(content, encoding="utf-8")


# ── Интерактивный мастер ───────────────────────────────────────
def resolve_output_path(in_path: Path, config) -> Path:
    """Путь результата по умолчанию.

    Всегда кладём в каталог output/ (в контейнере examples/ и /input смонтированы
    read-only — писать рядом со входом нельзя). Каталог создаётся при необходимости.
    """
    out_dir = Path(config.profiles_dir).parent or Path(".")
    out_dir.mkdir(parents=True, exist_ok=True)
    return out_dir / f"{in_path.stem}_sanitized.sql"


def ask_yes_no(question: str, default_yes: bool = True) -> bool:
    """Да/нет числом: 1 — да, 2 — нет.

    Текстовый Confirm.ask требует ровно 'y'/'n' и на Windows-консоли с кириллицей
    или EOF валился traceback'ом посреди визарда. Число принимает Prompt с choices,
    а не-числовой ввод он переспрашивает сам.
    """
    console.print(question)
    return Prompt.ask("  Ответ (1 — да, 2 — нет)", choices=["1", "2"],
                      default="1" if default_yes else "2") == "1"


def run_interactive_wizard(examples_dir: Path, config_path: Path) -> bool:
    """
    Полноценный пошаговый мастер:
      1. Выбор БД
      2. Сканирование + Авто-классификация (с прогресс-баром)
      3. Просмотр конфига + Подтверждение / Правка
      4. Запуск санитайза (с Live прогрессом)
      5. Результаты
    """
    print_header()

    # ── Шаг 1: Выбор БД ─────────────────────────────────────────
    console.print("\n[bold cyan]═══ Шаг 1/4: Выбор базы данных ═══[/bold cyan]\n")
    dump_path = show_dump_menu(examples_dir)
    if not dump_path:
        return False

    console.print(f"[green]✓ Выбрано:[/green] {dump_path.name} "
                  f"({dump_path.stat().st_size / 1024:.0f} KB)\n")

    # ── Шаг 2: Профилирование + Авто-классификация ──────────────
    console.print("[bold cyan]═══ Шаг 2/4: Анализ структуры БД ═══[/bold cyan]\n")
    console.print("[dim]Загрузка конфигурации и настроек LLM...[/dim]")

    try:
        config = load_config(config_path)
    except Exception as e:
        console.print(f"[red]❌ Ошибка загрузки конфига: {e}[/red]")
        return False

    processor = SQLProcessor(config)

    # Кодировку определяем до первого чтения: шаг 2 читает файл напрямую, минуя
    # process_file, и без этого cp1251-выгрузка валилась здесь traceback'ом.
    processor._dump_encoding = detect_sql_encoding(dump_path)
    if processor._dump_encoding not in ("utf-8", "utf-8-sig"):
        console.print(
            f"[yellow]⚠  Дамп в кодировке {processor._dump_encoding} — "
            f"выход напишу в ней же, чтобы IMPORT не сломался[/yellow]\n")

    # Фаза 2A: Сбор сэмплов с ProgressBar
    console.print("[dim]Фаза 2a — Сканирование дамп-файла и сбор уникальных значений...[/dim]")
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(complete_style="green", finished_style="bold green"),
        TextColumn("{task.completed}/{task.total} полей"),
        TimeElapsedColumn(),
        console=console,
    ) as progress:
        task = progress.add_task("Сканирование...", total=None)
        raw_samples = processor._collect_samples(str(dump_path))
        progress.update(task, completed=True, visible=False)

    total_unique = sum(len(v) for v in raw_samples.values())
    console.print(f"\n[cyan]📊 Найдено:[/cyan] {len(raw_samples)} полей, "
                  f"{total_unique} уникальных значений\n")

    # Фаза 2B: Классификация
    console.print("[dim]Фаза 2b — Определение типа трансформера для каждого поля...[/dim]")
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(complete_style="blue", finished_style="bold blue"),
        TextColumn("{task.percentage:.0f}%"),
        TimeElapsedColumn(),
        console=console,
    ) as progress:
        task = progress.add_task("Классификация...", total=None)
        selected_fields = processor.select_transformers(raw_samples)
        progress.update(task, completed=True, visible=False)

    # Показать результаты классификации
    show_classification_table(selected_fields)

    # ── Шаг 3: Настройка конфига ────────────────────────────────
    console.print("\n[bold cyan]═══ Шаг 3/4: Настройка трансформеров ═══[/bold cyan]\n")

    config_text = preview_generated_config(selected_fields)
    console.print(Panel(
        config_text,
        title="📝 Предварительная конфигурация",
        subtitle="Проверьте типы трансформеров для каждого поля",
        border_style="yellow"
    ))

    # Информация о стоимости LLM. Считаем ЗАПРОСЫ, а не поля: в одно поле лезет
    # до sample_limit уникалов, в один запрос — до max_values_per_chunk. Раньше
    # визард обещал «~22 вызова» (по числу полей), а движок делал сотни — и шаг 4
    # выглядел как зависший.
    llm_fields = [fk for fk, info in selected_fields.items()
                  if info["type"] in LLM_BACKED_TYPES]
    local_fields = [fk for fk, info in selected_fields.items()
                    if info["type"] not in LLM_BACKED_TYPES]
    try:
        from cloaker.llm_client import LLMClient
        chunk = LLMClient(config).max_values_per_chunk
    except Exception:
        chunk = 20
    chunk = 20
    try:
        from cloaker.llm_client import LLMClient
        chunk = LLMClient(config).max_values_per_chunk
    except Exception:
        pass
    # Считаем по ВСЕМ уникальным значениям поля: через API проходит каждый
    # уникальный value, усечения нет (иначе часть значений не поменялась бы).
    llm_values = sum(len(raw_samples.get(fk, [])) for fk in llm_fields)
    est_calls = sum(-(-len(raw_samples.get(fk, [])) // chunk) for fk in llm_fields)

    if llm_fields:
        console.print(f"\n[dim]ℹ️  Через LLM: {len(llm_fields)} полей, "
                      f"{llm_values} уникальных значений → ~{est_calls} запросов "
                      f"(по ≤{chunk} значений)[/dim]")
        console.print(f"[dim]   💰 Примерная стоимость: ${est_calls * 0.002:.2f}, "
                      f"ожидаемо ~{est_calls * 25 / 60:.0f} мин (первый прогон)[/dim]")
        console.print("   ♻ Повторный прогон того же дампа читает маппинг с диска "
                      "(output/global_mapping.json) и сети почти не трогает")
    if local_fields:
        console.print(f"   ✅ {len(local_fields)} полей будут обработаны локально (без LLM)")

    # Вопрос: использовать конфиг по умолчанию (config.yaml) или редактировать?
    # Наш config.yaml уже правильно настроен для выбранной базы.
    use_default = ask_yes_no(
        "\n✅ Применить конфигурацию по умолчанию?\n"
        "   1 — да (использовать текущий config.yaml)\n"
        "   2 — нет (открыть config.yaml для редактирования)",
        default_yes=True
    )
    if not use_default:
        console.print(f"\n[yellow]✏️  Откройте файл config.yaml для ручной настройки.[/yellow]")
        console.print(f"   После редактирования запустите визард повторно.")
        console.print(f"   [bold]python -m cloaker[/bold]")
        return True

    # Финальное подтверждение запуска
    output_path = resolve_output_path(dump_path, config)
    proceed = ask_yes_no(
        f"\n▶ Запустить анонимизацию?\n"
        f"   Вход:   {dump_path.name}\n"
        f"   Выход:  {output_path.name}",
        default_yes=True
    )
    if not proceed:
        console.print("\n[yellow]Отменено пользователем — входной и выходной файлы не менялись.[/yellow]")
        return True   # намеренная остановка, не сбой

    # ── Шаг 4: Запуск санитайзинга с живым прогрессом ───────────
    console.print("\n[bold green]═══ Шаг 4/4: Запуск обработки ═══[/bold green]\n")

    console.print(f"  Вход:     {dump_path}")
    console.print(f"  Выход:    {output_path}")
    console.print(f"  Модель:   {config.llm.model}")
    console.print(f"  API:      {config.llm.endpoint}")
    console.print()

    try:
        # Запускаем процесс файла (он сам печатает прогресс внутри)
        row_count = processor.process_file(str(dump_path), str(output_path))

        output_size_mb = output_path.stat().st_size / 1024 / 1024

        # ── Экран результатов ─────────────────────────────────────
        console.print()
        console.print(Panel(
            f"[bold green]✅ Обработка завершена![/bold green]\n"
            f"Обработано строк: [bold]{row_count:,}[/bold]\n"
            f"Всего маппингов:  [bold]{len(processor.reg._mapping):,}[/bold]\n"
            f"Выходной файл:    [dim]{output_path} ({output_size_mb:.2f} MB)[/dim]",
            title="🎉 Готово!",
            border_style="green"
        ))

        console.print("\n[bold]Команды для проверки:[/bold]")
        console.print(f"  [dim]grep '^INSERT' {dump_path} | wc -l[/dim]         # Строки входа")
        console.print(f"  [dim]grep '^INSERT' {output_path} | wc -l[/dim]     # Строки выхода")
        console.print(f"  [dim]diff <(grep '^INSERT' {dump_path}) <(grep '^INSERT' {output_path}) | head -20[/dim]  # Разница")

        return True

    except KeyboardInterrupt:
        console.print("\n[yellow]⚠  Прервано пользователем[/yellow]")
        return False
    except DumpNotAnonymizable as e:
        console.print(f"\n[red]❌ Дамп не обезличен:[/red] {e}")
        console.print("[dim]Файл выхода не создавался (или удалён). "
                      "Выход — код возврата 1.[/dim]")
        return False
    except Exception as e:
        console.print(f"\n[red]❌ Ошибка процесса: {e}[/red]")
        console.print(f"[dim]{type(e).__name__}[/dim]")
        return False


# ── Batch режим (без вопросов) ──────────────────────────────────
def run_batch_mode(input_path: str, output_path: Optional[str], config_path: str) -> bool:
    """Быстрый запуск без мастера — для CI/CD и повторных прогонов."""
    config = load_config(config_path)
    processor = SQLProcessor(config)

    in_path = Path(input_path)
    out_path = Path(output_path) if output_path else resolve_output_path(in_path, config)

    console.print(f"[bold]🚀 CloakDB Batch Mode[/bold]\n")
    console.print(f"  Input:    {in_path}")
    console.print(f"  Output:   {out_path}")
    console.print(f"  Model:    {config.llm.model}")
    console.print()

    try:
        row_count = processor.process_file(str(in_path), str(out_path))
        out_size_mb = out_path.stat().st_size / 1024 / 1024

        console.print(f"\n[bold green]✅ Done! Processed {row_count:,} rows → {out_path} ({out_size_mb:.2f} MB)[/bold green]")
        return True

    except KeyboardInterrupt:
        console.print("\n[yellow]⚠ Interrupted[/yellow]")
        return False
    except DumpNotAnonymizable as e:
        console.print(f"\n[red]❌ Дамп не обезличен:[/red] {e}")
        return False
    except Exception as e:
        console.print(f"\n[red]❌ Error: {e}[/red]")
        return False


# ── Helpers ─────────────────────────────────────────────────────
def print_header() -> None:
    console.print(Panel(
        Text("MySQL Sanitizer", style="bold cyan"),
        subtitle="Интерактивный мастер",
        border_style="blue",
        padding=(0, 1)
    ))


# ── CLI Parser ──────────────────────────────────────────────────
def _force_utf8_stdio() -> None:
    """Guarantee UTF-8 I/O so the wizard never crashes on non-ASCII input.

    In a slim container the process locale may be C/POSIX, which makes
    sys.stdin decode incoming bytes with a non-UTF-8 codec and raise
    UnicodeDecodeError on Cyrillic prompts. Reconfiguring the streams to
    UTF-8 with a tolerant error handler makes input() robust regardless.
    """
    for name, errors in (("stdin", "ignore"), ("stdout", "replace"), ("stderr", "replace")):
        stream = getattr(sys, name, None)
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors=errors)
            except (ValueError, OSError, AttributeError):
                pass


def main() -> None:
    import argparse

    _force_utf8_stdio()

    parser = argparse.ArgumentParser(
        prog="cloakdb",
        description="CloakDB — Интерактивный & Batch анонимизатор SQL дампов",
        epilog="Примеры:\n"
               "  python -m cloaker                          # Интерактивный мастер\n"
               "  python -m cloaker --batch file.sql         # Без вопросов\n"
               "  python -m cloaker --batch file.sql -o out.sql  # С указанием вывода",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    parser.add_argument(
        "--wizard", "-w",
        action="store_true",
        help="Явный запуск интерактивного мастера (он и так режим по умолчанию; "
             "флаг принят, потому что обещан в README)"
    )
    parser.add_argument(
        "--batch", "-b",
        metavar="FILE",
        help="Batch mode: sanitize FILE without wizard prompts"
    )
    parser.add_argument(
        "-o", "--output",
        metavar="OUT",
        help="Output file path (default: <input>_sanitized.sql)"
    )
    parser.add_argument(
        "-c", "--config",
        default="config.yaml",
        help="Config file path (default: config.yaml)"
    )

    args = parser.parse_args()

    examples_dir = Path(__file__).parent.parent / "examples"
    config_path = Path(args.config)

    # ── Batch mode ──────────────────────────────────────────────
    if args.batch:
        if not Path(args.batch).exists():
            console.print(f"[red]❌ Файл не найден: {args.batch}[/red]")
            sys.exit(1)
        success = run_batch_mode(args.batch, args.output, config_path)
        sys.exit(0 if success else 1)

    # ── Interactive wizard (default) ────────────────────────────
    if not examples_dir.exists():
        console.print(f"[red]❌ Папка examples/ не найдена: {examples_dir}[/red]")
        sys.exit(1)

    try:
        success = run_interactive_wizard(examples_dir, config_path)
    except DumpNotAnonymizable as e:
        console.print(f"\n[red]❌ Дамп не обезличен:[/red] {e}")
        sys.exit(1)
    except Exception as e:
        # Мастер запускается человеком в терминале: вместо traceback'а посреди
        # шага — понятный текст и ненулевой код возврата.
        console.print(f"\n[red]❌ Мастер прервался:[/red] {type(e).__name__}: {e}")
        console.print("[dim]Тем же дампом, но без вопросов: "
                      "python -m cloaker --batch <файл.sql>[/dim]")
        sys.exit(1)
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
