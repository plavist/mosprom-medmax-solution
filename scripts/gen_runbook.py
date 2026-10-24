#!/usr/bin/env python3
"""Собирает CLAUDE_TEST_RUNBOOK.md: шаблон docs/runbook.template.md + эталоны, сгенерированные из сценария (scenario.py).
Запуск: .venv/bin/python scripts/gen_runbook.py"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "services" / "pipeline"))
from app import commands, scenario as sc  # noqa: E402

SITE = {"GrPP": "ГрПП", "GTP": "ГТП", "Luzhniki": "Лужники"}


def ex(text: str, n: int = 230) -> str:
    t = " ⏎ ".join(x.strip() for x in text.strip().splitlines() if x.strip())
    return (t[:n].rstrip() + "…") if len(t) > n else t


def tags(it: dict) -> str:
    out = []
    if it.get("sites"):
        out.append("только " + "/".join(SITE[s] for s in it["sites"]))
    if it.get("office") is True:
        out.append("только офис")
    if it.get("cond"):
        out.append(f"если ответ «{it['cond'][0]}» ∈ {it['cond'][1]}")
    return f" _({'; '.join(out)})_" if out else ""


def opt_line(o: dict) -> str:
    bits = []
    if o["then"]:
        bits.append("ответ: «" + ex(" ".join(t.get("text") or t.get("fallback") or "(из базы знаний)" for t in o["then"]), 160) + "»")
    if o["hr"] == "contact":
        bits.append("только кнопка «Связаться с рекрутером»")
    elif o["hr"]:
        bits.append("кнопки «Передать HR через Корсика» / «Связаться с рекрутером»")
    if o["contact"]:
        bits.append("кнопка «Готов озвучить причину в личной коммуникации с рекрутером»")
    if o["follow"] is not None:
        bits.append("ждёт свободный текст-пояснение")
    if o["escalate"]:
        bits.append(f"**эскалация в админке**: «{o['escalate']}»")
    return f"  - «{o['label']}» → " + ("; ".join(bits) if bits else "следующий вопрос")


def appendix_a() -> str:
    out = []
    for s in sc.STEPS:
        head = f"#### `{s['id']}` — {s['title']}"
        meta = [f"когда: {s['when']}"]
        if s.get("sites"):
            meta.append("площадки: " + "/".join(SITE[x] for x in s["sites"]))
        if s.get("office"):
            meta.append("только офисные")
        if s.get("gate"):
            meta.append("**только после «отмашки рекрутера»**")
        if s["survey"]:
            meta.append(f"опрос `{s['survey']}` (напоминания, аналитика)")
        out += [head, "_" + "; ".join(meta) + "_", ""]
        for i, it in enumerate(s["items"], 1):
            t = it["t"]
            if t == "msg":
                note = f" ⚠️ без вложения «{it['asset']}» НЕ отправляется" + (f", вместо него: «{ex(it['fallback'], 90)}»" if it.get("fallback") else "") \
                    if it.get("req") and it.get("asset") else ""
                hr = " + кнопки «Передать HR через Корсика» / «Связаться с рекрутером»" if it.get("hr") else ""
                out.append(f"{i}. **Сообщение**{tags(it)}: «{ex(it['text'])}»{note}{hr}")
            elif t == "ask":
                kind = "мульти-выбор (несколько вариантов + «Готово ✔»)" if it["multi"] else "кнопки"
                out.append(f"{i}. **Вопрос** [{kind}]{tags(it)}: «{ex(it['text'], 160)}»" + (f" + кнопка «{it['other']}» (свободный текст)" if it["other"] else ""))
                out += [opt_line(o) for o in it["options"]]
            elif t == "scale":
                out.append(f"{i}. **Шкала 1–10** (10 кнопок в 2 ряда){tags(it)}: «{ex(it['text'], 160)}» — оценка < {it['thr']} → бот просит указать причину")
            elif t == "yesno":
                out.append(f"{i}. **Да/Нет**{tags(it)}: «{ex(it['text'], 160)}» — «Нет» → бот просит указать причину")
            else:
                out.append(f"{i}. **Открытый вопрос**{tags(it)}: «{ex(it['text'], 160)}»")
        out.append("")
    return "\n".join(out)


def appendix_b() -> str:
    rows = ["| Опрос | Вопрос (ключ) | Что ответить | Ожидаемая реакция | Эскалация |", "|---|---|---|---|---|"]
    for s in sc.STEPS:
        for it in s["items"]:
            if it["t"] == "ask":
                for o in it["options"]:
                    then = ex(" ".join(t.get("text") or t.get("fallback") or "(ответ из базы знаний)" for t in o["then"]), 110) if o["then"] else "—"
                    rows.append(f"| {s['survey'] or s['id']} | {it['key']}{tags(it).replace('_', '')} | {o['label']} | {then}"
                                f"{(' + кнопка «Связаться с рекрутером»' if o['hr'] == 'contact' else ' + кнопки HR') if o['hr'] else ''}{' + «готов лично»' if o['contact'] else ''} | {'да: ' + o['escalate'] if o['escalate'] else '—'} |")
            elif it["t"] in ("scale", "yesno"):
                what = f"оценка < {it.get('thr', 8)}" if it["t"] == "scale" else "«Нет»"
                rows.append(f"| {s['survey'] or s['id']} | {it['key']} | {what} | просит причину: «{ex(sc.REASON_PROMPT, 60)}» | нет (комментарий проходит анализ тональности) |")
    return "\n".join(rows)


def appendix_c() -> str:
    """Расписание для тестового сотрудника с датой выхода 2026-11-02 (понедельник): когда тикать планировщиком."""
    rows = ["| Этап | Время (МСК) | Тикнуть на (due + 1 мин) | ГрПП | ГТП | Лужники |", "|---|---|---|---|---|---|"]
    emps = {k: {"hire_date": "2026-11-02", "probation_end": None, "site": k, "is_office": k != "GTP"} for k in SITE}
    for st in sc.STEPS:
        if not st["due"]:
            rows.append(f"| `{st['id']}` | при привязке (`/start КОД`) | — | ✅ | ✅ | ✅ |")
            continue
        due = sc.due_at(st, emps["GrPP"])
        tick = (due + sc.timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:00")
        marks = ["✅" if sc.applies(st, e) else "—" for e in emps.values()]
        gate = " (+ отмашка рекрутера)" if st.get("gate") else ""
        rows.append(f"| `{st['id']}`{gate} | {due:%Y-%m-%d %H:%M} | `{tick}` | {' | '.join(marks)} |")
    return "\n".join(rows)


def main():
    tpl = (ROOT / "docs" / "runbook.template.md").read_text(encoding="utf-8")
    out = (tpl.replace("{{APPENDIX_A}}", appendix_a()).replace("{{APPENDIX_B}}", appendix_b()).replace("{{APPENDIX_C}}", appendix_c())
           .replace("{{COMMANDS}}", ", ".join(f"/{d['name']}" for d in commands.DEFAULTS))
           .replace("{{N_STEPS}}", str(len(sc.STEPS))).replace("{{MISSING_ASSETS}}", ", ".join(sc.missing_assets())))
    (ROOT / "CLAUDE_TEST_RUNBOOK.md").write_text(out, encoding="utf-8")
    print(f"CLAUDE_TEST_RUNBOOK.md: {len(out.splitlines())} строк, {len(out)} символов")


if __name__ == "__main__":
    main()
