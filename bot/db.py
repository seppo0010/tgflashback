import sqlite3
from collections import defaultdict
from datetime import date, datetime, timedelta

from bot.config import DB_PATH, PLAYER_CODES, TIMEZONE

SCHEMA = """
CREATE TABLE IF NOT EXISTS players (
    code TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    telegram_user_id INTEGER UNIQUE,
    telegram_username TEXT
);

CREATE TABLE IF NOT EXISTS scores (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    player_code TEXT NOT NULL REFERENCES players(code),
    puzzle_date TEXT NOT NULL,
    score INTEGER NOT NULL,
    chat_id INTEGER,
    message_id INTEGER,
    raw_text TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(player_code, puzzle_date)
);
"""


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    conn = get_connection()
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()


def seed_players() -> None:
    conn = get_connection()
    try:
        for code in PLAYER_CODES:
            conn.execute(
                "INSERT OR IGNORE INTO players (code, display_name) VALUES (?, ?)",
                (code, code),
            )
        conn.commit()
    finally:
        conn.close()


def get_player_by_telegram_id(telegram_user_id: int) -> sqlite3.Row | None:
    conn = get_connection()
    try:
        return conn.execute(
            "SELECT * FROM players WHERE telegram_user_id = ?", (telegram_user_id,)
        ).fetchone()
    finally:
        conn.close()


def get_player_by_code(code: str) -> sqlite3.Row | None:
    conn = get_connection()
    try:
        return conn.execute(
            "SELECT * FROM players WHERE code = ?", (code,)
        ).fetchone()
    finally:
        conn.close()


def link_player(code: str, telegram_user_id: int, telegram_username: str | None) -> None:
    conn = get_connection()
    try:
        # A telegram user can only be linked to one code at a time.
        conn.execute(
            "UPDATE players SET telegram_user_id = NULL, telegram_username = NULL "
            "WHERE telegram_user_id = ? AND code != ?",
            (telegram_user_id, code),
        )
        conn.execute(
            "UPDATE players SET telegram_user_id = ?, telegram_username = ? WHERE code = ?",
            (telegram_user_id, telegram_username, code),
        )
        conn.commit()
    finally:
        conn.close()


def upsert_score(
    player_code: str,
    puzzle_date: str,
    score: int,
    chat_id: int | None = None,
    message_id: int | None = None,
    raw_text: str | None = None,
) -> None:
    conn = get_connection()
    try:
        conn.execute(
            """
            INSERT INTO scores (player_code, puzzle_date, score, chat_id, message_id, raw_text)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(player_code, puzzle_date) DO UPDATE SET
                score = excluded.score,
                chat_id = excluded.chat_id,
                message_id = excluded.message_id,
                raw_text = excluded.raw_text,
                created_at = datetime('now')
            """,
            (player_code, puzzle_date, score, chat_id, message_id, raw_text),
        )
        conn.commit()
    finally:
        conn.close()


def get_standings(year: int) -> list[dict]:
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT player_code, puzzle_date, score FROM scores "
            "WHERE strftime('%Y', puzzle_date) = ?",
            (str(year),),
        ).fetchall()
    finally:
        conn.close()

    totals: dict[str, int] = defaultdict(int)
    counts: dict[str, int] = defaultdict(int)
    by_date: dict[str, dict[str, int]] = defaultdict(dict)

    for row in rows:
        code, puzzle_date, score = row["player_code"], row["puzzle_date"], row["score"]
        totals[code] += score
        counts[code] += 1
        by_date[puzzle_date][code] = score

    wins: dict[str, int] = defaultdict(int)
    for scores_that_week in by_date.values():
        if not scores_that_week:
            continue
        best = max(scores_that_week.values())
        for code, score in scores_that_week.items():
            if score == best:
                wins[code] += 1

    standings = []
    for code in PLAYER_CODES:
        played = counts.get(code, 0)
        total = totals.get(code, 0)
        standings.append(
            {
                "code": code,
                "total": total,
                "played": played,
                "avg": (total / played) if played else 0.0,
                "wins": wins.get(code, 0),
            }
        )
    standings.sort(key=lambda p: p["total"], reverse=True)
    return standings


def get_weekly_scores(year: int) -> dict[str, list[tuple[str, int]]]:
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT player_code, puzzle_date, score FROM scores "
            "WHERE strftime('%Y', puzzle_date) = ? ORDER BY puzzle_date",
            (str(year),),
        ).fetchall()
    finally:
        conn.close()

    weekly: dict[str, list[tuple[str, int]]] = {code: [] for code in PLAYER_CODES}
    for row in rows:
        weekly[row["player_code"]].append((row["puzzle_date"], row["score"]))
    return weekly


def get_played_dates() -> list[str]:
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT DISTINCT puzzle_date FROM scores ORDER BY puzzle_date"
        ).fetchall()
    finally:
        conn.close()
    return [row["puzzle_date"] for row in rows]


def has_score(code: str, puzzle_date: str) -> bool:
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT 1 FROM scores WHERE player_code = ? AND puzzle_date = ?",
            (code, puzzle_date),
        ).fetchone()
    finally:
        conn.close()
    return row is not None


def get_pending(code: str) -> list[str]:
    # Every Saturday since the first recorded edition counts, not just the
    # ones somebody already scored: otherwise a week nobody has played yet
    # (e.g. because the announcement failed to go out) never shows up.
    conn = get_connection()
    try:
        first = conn.execute("SELECT MIN(puzzle_date) AS d FROM scores").fetchone()["d"]
        own_dates = {
            row["puzzle_date"]
            for row in conn.execute(
                "SELECT puzzle_date FROM scores WHERE player_code = ?", (code,)
            )
        }
    finally:
        conn.close()

    if first is None:
        return []

    today = datetime.now(TIMEZONE).date()
    pending = []
    d = date.fromisoformat(first)
    while d <= today:
        if d.isoformat() not in own_dates:
            pending.append(d.isoformat())
        d += timedelta(days=7)
    return pending


def interactive_url(puzzle_date: str) -> str:
    d = date.fromisoformat(puzzle_date) - timedelta(days=1)
    return f"https://www.nytimes.com/interactive/{d.year}/{d.month:02d}/{d.day:02d}/upshot/flashback.html"
