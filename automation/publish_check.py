#!/usr/bin/env python3
"""Проверка перед публикацией в GitHub. Сначала исправить всё, что ✗, потом коммит.

    python automation/publish_check.py          # код выхода 1 — публиковать нельзя

Что проверяет:
  ✗ секреты в текстах (automation/redact.py --check)
  ✗ .env / *.session / .venv в индексе git
  ✗ .gitignore без нужных строк
  ✗ нет README.md в папке проекта или в automation/
  ✗ новый файл в корне репозитория
  ✗ скрипт (*.py, *.sh) вне automation/
  ✗ правка существующего файла в sources/ (сырьё не правится; дописать выгрузкой
    или скрыть секрет через redact.py — можно)
  ✗ репозиторий на GitHub открытый, а в projects/ есть данные проектов
  ✗ удаление строк в journal/ во вчерашних и более старых записях (журнал только
    дописывается; сегодняшняя запись — черновик, её править можно)
  ! status.md изменён, а дата «Обновлено» не сегодняшняя
  ! добавлен/удалён/переименован файл, а README его папки не тронут
  ! нет projects/_template — инструкции «cp -r projects/_template» не сработают
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
from redact import redact_path, redact_text  # noqa: E402

ROOT_ALLOWED = {"README.md", "CLAUDE.md", "AGENTS.md", "SETUP.md", ".gitignore",
                ".env.example", "LICENSE", "requirements.txt"}
GITIGNORE_MUST = (".env", "*.session", ".venv/")

errors: list[str] = []
warnings: list[str] = []


def git(*args: str) -> str:
    # quotepath=false — кириллица и пробелы в путях приходят как есть, а не в кавычках с \ooo
    return subprocess.run(["git", "-c", "core.quotepath=false", *args], cwd=ROOT,
                          capture_output=True, text=True).stdout


def has_head() -> bool:
    return subprocess.run(["git", "rev-parse", "--verify", "HEAD"], cwd=ROOT,
                          capture_output=True).returncode == 0


def changed_files() -> list[tuple[str, str]]:
    """[(статус, путь)] — рабочее дерево + индекс относительно HEAD, плюс новые файлы."""
    base = "HEAD" if has_head() else "4b825dc642cb6eb9a060e54bf8d69288fbee4904"  # пустое дерево
    out = git("diff", "--name-status", base)
    rows = []
    for line in out.splitlines():
        parts = line.split("\t")
        status = parts[0][0]
        path = parts[-1]
        rows.append((status, path))
    for path in git("ls-files", "--others", "--exclude-standard").splitlines():
        rows.append(("A", path))
    return rows


def check_secrets() -> None:
    found = redact_path(ROOT, check_only=True)
    for f, n in found.items():
        errors.append(f"секреты в тексте: {Path(f).relative_to(ROOT)} — {n} мест. Вычистить: python automation/redact.py \"{Path(f).relative_to(ROOT)}\"")


def check_forbidden_tracked() -> None:
    tracked = git("ls-files").splitlines() + git("diff", "--name-only", "--cached").splitlines()
    for path in set(tracked):
        if path == ".env" or path.endswith(".session") or path.startswith(".venv/"):
            errors.append(f"в git попал секретный файл: {path}. Убрать: git rm --cached \"{path}\"")


def check_gitignore() -> None:
    gi = ROOT / ".gitignore"
    text = gi.read_text(encoding="utf-8") if gi.exists() else ""
    missing = [m for m in GITIGNORE_MUST if m not in text]
    if missing:
        errors.append(f".gitignore без строк: {', '.join(missing)}")


def check_readmes() -> None:
    projects = ROOT / "projects"
    if projects.exists():
        for d in sorted(p for p in projects.iterdir() if p.is_dir()):
            if not (d / "README.md").exists():
                errors.append(f"нет README.md в {d.relative_to(ROOT)}/")
    if (ROOT / "automation").exists() and not (ROOT / "automation" / "README.md").exists():
        errors.append("нет automation/README.md")
    if projects.exists() and not (projects / "_template").is_dir():
        warnings.append("нет projects/_template — «cp -r projects/_template» из инструкций не сработает")


def check_remote_private() -> None:
    """Данные проектов — только в закрытом репозитории. Открытый допустим, пока в projects/ только _template и _example."""
    if not git("remote").strip():
        return
    r = subprocess.run(["gh", "repo", "view", "--json", "isPrivate,nameWithOwner"], cwd=ROOT,
                       capture_output=True, text=True)
    if r.returncode != 0:
        warnings.append("не удалось проверить, закрыт ли репозиторий на GitHub (gh repo view). "
                        "Проверь руками: страница репозитория → Private")
        return
    info = json.loads(r.stdout)
    projects = ROOT / "projects"
    has_data = projects.exists() and any(p.is_dir() and not p.name.startswith("_") for p in projects.iterdir())  # _template, _example — не данные
    if has_data and not info.get("isPrivate"):
        errors.append(f"репозиторий {info.get('nameWithOwner')} открытый, а в projects/ данные проектов. "
                      "Закрыть: gh repo edit --visibility private --accept-visibility-change-consequences")


def _sources_change_allowed(path: str) -> bool:
    """Правка сырья допустима, если строки только добавлены, скрыт секрет или обновилась шапка выгрузки."""
    if path.endswith(".json"):  # выгрузка .json: старые сообщения должны остаться
        try:
            old_ids = {m["id"] for m in json.loads(git("show", f"HEAD:{path}"))}
            new_ids = {m["id"] for m in json.loads((ROOT / path).read_text(encoding="utf-8"))}
            return old_ids <= new_ids
        except (ValueError, TypeError, KeyError):
            return False
    diff = git("diff", "-U0", "HEAD", "--", path)
    added = {ln[1:] for ln in diff.splitlines() if ln.startswith("+") and not ln.startswith("+++")}
    for ln in diff.splitlines():
        if not ln.startswith("-") or ln.startswith("---"):
            continue
        removed = ln[1:]
        if removed.startswith("Выгружено:") or removed.startswith("> "):
            continue  # шапка выгрузки; текст сообщения (отредактирован в Telegram или скрыт секрет)
        if redact_text(removed).text in added:
            continue
        return False
    return True


def _removed_lines_outside_today(old_text: str, diff: str, today: str) -> str | None:
    """Вернуть дату записи, из которой удалены строки, если запись не сегодняшняя."""
    old_lines = old_text.split("\n")
    entry_date_at: list[str | None] = []
    current = None
    for ln in old_lines:
        m = re.match(r"^## (\d{4}-\d{2}-\d{2})", ln)
        if m:
            current = m.group(1)
        entry_date_at.append(current)
    # В unified diff с -U0 заголовок ханка: @@ -старт[,длина] +... @@
    for h in re.finditer(r"^@@ -(\d+)(?:,(\d+))? \+\d+(?:,\d+)? @@", diff, re.M):
        start, length = int(h.group(1)), int(h.group(2) or 1)
        if length == 0:
            continue  # только добавление
        for i in range(start - 1, min(start - 1 + length, len(entry_date_at))):
            d = entry_date_at[i]
            if d and d != today:
                return d
    return None


def check_changes() -> None:
    rows = changed_files()
    touched_dirs = {str(Path(p).parent) for _, p in rows}
    today = date.today().isoformat()
    for status, path in rows:
        p = Path(path)
        if len(p.parts) == 1 and status == "A" and p.name not in ROOT_ALLOWED:
            errors.append(f"новый файл в корне: {path}. Корень — только для правил и README")
        if p.suffix in {".py", ".sh"} and p.parts[0] != "automation":
            errors.append(f"скрипт вне automation/: {path}")
        if "sources" in p.parts and status == "M" and has_head() and not _sources_change_allowed(path):
            errors.append(f"правка сырья: {path}. sources/ не правится — вернуть (git checkout -- <файл>), "
                          "новое положить новым файлом. Можно только: дописать выгрузкой, скрыть секрет redact.py")
        if ("journal" in p.parts and status == "M" and has_head() and p.name != "README.md"
                and not (p.parts[0] == "projects" and p.parts[1].startswith("_"))):  # README и учебные _example — не журналы
            old = git("show", f"HEAD:{path}")
            removed_in_old_entries = _removed_lines_outside_today(old, git("diff", "-U0", "HEAD", "--", path), today)
            if removed_in_old_entries:
                errors.append(f"удалены строки в журнале: {path} — в записи от {removed_in_old_entries}. "
                              f"Старые записи не правятся, уточнение — новой записью «со слов …, {today}»")
        if p.name == "status.md" and status in "AM":
            text = (ROOT / path).read_text(encoding="utf-8") if (ROOT / path).exists() else ""
            m = re.search(r"Обновлено:\s*(\d{4}-\d{2}-\d{2})", text)
            if not m or m.group(1) != today:
                warnings.append(f"{path}: дата «Обновлено» не сегодняшняя ({m.group(1) if m else 'нет'})")
        if status in "ADR" and p.name != "README.md" and len(p.parts) > 1 and p.parts[0] != ".claude":
            readme = str(p.parent / "README.md")
            if readme not in {pp for _, pp in rows} and not (p.parent.name in {"sources", "journal"}):
                warnings.append(f"{status} {path} — README папки не тронут ({readme})")


def main() -> int:
    check_secrets()
    check_forbidden_tracked()
    check_gitignore()
    check_readmes()
    check_remote_private()
    check_changes()
    for w in warnings:
        print(f"!  {w}")
    for e in errors:
        print(f"✗  {e}")
    if errors:
        print(f"\nПубликовать нельзя: {len(errors)} ошибок. Сначала исправить, потом коммит.")
        return 1
    print(f"Проверка пройдена{' (' + str(len(warnings)) + ' предупреждений)' if warnings else ''}. Можно коммитить.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
