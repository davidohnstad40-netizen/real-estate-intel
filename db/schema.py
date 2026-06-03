"""
DuckDB schema â€” creates all tables on first run.
"""
import duckdb, os

def get_db(path: str = None) -> duckdb.DuckDBPyConnection:
    path = path or os.getenv("DB_PATH", "./data/rei.duckdb")
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    con = duckdb.connect(path)
    _init(con)
    return con

def _init(con: duckdb.DuckDBPyConnection):
    con.executemany("", [])   # no-op to warm connection
    con.execute("""
    CREATE TABLE IF NOT EXISTS properties (
        id             VARCHAR PRIMARY KEY,
        address        VARCHAR NOT NULL,
        city           VARCHAR DEFAULT 'Blaine',
        state          VARCHAR DEFAULT 'MN',
        zip            VARCHAR DEFAULT '55449',
        lat            DOUBLE,
        lng            DOUBLE,
        owner_name     VARCHAR,
        beds           INTEGER,
        baths          DOUBLE,
        sqft           INTEGER,
        year_built     INTEGER,
        lot_size       VARCHAR,
        emv            DOUBLE,
        est_value      DOUBLE,
        prior_sale_price DOUBLE,
        prior_sale_year  INTEGER,
        years_owned    DOUBLE,
        homestead      VARCHAR,
        owner_type     VARCHAR,
        anoka_pin      VARCHAR,
        school_district VARCHAR,
        created_at     TIMESTAMP DEFAULT current_timestamp,
        updated_at     TIMESTAMP DEFAULT current_timestamp
    )""")

    con.execute("""
    CREATE TABLE IF NOT EXISTS property_scores (
        id                  VARCHAR PRIMARY KEY,
        motivation_score    INTEGER DEFAULT 0,
        knock_tier          VARCHAR DEFAULT 'TBD',
        primary_signal      TEXT,
        score_factors       JSON,
        est_equity_usd      DOUBLE,
        equity_pct          DOUBLE,
        monthly_piti        DOUBLE,
        updated_at          TIMESTAMP DEFAULT current_timestamp
    )""")

    con.execute("""
    CREATE TABLE IF NOT EXISTS property_signals (
        id           VARCHAR,
        signal_type  VARCHAR,
        signal_value TEXT,
        source       VARCHAR,
        confidence   DOUBLE DEFAULT 1.0,
        date_found   TIMESTAMP DEFAULT current_timestamp,
        PRIMARY KEY (id, signal_type)
    )""")

    con.execute("""
    CREATE TABLE IF NOT EXISTS mcro_records (
        id           VARCHAR,
        case_number  VARCHAR,
        case_type    VARCHAR,
        date_filed   DATE,
        parties      TEXT,
        county       VARCHAR,
        status       VARCHAR,
        notes        TEXT,
        PRIMARY KEY (id, case_number)
    )""")

    con.execute("""
    CREATE TABLE IF NOT EXISTS score_history (
        id               VARCHAR,
        snapshot_date    DATE DEFAULT current_date,
        motivation_score INTEGER,
        knock_tier       VARCHAR,
        primary_signal   TEXT,
        score_factors    JSON,
        PRIMARY KEY (id, snapshot_date)
    )""")

    con.execute("""
    CREATE TABLE IF NOT EXISTS regions (
        region_id   VARCHAR PRIMARY KEY,
        name        VARCHAR,
        geojson     JSON,
        created_at  TIMESTAMP DEFAULT current_timestamp
    )""")

    con.execute("""
    CREATE TABLE IF NOT EXISTS human_feedback (
        id           VARCHAR,
        outcome      VARCHAR,  -- good_lead, bad_lead, sold, interested, not_interested, follow_up
        notes        TEXT,
        recorded_at  TIMESTAMP DEFAULT current_timestamp,
        PRIMARY KEY (id, recorded_at)
    )""")
    con.execute("""
    CREATE TABLE IF NOT EXISTS contact_log (
        log_id         VARCHAR PRIMARY KEY,
        property_id    VARCHAR,
        contact_date   DATE,
        method         VARCHAR,
        outcome        VARCHAR,
        notes          TEXT,
        follow_up_date DATE,
        created_at     TIMESTAMP DEFAULT current_timestamp
    )""")

