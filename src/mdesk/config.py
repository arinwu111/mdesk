"""路径与常量。所有模块从这里取配置，不要在别处硬编码路径。"""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

DATA_DIR = ROOT / "data"
SQL_DIR = ROOT / "sql"
TEMPLATE_DIR = ROOT / "templates"
STATIC_DIR = ROOT / "static"
DOCS_DIR = ROOT / "docs"
SITE_DIR = ROOT / "site"          # 渲染出的静态站，GitHub Pages 指向这里

DB_PATH = DATA_DIR / "mdesk.duckdb"
WATCHLIST_CSV = DATA_DIR / "watchlist.csv"
CROSS_LISTING_CSV = DATA_DIR / "cross_listing.csv"
SCHEMA_SQL = SQL_DIR / "schema.sql"
RULES_SQL = SQL_DIR / "rules_seed.sql"

# 采集范围
HISTORY_YEARS = 2

# 异常严重度。P0 当场处理，P1 记台账排期，P2 归档观察
SEVERITY_ORDER = {"P0": 0, "P1": 1, "P2": 2}

# 角标状态到展示的映射
BADGE_LABEL = {
    "ok": ("灰", "今日无异常"),
    "explained": ("黄", "有异常但原因已查明，数字可用"),
    "open": ("红", "有异常且原因未明，该行数字暂不可信"),
}
