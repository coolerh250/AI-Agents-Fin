#!/bin/bash
# backup_db.sh — TiDB agent_memory daily backup via Python/SQLAlchemy
# Crontab: 0 22 * * * /home/itadmin/ai_agent_studio/backup_db.sh >> /home/itadmin/logs/backup.log 2>&1

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKUP_DIR="/home/itadmin/backups/tidb"
DATE=$(date +%Y%m%d)
OUTFILE="$BACKUP_DIR/agent_memory_$DATE.sql"

mkdir -p "$BACKUP_DIR"

cd "$SCRIPT_DIR" || exit 1

~/.local/bin/uv run python - "$OUTFILE" "$SCRIPT_DIR" <<'PYEOF'
import sys, os
from datetime import datetime, timezone
from sqlalchemy import create_engine, inspect, text
from dotenv import load_dotenv

load_dotenv(os.path.join(sys.argv[2], ".env"))

host     = os.getenv("TIDB_HOST", "127.0.0.1")
port     = os.getenv("TIDB_PORT", "4000")
user     = os.getenv("TIDB_USER", "root")
password = os.getenv("TIDB_PASSWORD", "")
db       = os.getenv("TIDB_DB", "agent_memory")
url      = f"mysql+pymysql://{user}:{password}@{host}:{port}/{db}?charset=utf8mb4"

outfile = sys.argv[1]
engine  = create_engine(url, pool_pre_ping=True)
insp    = inspect(engine)
tables  = insp.get_table_names()

with open(outfile, "w", encoding="utf-8") as f:
    f.write(f"-- agent_memory backup {datetime.now(timezone.utc).isoformat()} UTC\n")
    f.write("SET NAMES utf8mb4;\n\n")
    with engine.connect() as conn:
        for table in tables:
            f.write(f"-- Table: {table}\n")
            ddl = conn.execute(text(f"SHOW CREATE TABLE `{table}`")).fetchone()
            f.write(ddl[1] + ";\n\n")
            rows = conn.execute(text(f"SELECT * FROM `{table}`")).fetchall()
            if rows:
                cols = ", ".join(f"`{c}`" for c in rows[0]._fields)
                for row in rows:
                    vals = ", ".join(
                        "NULL" if v is None
                        else f"'{str(v).replace(chr(39), chr(39)+chr(39))}'"
                        for v in row
                    )
                    f.write(f"INSERT INTO `{table}` ({cols}) VALUES ({vals});\n")
            f.write("\n")

print(f"Exported {len(tables)} tables → {outfile}")
PYEOF

if [ $? -ne 0 ]; then
    echo "[$(date)] ERROR: backup failed"
    exit 1
fi

SIZE=$(du -h "$OUTFILE" | cut -f1)
echo "[$(date)] backup done: agent_memory_$DATE.sql ($SIZE)"

# Keep last 30 days
find "$BACKUP_DIR" -name "*.sql" -mtime +30 -delete
