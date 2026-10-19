"""SQLite-хранилище записей. Время хранится как локальное (TZ из .env) в формате ISO."""
import sqlite3
from datetime import date, datetime, timedelta

SCHEMA = """
CREATE TABLE IF NOT EXISTS bookings (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL,
    user_name   TEXT,
    phone       TEXT,
    service     TEXT NOT NULL,
    master      TEXT NOT NULL,
    start       TEXT NOT NULL,
    duration    INTEGER NOT NULL,
    status      TEXT NOT NULL DEFAULT 'active',
    reminded    INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS ix_bookings_master_start ON bookings(master, start);
CREATE INDEX IF NOT EXISTS ix_bookings_user ON bookings(user_id, status);
"""

FMT = "%Y-%m-%dT%H:%M"


class DB:
    def __init__(self, path: str):
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.close()

    def busy_intervals(self, master: str, day: date) -> list[tuple[datetime, datetime]]:
        rows = self.conn.execute(
            "SELECT start, duration FROM bookings WHERE master=? AND status='active' AND start LIKE ?",
            (master, day.isoformat() + "%"),
        ).fetchall()
        result = []
        for r in rows:
            s = datetime.strptime(r["start"], FMT)
            result.append((s, s + timedelta(minutes=r["duration"])))
        return result

    def add(self, *, user_id: int, user_name: str, phone: str, service: str, master: str,
            start: datetime, duration: int) -> int:
        cur = self.conn.execute(
            "INSERT INTO bookings(user_id, user_name, phone, service, master, start, duration) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (user_id, user_name, phone, service, master, start.strftime(FMT), duration),
        )
        self.conn.commit()
        return cur.lastrowid

    def get(self, booking_id: int):
        return self.conn.execute("SELECT * FROM bookings WHERE id=?", (booking_id,)).fetchone()

    def user_upcoming(self, user_id: int, now: datetime):
        return self.conn.execute(
            "SELECT * FROM bookings WHERE user_id=? AND status='active' AND start>=? ORDER BY start",
            (user_id, now.strftime(FMT)),
        ).fetchall()

    def cancel(self, booking_id: int, user_id: int) -> bool:
        cur = self.conn.execute(
            "UPDATE bookings SET status='cancelled' WHERE id=? AND user_id=? AND status='active'",
            (booking_id, user_id),
        )
        self.conn.commit()
        return cur.rowcount == 1

    def due_reminders(self, now: datetime, ahead: timedelta):
        return self.conn.execute(
            "SELECT * FROM bookings WHERE status='active' AND reminded=0 AND start>? AND start<=?",
            (now.strftime(FMT), (now + ahead).strftime(FMT)),
        ).fetchall()

    def mark_reminded(self, booking_id: int) -> None:
        self.conn.execute("UPDATE bookings SET reminded=1 WHERE id=?", (booking_id,))
        self.conn.commit()

    def for_day(self, day: date):
        return self.conn.execute(
            "SELECT * FROM bookings WHERE status='active' AND start LIKE ? ORDER BY master, start",
            (day.isoformat() + "%",),
        ).fetchall()


def parse_start(value: str) -> datetime:
    return datetime.strptime(value, FMT)
