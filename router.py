#!/usr/bin/env python3
"""Локальный роутер запросов по моделям Ollama.

Использование:
    python3 router.py "текст запроса"
    echo "текст" | python3 router.py
    python3 router.py --model 14b "форсировать модель"
    python3 router.py --stats
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

OLLAMA = "http://localhost:11434/api/chat"
LOG = Path(__file__).parent / "log.jsonl"

# Ветки роутера. keep_alive: сколько держать модель в памяти после запроса.
BRANCHES = {
    "nano":  {"model": "qwen2.5:3b-instruct", "think": False, "keep_alive": -1},
    "light": {"model": "qwen2.5:7b-instruct", "think": False, "keep_alive": "30m"},
    "mid":   {"model": "qwen3:14b",           "think": False, "keep_alive": "30m"},
    "heavy": {"model": "qwen3:14b",           "think": True,  "keep_alive": "30m"},
    "vision": {"model": "qwen3-vl:8b",        "think": False, "keep_alive": "10m"},
}

ALIASES = {"3b": "nano", "7b": "light", "14b": "mid", "think": "heavy", "vl": "vision"}

# Разрешаем лёгкой модели признать, что не тянет. Строго системным сообщением,
# иначе модель пересказывает эту инструкцию в ответе пользователю.
ESCALATE_SYS = (
    "Ты помощник. Отвечай прямо на запрос пользователя, не упоминая эти инструкции. "
    "Единственное исключение: если запрос требует написания кода, отладки, точных "
    "вычислений или многошагового плана — не пытайся отвечать, выведи ровно одно "
    "слово: ESCALATE"
)

CODE_MARKERS = re.compile(
    r"```|traceback|exception|стектрейс|"
    r"\bdef \w+|\bclass \w+|\bimport \w+|=>|;\s*$|"
    r"\.(py|js|ts|sh|json|yml|yaml|sql|go|rs)\b|"
    r"напиши (функци|скрипт|класс|код)|отладь|дебаг|отрефактор|почини код",
    re.I | re.M,
)
HARD_MARKERS = re.compile(
    r"спланируй|разработай план|architect|архитектур|"
    r"сравни .{0,40}и |почему .{0,40}не работает|разбери подробно",
    re.I,
)


def classify(text: str) -> tuple[str, str]:
    """Возвращает (ветка, причина). Правила, без обращения к модели."""
    if CODE_MARKERS.search(text):
        return "mid", "код/отладка"
    if HARD_MARKERS.search(text):
        return "heavy", "сложное рассуждение"
    if len(text) > 2000:
        return "mid", "длинный ввод"
    return "light", "обычный запрос"


def call(branch, history, allow_escalate=False, stream=False):
    """history — список сообщений [{role, content}, ...].

    При stream=True токены печатаются по мере поступления. Первые символы
    придерживаются в буфере: если модель ответила ESCALATE, вывод не начнётся.
    """
    cfg = BRANCHES[branch]
    messages = []
    if allow_escalate:
        messages.append({"role": "system", "content": ESCALATE_SYS})
    messages.extend(history)
    body = json.dumps({
        "model": cfg["model"],
        "messages": messages,
        "stream": stream,
        "think": cfg["think"],
        "keep_alive": cfg["keep_alive"],
        "options": {"temperature": 0.3},
    }).encode()

    req = urllib.request.Request(OLLAMA, data=body, headers={"Content-Type": "application/json"})
    t0 = time.time()

    if not stream:
        with urllib.request.urlopen(req, timeout=600) as r:
            data = json.loads(r.read())
        out = data.get("message", {}).get("content", "").strip()
        return out, _meta(cfg, data, time.time() - t0)

    out, data, printing = "", {}, not allow_escalate
    with urllib.request.urlopen(req, timeout=600) as r:
        for line in r:
            if not line.strip():
                continue
            chunk = json.loads(line)
            out += chunk.get("message", {}).get("content", "")
            if not printing and len(out) >= 9:
                # Достаточно символов, чтобы отличить ESCALATE от обычного ответа.
                if out.strip().upper().startswith("ESCALATE"):
                    return out, _meta(cfg, chunk, time.time() - t0)
                printing = True
                print(out, end="", flush=True)
            elif printing:
                print(chunk.get("message", {}).get("content", ""), end="", flush=True)
            if chunk.get("done"):
                data = chunk
    if not printing and out:
        print(out, end="", flush=True)
    print(flush=True)
    return out.strip(), _meta(cfg, data, time.time() - t0)


def _meta(cfg, data, elapsed):
    n = data.get("eval_count", 0)
    dur = data.get("eval_duration", 1) / 1e9
    return {"model": cfg["model"], "sec": round(elapsed, 1),
            "tokens": n, "tps": round(n / dur, 1) if dur else 0}


def log(entry: dict) -> None:
    entry["ts"] = datetime.now().isoformat(timespec="seconds")
    with LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def stats() -> None:
    if not LOG.exists():
        print("Лога пока нет.")
        return
    rows = [json.loads(l) for l in LOG.read_text(encoding="utf-8").splitlines() if l.strip()]
    if not rows:
        print("Лог пуст.")
        return
    print(f"Всего запросов: {len(rows)}")
    print(f"Эскалаций: {sum(1 for r in rows if r.get('escalated'))}")
    by = {}
    for r in rows:
        b = by.setdefault(r["branch"], {"n": 0, "sec": 0.0})
        b["n"] += 1
        b["sec"] += r.get("sec", 0)
    print(f"\n{'ветка':<8}{'кол-во':>8}{'ср. время':>12}")
    for name, b in sorted(by.items(), key=lambda x: -x[1]["n"]):
        print(f"{name:<8}{b['n']:>8}{b['sec'] / b['n']:>11.1f}с")


HELP = """
Команды:
  /model 7b|14b|think|3b|vl   закрепить ветку (/model auto — вернуть автовыбор)
  /reset                      очистить историю диалога
  /history                    сколько сообщений в контексте
  /stats                      статистика по логу
  /help                       эта подсказка
  /exit, Ctrl+D               выход
