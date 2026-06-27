import sqlite3
import json
import os

DB_PATH = os.environ.get("DB_PATH", "audit.db")


def _connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with _connect() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS submissions (
                content_id TEXT PRIMARY KEY,
                creator_id TEXT,
                content_snippet TEXT,
                attribution TEXT,
                confidence REAL,
                llm_score REAL,
                stylometric_score REAL,
                burstiness_score REAL,
                label_verdict TEXT,
                status TEXT DEFAULT 'classified',
                timestamp TEXT,
                signals_json TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS appeals (
                appeal_id TEXT PRIMARY KEY,
                content_id TEXT,
                creator_id TEXT,
                reasoning TEXT,
                timestamp TEXT,
                FOREIGN KEY (content_id) REFERENCES submissions(content_id)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS certificates (
                certificate_id TEXT PRIMARY KEY,
                content_id TEXT,
                creator_id TEXT,
                statement TEXT,
                draft_evidence TEXT,
                issued_at TEXT,
                FOREIGN KEY (content_id) REFERENCES submissions(content_id)
            )
        """)
        conn.execute("""
            ALTER TABLE submissions ADD COLUMN certificate_id TEXT
        """) if not _column_exists(conn, "submissions", "certificate_id") else None
        conn.commit()


def _column_exists(conn, table, column):
    cols = [row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()]
    return column in cols


def log_submission(content_id, creator_id, content, result, timestamp):
    snippet = content[:200] + "..." if len(content) > 200 else content
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO submissions
                (content_id, creator_id, content_snippet, attribution, confidence,
                 llm_score, stylometric_score, burstiness_score, label_verdict,
                 status, timestamp, signals_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'classified', ?, ?)
            """,
            (
                content_id,
                creator_id,
                snippet,
                result["attribution"],
                result["confidence"],
                result["signals"]["llm_score"],
                result["signals"]["stylometric_score"],
                result["signals"]["burstiness_score"],
                result["label"]["verdict"],
                timestamp,
                json.dumps(result["signals"]),
            ),
        )
        conn.commit()


def log_appeal(appeal_id, content_id, creator_id, reasoning, timestamp):
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO appeals (appeal_id, content_id, creator_id, reasoning, timestamp)
            VALUES (?, ?, ?, ?, ?)
            """,
            (appeal_id, content_id, creator_id, reasoning, timestamp),
        )
        conn.execute(
            "UPDATE submissions SET status = 'under_review' WHERE content_id = ?",
            (content_id,),
        )
        conn.commit()


def get_submission(content_id):
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM submissions WHERE content_id = ?", (content_id,)
        ).fetchone()
        return dict(row) if row else None


def get_log(limit=20, offset=0):
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT s.*, a.appeal_id, a.reasoning as appeal_reasoning, a.timestamp as appeal_at
            FROM submissions s
            LEFT JOIN appeals a ON s.content_id = a.content_id
            ORDER BY s.timestamp DESC
            LIMIT ? OFFSET ?
            """,
            (limit, offset),
        ).fetchall()
        return [dict(r) for r in rows]


def get_analytics():
    with _connect() as conn:
        total = conn.execute("SELECT COUNT(*) FROM submissions").fetchone()[0]

        counts = conn.execute(
            "SELECT attribution, COUNT(*) as cnt FROM submissions GROUP BY attribution"
        ).fetchall()
        attribution_counts = {"human": 0, "ai": 0, "uncertain": 0}
        for row in counts:
            attribution_counts[row["attribution"]] = row["cnt"]

        appealed = conn.execute(
            "SELECT COUNT(DISTINCT content_id) FROM appeals"
        ).fetchone()[0]
        appeal_rate = round((appealed / total * 100), 2) if total else 0.0

        # Signal disagreement: |llm_score - stylometric_score| > 0.3
        disagreements = conn.execute(
            "SELECT COUNT(*) FROM submissions WHERE ABS(llm_score - stylometric_score) > 0.3"
        ).fetchone()[0]
        disagreement_rate = round((disagreements / total * 100), 2) if total else 0.0

        avg_conf = conn.execute(
            "SELECT AVG(confidence) FROM submissions"
        ).fetchone()[0]

        return {
            "total_submissions": total,
            "attribution_counts": attribution_counts,
            "appeal_rate_pct": appeal_rate,
            "signal_disagreement_rate_pct": disagreement_rate,
            "avg_confidence": round(avg_conf, 4) if avg_conf else 0.0,
        }


def issue_certificate(certificate_id, content_id, creator_id, statement, draft_evidence, issued_at):
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO certificates
                (certificate_id, content_id, creator_id, statement, draft_evidence, issued_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (certificate_id, content_id, creator_id, statement, draft_evidence or "", issued_at),
        )
        conn.execute(
            "UPDATE submissions SET certificate_id = ? WHERE content_id = ?",
            (certificate_id, content_id),
        )
        conn.commit()


def get_certificate(certificate_id):
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM certificates WHERE certificate_id = ?", (certificate_id,)
        ).fetchone()
        return dict(row) if row else None
