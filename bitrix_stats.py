"""
Агрегаты из Битрикса для советника (этап B2).

Метод выручки СОГЛАСОВАН с владельцем — по ДАТЕ ДОГОВОРА:
в воронке САЛОН (CATEGORY_ID=0) сделка заводится сразу как договор, поэтому
дата договора = DATE_CREATE. Выручка месяца = сумма OPPORTUNITY сделок САЛОН,
созданных в месяце, кроме стадий NEW (до договора) и LOSE (отмена), сумма > 0.

Деньги живут только в САЛОН; ЗАМЕР/МОНТАЖ/АКТИВНЫЕ ПРОДАЖИ — операционные (суммы 0).

Только сводные цифры (выручка, число договоров, средний чек, лиды) — не операционка
по отдельным сделкам и без персональных данных клиентов.
"""

from __future__ import annotations

import datetime

from common import bx_call, bx_list

SALON_CATEGORY = 0
EXCLUDE_STAGES = {"NEW", "LOSE"}  # NEW — договор ещё не подписан, LOSE — отмена


def _month_bounds(year: int, month: int) -> tuple[str, str]:
    """Начало месяца и начало следующего (ISO-даты) для фильтра Битрикса."""
    d1 = datetime.date(year, month, 1)
    ny, nm = (year + 1, 1) if month == 12 else (year, month + 1)
    return d1.isoformat(), datetime.date(ny, nm, 1).isoformat()


def salon_revenue(d1: str, d2: str) -> dict:
    """Выручка воронки САЛОН по дате договора за период [d1, d2)."""
    deals = bx_list("crm.deal.list", {
        "filter": {"CATEGORY_ID": SALON_CATEGORY, ">=DATE_CREATE": d1, "<DATE_CREATE": d2},
        "select": ["ID", "OPPORTUNITY", "STAGE_ID"],
    })
    live = [x for x in deals
            if x.get("STAGE_ID") not in EXCLUDE_STAGES and float(x.get("OPPORTUNITY") or 0) > 0]
    revenue = sum(float(x.get("OPPORTUNITY") or 0) for x in live)
    return {
        "created": len(deals),
        "contracts": len(live),
        "revenue": revenue,
        "avg_check": revenue / len(live) if live else 0.0,
    }


def leads_count(d1: str, d2: str) -> int:
    """Сколько лидов создано за период (для оценки входящего потока)."""
    return bx_call("crm.lead.list", {
        "filter": {">=DATE_CREATE": d1, "<DATE_CREATE": d2}, "select": ["ID"],
    }).get("total", 0) or 0


def monthly_rows(today: datetime.date | None = None, months_back: int = 3) -> list[dict]:
    """Сводка по текущему и предыдущим месяцам (по убыванию свежести)."""
    today = today or datetime.date.today()
    rows = []
    y, m = today.year, today.month
    for back in range(months_back + 1):
        yy, mm = y, m - back
        while mm < 1:
            mm += 12
            yy -= 1
        d1, d2 = _month_bounds(yy, mm)
        r = salon_revenue(d1, d2)
        r["leads"] = leads_count(d1, d2)
        r["month"] = f"{yy}-{mm:02d}"
        r["is_current"] = (back == 0)
        rows.append(r)
    return rows


def _rub(n: float) -> str:
    """Число с пробелом-разделителем тысяч: 1 000 000 вместо 1,000,000."""
    return f"{n:,.0f}".replace(",", " ")


def render_summary(today: datetime.date | None = None) -> str:
    """Компактная сводка для контекста модели (плоский текст, без markdown)."""
    rows = monthly_rows(today)
    out = [
        "Метод выручки — по дате договора, воронка САЛОН (согласовано с владельцем).",
        "Помесячно (свежее сверху):",
    ]
    for r in rows:
        tail = "  <- текущий месяц, ещё идёт (неполный)" if r["is_current"] else ""
        out.append(
            f"  {r['month']}: выручка {_rub(r['revenue'])} руб, договоров {r['contracts']}, "
            f"средний чек {_rub(r['avg_check'])} руб, лидов {r['leads']}{tail}"
        )
    return "\n".join(out)
