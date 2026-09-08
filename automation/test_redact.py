"""Проверка фильтра секретов: python -m pytest automation/test_redact.py -q"""
from redact import redact_text, PLACEHOLDER as P

SECRETS = [
    ("пароль: 58h6d6nXD(", "58h6d6nXD("),
    ("логин dny@a1prolab.ru пароль bo9DWXCDJVFk", "bo9DWXCDJVFk"),
    ("у нас такой пароль - vzdQmQ7YE-! и он не подходит", "vzdQmQ7YE-!"),
    ("пароль от почты: Ek6H3+ex4v", "Ek6H3+ex4v"),
    ("пасс Qw3rty!9 логин admin", "Qw3rty!9"),
    ("лог/пас admin / Zx9!kLm2", "Zx9!kLm2"),
    ("пороль для битрикса Xy7#pqr1", "Xy7#pqr1"),
    ("пасворд новый: Ab12cd34", "Ab12cd34"),
    ("токен бота 1234567890:AAHfz_kLmNoPqRsTuVwXyZ0123456789abcd", "AAHfz"),
    ("api_hash = 0123456789abcdef0123456789abcdef", "0123456789abcdef"),
    ("вот ключ sk-abcdefghijklmnop1234567890", "sk-abcdefgh"),
    ("Password: Summer2026!", "Summer2026!"),
    ("код доступа BNM2pr8", "BNM2pr8"),
    ("login/pass: admin / Tr0ub4dor&3", "Tr0ub4dor&3"),
    ("credentials for CRM: user1 / P@ssw0rd2026", "P@ssw0rd2026"),
    ("client_secret=abc123DEF456ghi", "abc123DEF456ghi"),
    ("Bearer x9Y8z7W6v5U4t3S2", "x9Y8z7W6v5U4t3S2"),
    ("the password for the mailbox is Ek6H3+ex4v", "Ek6H3+ex4v"),
]

KEEP = [
    "тут верно, пароль не подходит",
    "пароль клиент задаёт сам",
    "06.07 пароль перестал подходить, запрос без ответа",
    "восстановить пароль &nbsp; чт, 9 июл",
    "обсудили токены и ключи в целом",
    "пропуск на объект выдан",
    "we discussed the token budget for the model",
    "password reset link was sent to the client",
    "секреты в текстах (по-русски и по-английски)",
    "пароль что-то не подходит",
]


def test_secrets_hidden():
    for text, secret in SECRETS:
        r = redact_text(text)
        assert secret not in r.text, text
        assert P in r.text, text


def test_context_kept():
    r = redact_text("логин dny@a1prolab.ru пароль bo9DWXCDJVFk")
    assert "dny@a1prolab.ru" in r.text and "логин" in r.text


def test_narrative_unchanged():
    for text in KEEP:
        assert redact_text(text).text == text, text


def test_idempotent():
    for text, _ in SECRETS:
        once = redact_text(text).text
        assert redact_text(once).text == once


def test_json_escaped_newline_is_a_boundary():
    # Текст как в .json: после «пароли» идёт \n и обычное слово — скрывать нечего
    s = r"пароли\nзатрагивались в ТЗ, пароль: Qw3rty!9 и всё"
    r = redact_text(s)
    assert "затрагивались" in r.text
    assert "Qw3rty!9" not in r.text
    assert r.count == 1