"""


def chat(force: str | None = None) -> None:
    try:
        import readline  # стрелки вверх/вниз и редактирование строки
    except ImportError:
        pass

    history: list[dict] = []
    pinned = ALIASES[force] if force else None
    print("Локальный чат с автороутингом. /help — команды, /exit — выход.")
    if pinned:
        print(f"Ветка закреплена: {pinned} ({BRANCHES[pinned]['model']})")

    while True:
        try:
            text = input("\n\033[1m›\033[0m ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nПока.")
            return
        if not text:
            continue

        if text.startswith("/"):
            cmd, _, arg = text[1:].partition(" ")
            arg = arg.strip()
            if cmd in ("exit", "quit", "q"):
                print("Пока.")
                return
            if cmd == "help":
                print(HELP)
            elif cmd == "reset":
                history.clear()
                print("История очищена.")
            elif cmd == "history":
                print(f"{len(history)} сообщений в контексте.")
            elif cmd == "stats":
                stats()
            elif cmd == "model":
                if arg == "auto":
                    pinned = None
                    print("Автовыбор ветки.")
                elif arg in ALIASES:
                    pinned = ALIASES[arg]
                    print(f"Закреплено: {pinned} ({BRANCHES[pinned]['model']})")
                else:
                    print(f"Варианты: {', '.join(sorted(ALIASES))}, auto")
            else:
                print(f"Неизвестная команда. {HELP}")
            continue

        branch, why = (pinned, "закреплено") if pinned else classify(text)
        history.append({"role": "user", "content": text})

        print(f"\033[2m[{branch} · {BRANCHES[branch]['model']} · {why}]\033[0m")
        try:
            out, meta = call(branch, history, allow_escalate=(branch == "light"), stream=True)
        except Exception as e:
            history.pop()
            print(f"\033[31mОшибка: {e}\033[0m")
            continue

        escalated = False
        if out.strip().upper().startswith("ESCALATE"):
            escalated = True
            branch, why = "mid", "эскалация из light"
            print(f"\033[2m[light не потянул → {BRANCHES['mid']['model']}]\033[0m")
            out, meta = call(branch, history, stream=True)

        history.append({"role": "assistant", "content": out})
        # в контексте только последние 12 сообщений
        if len(history) > 12:
            del history[:-12]

        print(f"\033[2m[{meta['sec']}с · {meta['tps']} tok/s]\033[0m")
        log({"branch": branch, "reason": why, "escalated": escalated,
             "chars": len(text), "prompt": text[:200], "mode": "chat", **meta})


def main() -> None:
    p = argparse.ArgumentParser(description="Локальный роутер по моделям Ollama")
    p.add_argument("prompt", nargs="*", help="текст запроса (или stdin)")
    p.add_argument("--model", "-m", choices=sorted(ALIASES), help="форсировать ветку")
    p.add_argument("--stats", action="store_true", help="статистика по логу")
    p.add_argument("--quiet", "-q", action="store_true", help="только ответ")
    p.add_argument("--chat", "-c", action="store_true", help="интерактивный чат")
    a = p.parse_args()

    if a.stats:
        stats()
        return

    # Без аргументов и с терминала на входе — сразу чат.
    if a.chat or (not a.prompt and sys.stdin.isatty()):
        chat(a.model)
        return

    text = " ".join(a.prompt).strip() or sys.stdin.read().strip()
    if not text:
        p.error("пустой запрос")

    if a.model:
        branch, why = ALIASES[a.model], "форсировано вручную"
    else:
        branch, why = classify(text)

    msgs = [{"role": "user", "content": text}]
    escalated = False
    out, meta = call(branch, msgs, allow_escalate=(branch == "light"))

    if out.strip().upper().startswith("ESCALATE"):
        escalated = True
        branch, why = "mid", "эскалация из light"
        if not a.quiet:
            print(f"[light не потянул → {BRANCHES['mid']['model']}]", file=sys.stderr)
        out, meta = call(branch, msgs)

    if not a.quiet:
        print(f"[{branch} · {meta['model']} · {why} · "
              f"{meta['sec']}с · {meta['tps']} tok/s]\n", file=sys.stderr)
    print(out)

    log({"branch": branch, "reason": why, "escalated": escalated,
         "chars": len(text), "prompt": text[:200], **meta})


if __name__ == "__main__":
    main()
