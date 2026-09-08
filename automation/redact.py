#!/usr/bin/env python3
"""Фильтр секретов: убирает пароли, ключи и токены из текста до того, как он
попадёт в репозиторий. Значение заменяется на [скрыто: пароль], окружающий текст
(логин, почта, кто кому писал) остаётся — факт «доступ передан» сохраняется,
сам доступ — нет. За ним идут в исходный чат по номеру сообщения.

Два слоя:
  1. слово-подсказка по-русски и по-английски, с опечатками: пароль, пасс, лог/пас,
     токен, ключ доступа, password, pwd, login/pass, credentials, api key, secret,
     private key, bearer… + значение в пределах 6 слов после него;
  2. узнаваемые форматы ключей без слова рядом: hex от 32 символов, sk-…, gho_…,
     JWT, токен бота Telegram, AKIA….

Проверка перед коммитом (ничего не меняет, код выхода 1, если нашёл):
    python automation/redact.py --check .
Вычистить на месте (*.md, *.txt, *.json):
    python automation/redact.py projects/o3/sources
"""
from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

PLACEHOLDER = "[скрыто: пароль]"

# Слой 1. Слово-подсказка. Русские формы с частыми опечатками, английские варианты.
_KEYWORD_RE = re.compile(
    r"(?<![\w/])(?:"
    r"парол[ьяеию]?\w*|порол[ьяеию]?\w*|пасв[оа]рд\w*|пассв[оа]рд\w*|пасс\b|пас\b|"
    r"лог[ие]?н?[ /\\]+пас\w*|л/п|"
    r"код[ \t]+доступа|ключ[ \t]+доступа|ключ[ \t]+api|апи[ \t]*ключ|"
    r"токен\w*|секрет\w*|"
    r"password|passwd|passcode|pwd|pw\b|pass(?:word)?\b|login[ /\\]+pass\w*|"
    r"credentials?|creds|"
    r"token|secret|client[ _-]?secret|private[ _-]?key|api[ _-]?key|api[ _-]?hash|"
    r"access[ _-]?key|auth[ _-]?(?:key|token)|bearer|session[ _-]?(?:key|id)"
    r")",
    re.IGNORECASE,
)
_SEP_RE = re.compile(r"^[\s:=\-—–«»\"'`]+")
_MAX_LOOKAHEAD = 6  # сколько слов после подсказки смотреть

# Слой 2. Форматы, которые секретны сами по себе.
_FORMAT_RES = (
    re.compile(r"\b[0-9a-f]{32,}\b", re.IGNORECASE),          # api_hash, md5, hex-токены
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"),                    # OpenAI-подобные ключи
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"),               # GitHub-токены
    re.compile(r"\beyJ[\w-]{10,}\.[\w-]{10,}\.[\w-]{10,}"),     # JWT
    re.compile(r"\b\d{8,10}:[A-Za-z0-9_-]{30,}"),              # токен бота Telegram
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),                       # AWS
)

_SPECIALS = set("!@#$%^&*()+=")  # дефис и подчёркивание — только вместе с цифрой («по-русски» — не пароль)
_HTML_ENTITY_RE = re.compile(r"^&#?\w+;$")
# Слова, которые часто стоят после «пароль» и не являются значением.
_STOPWORDS = frozenset({
    "от", "для", "к", "на", "в", "не", "нет", "же", "уже", "и", "или", "а", "но",
    "такой", "тот", "этот", "новый", "старый", "прежний", "клиент", "клиента",
    "сменил", "сменили", "поменял", "поменяли", "перестал", "подходит", "подошёл",
    "восстановить", "сбросить", "задаёт", "задал", "будет", "нужен", "нужно",
    "the", "is", "was", "to", "for", "of", "and", "or", "not", "reset", "change",
})


@dataclass
class Result:
    text: str
    count: int
    findings: list[str] = field(default_factory=list)  # только метки, без значений


def _looks_like_secret(token: str) -> bool:
    t = token.strip("«»\"'`.,;)(")
    if len(t) < 6 or t.lower() in _STOPWORDS or _HTML_ENTITY_RE.match(t):
        return False
    if PLACEHOLDER in t:
        return False
    has_alpha = any(c.isalpha() for c in t)
    has_digit = any(c.isdigit() for c in t)
    has_special = any(c in _SPECIALS for c in t) or (has_digit and ("-" in t or "_" in t))
    return (has_alpha and has_digit) or has_special


def _redact_line(line: str, findings: list[str]) -> str:
    # Слой 2 — форматы.
    for rx in _FORMAT_RES:
        if rx.search(line):
            line, n = rx.subn(PLACEHOLDER, line)
            findings.extend(["формат"] * n)

    # Слой 1 — слово-подсказка, потом до 4 слов вперёд на той же строке.
    pos = 0
    while True:
        m = _KEYWORD_RE.search(line, pos)
        if not m:
            break
        rest = line[m.end():]
        sep = _SEP_RE.match(rest)
        offset = m.end() + (sep.end() if sep else 0)
        tail = line[offset:]
        # В .json перенос строки лежит как два символа \n — это граница слова, не часть его.
        words = list(re.finditer(r"(?:\\(?![ntr])|[^\s\\])+", tail))[:_MAX_LOOKAHEAD]
        replaced = False
        for w in words:
            tok = w.group(0)
            if tok.lower().strip("«»\"'`.,;)(") in _STOPWORDS:
                continue
            if _looks_like_secret(tok):
                core = tok.strip("«»\"'`.,;)(")
                start = offset + w.start() + tok.find(core)
                line = line[:start] + PLACEHOLDER + line[start + len(core):]
                findings.append("слово")
                replaced = True
                pos = start + len(PLACEHOLDER)
                break
        if not replaced:
            pos = m.end()
    return line


def redact_text(text: str) -> Result:
    findings: list[str] = []
    lines = [_redact_line(ln, findings) for ln in text.split("\n")]
    return Result("\n".join(lines), len(findings), findings)


def redact_path(path: Path, check_only: bool = False) -> dict[str, int]:
    files = [path] if path.is_file() else sorted(
        p for p in path.rglob("*") if p.suffix in {".md", ".txt", ".json"}
        and ".venv" not in p.parts and ".git" not in p.parts
    )
    summary: dict[str, int] = {}
    for f in files:
        try:
            original = f.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        r = redact_text(original)
        if r.count:
            summary[str(f)] = r.count
            if not check_only:
                f.write_text(r.text, encoding="utf-8")
    return summary


def main(argv: list[str]) -> int:
    check = "--check" in argv
    args = [a for a in argv if a != "--check"]
    if len(args) != 1:
        print(__doc__)
        return 2
    summary = redact_path(Path(args[0]), check_only=check)
    if not summary:
        print("Секретов не найдено.")
        return 0
    for f, n in summary.items():
        print(f"{n:>4}  {f}")
    total = sum(summary.values())
    if check:
        print(f"Найдено {total} мест в {len(summary)} файлах. Не коммить: python automation/redact.py <путь>")
        return 1
    print(f"Скрыто {total} значений в {len(summary)} файлах.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
