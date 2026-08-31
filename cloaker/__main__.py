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
from rich.prompt import Prompt, Confirm
from rich.table import Table
from rich.panel import Panel
from rich.text import Text
from rich.progress import (
    Progress, SpinnerColumn, TimeElapsedColumn, BarColumn,
    TextColumn, TaskID
)
from rich.live import Live

from cloaker.config import load_config
from cloaker.main import SQLProcessor

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
    "crm_status":  "⚙️ Бизнес-статусы/энумы (циклическая)",
    "skip":        "⏭ Пропуск (PK/FK, ИНН, метрики)",
}


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
    """Сгенерировать превью config.yaml на основе авто-детекта."""
    lines = [
        "# === Авто-сгенерированная конфигурация CloakDB ===",
        "# Проверьте типы перед запуском!",
        "",
        "transforms:",
    ]
    for fk, info in sorted(selected_fields.items()):
        typ = info["type"]
        line = f"  {fk}: {typ}"
        if info.get("note"):
            line += f"  #{info['note']}"
        lines.append(line)
    return "\n".join(lines)


# ── Интерактивный мастер ───────────────────────────────────────
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

    # Информация о стоимости LLM
    llm_types = [t for t, info in selected_fields.items()
                 if info["type"] not in ("skip", "genre", "crm_status", "date_shuffle_year", "date_shuffle_month")]
    local_types = [t for t, info in selected_fields.items()
                   if info["type"] in ("skip", "genre", "crm_status", "date_shuffle_year", "date_shuffle_month")]

    if llm_types:
        console.print(f"\n[dim]ℹ️  Будет сделано ~{len(llm_types)} вызовов к LLM API[/dim]")
        console.print(f"   💰 Примерная стоимость: $~{len(llm_types) * 0.002:.2f}")
    if local_types:
        console.print(f"   ✅ {len(local_types)} полей будут обработаны локально (без LLM)")

    # Вопрос: использовать авто-конфиг?
    use_auto = Confirm.ask(
        "\n✅ Использовать автоматически подобранные типы?\n"
        "   (Отвечайте 'n' чтобы отредактировать config.yaml вручную)",
        default=True
    )
    if not use_auto:
        console.print(f"\n[yellow]✏️  Откройте файл config.yaml для ручной настройки.[/yellow]")
        console.print(f"   После редактирования запустите повторно:")
        console.print(f"   [bold]python -m cloaker[/bold]")
        return False

    # Финальное подтверждение запуска
    output_path = dump_path.with_suffix("_sanitized.sql")
    proceed = Confirm.ask(
        f"\n▶ Запустить анонимизацию?\n"
        f"   Вход:   {dump_path.name}\n"
        f"   Выход:  {output_path.name}",
        default=True
    )
    if not proceed:
        console.print("\n[yellow]Отменено пользователем.[/yellow]")
        return False

    # ── Шаг 4: Запуск санитайзинга с живым прогрессом ───────────
    console.print("\n[bold green]═══ Шаг 4/4: Запуск обработки ═══[/bold cyan]\n")

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
    out_path = Path(output_path) if output_path else in_path.with_suffix("_sanitized.sql")

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
def main() -> None:
    import argparse

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

    success = run_interactive_wizard(examples_dir, config_path)
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
