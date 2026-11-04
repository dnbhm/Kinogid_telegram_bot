import csv
import io
from datetime import datetime
from zoneinfo import ZoneInfo


def parse_send_at(args: list[str], timezone: ZoneInfo) -> datetime:
    if len(args) != 2:
        raise ValueError("Формат: /post club ГГГГ-ММ-ДД ЧЧ:ММ")
    try:
        naive = datetime.strptime(" ".join(args), "%Y-%m-%d %H:%M")
    except ValueError as exc:
        raise ValueError("Пример даты: /post club 2026-11-09 12:00") from exc
    value = naive.replace(tzinfo=timezone)
    # Для других часовых поясов не принимаем отсутствующее/двусмысленное время при переводе часов.
    roundtrip = value.astimezone(ZoneInfo("UTC")).astimezone(timezone)
    if roundtrip.replace(tzinfo=None) != naive or value.utcoffset() != value.replace(fold=1).utcoffset():
        raise ValueError("Это время попадает на перевод часов. Выберите другое время.")
    if value <= datetime.now(timezone):
        raise ValueError("Для отложенного поста укажите время в будущем")
    return value


def local_time(value: str | None, timezone: ZoneInfo) -> str:
    return datetime.fromisoformat(value).astimezone(timezone).strftime("%d.%m.%Y %H:%M") if value else "—"


def csv_bytes(rows: list[dict]) -> bytes:
    stream = io.StringIO(newline="")
    if not rows:
        return b""
    writer = csv.DictWriter(stream, fieldnames=list(rows[0]), delimiter=";")
    writer.writeheader()
    for row in rows:
        # Имена и username задаёт пользователь; защищаем открытие CSV в Excel от формул.
        safe = {}
        for key, value in row.items():
            if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")):
                value = "'" + value
            if isinstance(value, str) and value.startswith(("\t", "\r", "\n")):
                value = "'" + value
            safe[key] = value
        writer.writerow(safe)
    return stream.getvalue().encode("utf-8-sig")
