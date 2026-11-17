"""SQLite catalog: what was found, where it came from, and how it changed.

`assets` holds the current state of everything harvested; `asset_versions` gets a row
only when an asset appears, changes, disappears or comes back (compared by content
hash), so history stays small even with frequent harvests.

SQLite because the catalog is small (hundreds of thousands of rows), mostly upserts,
and needs no server. All SQL lives here, so moving to Postgres/PostGIS (where the
2024 Munger tables lived) only means rewriting this module.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from pathlib import Path
from urllib.parse import urlsplit

from .models import Asset, Candidate, Endpoint, Jurisdiction, Platform
from .util import utcnow

SCHEMA = """
CREATE TABLE IF NOT EXISTS jurisdictions (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    website TEXT,
    kind TEXT,
    state TEXT,
    source TEXT,
    last_crawled_at TEXT,
    crawl_pages INTEGER,                  -- pages fetched by the last crawl
    crawl_failure TEXT                    -- why it fetched none: blocked | dead | robots | ...
);
CREATE TABLE IF NOT EXISTS candidates (
    url TEXT PRIMARY KEY,
    via TEXT NOT NULL,
    found_on TEXT,
    link_text TEXT,
    jurisdiction_id TEXT,
    first_seen TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'new',   -- new | classified | found | none | dead
    endpoint_id TEXT,
    probed_at TEXT
);
CREATE INDEX IF NOT EXISTS candidates_status ON candidates(status);
CREATE TABLE IF NOT EXISTS endpoints (
    id TEXT PRIMARY KEY,
    platform TEXT NOT NULL,
    url TEXT NOT NULL,
    vendor TEXT,
    label TEXT,
    discovered_via TEXT,
    discovered_from TEXT,
    first_seen TEXT NOT NULL,
    last_harvest_at TEXT,
    last_harvest_status TEXT
);
CREATE TABLE IF NOT EXISTS endpoint_jurisdictions (
    endpoint_id TEXT NOT NULL,
    jurisdiction_id TEXT NOT NULL,
    relation TEXT NOT NULL,               -- same_domain | linked
    evidence_url TEXT,
    PRIMARY KEY (endpoint_id, jurisdiction_id)
);
CREATE TABLE IF NOT EXISTS harvests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    endpoint_id TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT,                          -- ok | partial | failed
    n_assets INTEGER,
    n_new INTEGER,
    n_changed INTEGER,
    n_removed INTEGER,
    n_candidates INTEGER,
    errors TEXT,
    raw_path TEXT
);
CREATE TABLE IF NOT EXISTS assets (
    endpoint_id TEXT NOT NULL,
    key TEXT NOT NULL,
    kind TEXT NOT NULL,
    name TEXT,
    title TEXT,
    url TEXT,
    parent_key TEXT,
    service_type TEXT,
    geometry_type TEXT,
    format TEXT,
    srid INTEGER,
    xmin REAL, ymin REAL, xmax REAL, ymax REAL,
    bbox_srid INTEGER,
    description TEXT,
    source_updated_at TEXT,
    extra TEXT,
    content_hash TEXT NOT NULL,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    removed_at TEXT,
    missed_at TEXT,                       -- a complete harvest missed it; removed if the next does
    PRIMARY KEY (endpoint_id, key)
);
CREATE INDEX IF NOT EXISTS assets_kind ON assets(kind);
CREATE TABLE IF NOT EXISTS asset_versions (
    endpoint_id TEXT NOT NULL,
    key TEXT NOT NULL,
    harvest_id INTEGER NOT NULL,
    observed_at TEXT NOT NULL,
    change TEXT NOT NULL,                 -- new | changed | removed | returned
    content_hash TEXT,
    snapshot TEXT,
    PRIMARY KEY (endpoint_id, key, harvest_id)
);
CREATE TABLE IF NOT EXISTS checks (
    endpoint_id TEXT NOT NULL,
    checked_at TEXT NOT NULL,
    http_status INTEGER,
    ok INTEGER NOT NULL,
    elapsed_ms INTEGER,
    tls_insecure INTEGER,
    detail TEXT
);
CREATE VIEW IF NOT EXISTS current_assets AS
    SELECT e.platform, e.url AS endpoint_url, e.vendor, a.*
    FROM assets a JOIN endpoints e ON e.id = a.endpoint_id
    WHERE a.removed_at IS NULL;
-- Which jurisdictions publish what: "which MA towns publish parcels, where, and how?"
CREATE VIEW IF NOT EXISTS jurisdiction_assets AS
    SELECT j.id AS jurisdiction_id, j.name AS jurisdiction_name, j.state,
           j.kind AS jurisdiction_kind, ej.relation, ej.evidence_url, ca.*
    FROM jurisdictions j
    JOIN endpoint_jurisdictions ej ON ej.jurisdiction_id = j.id
    JOIN current_assets ca ON ca.endpoint_id = ej.endpoint_id;
"""

# Columns added after a table was first created: (table, column, type).
# CREATE TABLE IF NOT EXISTS leaves existing tables alone, so these are added by hand.
MIGRATIONS = (
    ("jurisdictions", "crawl_pages", "INTEGER"),
    ("jurisdictions", "crawl_failure", "TEXT"),
    ("assets", "missed_at", "TEXT"),
)

ASSET_COLUMNS = (
    "endpoint_id, key, kind, name, title, url, parent_key, service_type, geometry_type, format, "
    "srid, xmin, ymin, xmax, ymax, bbox_srid, description, source_updated_at, extra, "
    "content_hash, first_seen, last_seen, removed_at"
)


def _bare_host(url: str | None) -> str:
    host = (urlsplit(url or "").hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


class Store:
    def __init__(self, path: Path | str) -> None:
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path))
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        self._migrate()
        self.db.commit()

    def _migrate(self) -> None:
        for table, column, kind in MIGRATIONS:
            have = {r["name"] for r in self.db.execute(f"PRAGMA table_info({table})")}
            if column not in have:
                self.db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {kind}")
        self._rekey_viewers()

    def _rekey_viewers(self) -> None:
        """Merge viewer rows stored under ids from an older `Endpoint.identity`.

        Viewer ids were once the URL as first seen, so one town's viewer could be stored
        several times (http/https, www./next. host aliases, slug case). Rows are moved to
        the current id, oldest first, so the merged row keeps the first URL seen.
        """
        rows = self.db.execute(
            "SELECT * FROM endpoints WHERE platform = ? ORDER BY first_seen, id",
            (Platform.VIEWER.value,),
        ).fetchall()
        for r in rows:
            old = r["id"]
            new = Endpoint(Platform.VIEWER, r["url"], r["vendor"], r["label"]).id
            if new == old:
                continue
            if self.db.execute("SELECT 1 FROM endpoints WHERE id = ?", (new,)).fetchone():
                self.db.execute(
                    """UPDATE endpoints SET vendor = COALESCE(vendor, ?),
                         label = COALESCE(label, ?), first_seen = MIN(first_seen, ?)
                       WHERE id = ?""",
                    (r["vendor"], r["label"], r["first_seen"], new),
                )
                self.db.execute("DELETE FROM endpoints WHERE id = ?", (old,))
            else:
                self.db.execute("UPDATE endpoints SET id = ? WHERE id = ?", (new, old))
            # Tables keyed on endpoint_id keep the merged row's entry on a clash.
            for table in ("endpoint_jurisdictions", "assets", "asset_versions"):
                self.db.execute(
                    f"UPDATE OR IGNORE {table} SET endpoint_id = ? WHERE endpoint_id = ?",
                    (new, old),
                )
                self.db.execute(f"DELETE FROM {table} WHERE endpoint_id = ?", (old,))
            for table in ("candidates", "harvests", "checks"):
                self.db.execute(
                    f"UPDATE {table} SET endpoint_id = ? WHERE endpoint_id = ?", (new, old)
                )

    def close(self) -> None:
        self.db.commit()
        self.db.close()

    def commit(self) -> None:
        self.db.commit()

    def copy_to(self, path: Path | str) -> None:
        """Write a consistent copy of the whole catalog to `path`."""
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db.commit()
        dest = sqlite3.connect(str(path))
        try:
            self.db.backup(dest)
        finally:
            dest.close()

    # -- jurisdictions -------------------------------------------------------------

    def upsert_jurisdictions(self, rows: Iterable[Jurisdiction]) -> int:
        cur = self.db.executemany(
            """INSERT INTO jurisdictions (id, name, website, kind, state, source)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET name=excluded.name, website=excluded.website,
                 kind=COALESCE(excluded.kind, jurisdictions.kind),
                 state=COALESCE(excluded.state, jurisdictions.state), source=excluded.source""",
            [(j.id, j.name, j.website, j.kind, j.state, j.source) for j in rows],
        )
        self.db.commit()
        return cur.rowcount

    def jurisdictions_to_crawl(
        self, limit: int | None, recrawl: bool = False
    ) -> list[Jurisdiction]:
        where = "website IS NOT NULL" + ("" if recrawl else " AND last_crawled_at IS NULL")
        rows = self.db.execute(
            f"SELECT * FROM jurisdictions WHERE {where} ORDER BY last_crawled_at, id LIMIT ?",
            (limit if limit else -1,),
        ).fetchall()
        return [
            Jurisdiction(r["id"], r["name"], r["website"], r["kind"], r["state"], r["source"])
            for r in rows
        ]

    def mark_crawled(
        self, jurisdiction_id: str, *, pages: int | None = None, failure: str | None = None
    ) -> None:
        self.db.execute(
            """UPDATE jurisdictions SET last_crawled_at = ?, crawl_pages = ?, crawl_failure = ?
               WHERE id = ?""",
            (utcnow(), pages, failure, jurisdiction_id),
        )

    # -- candidates ----------------------------------------------------------------

    def add_candidate(self, c: Candidate) -> bool:
        cur = self.db.execute(
            """INSERT OR IGNORE INTO candidates (url, via, found_on, link_text, jurisdiction_id, first_seen)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (c.url, c.via, c.found_on, c.link_text, c.jurisdiction_id, utcnow()),
        )
        return cur.rowcount == 1

    def candidates_with_status(self, status: str, limit: int | None) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT * FROM candidates WHERE status = ? ORDER BY first_seen LIMIT ?",
            (status, limit if limit else -1),
        ).fetchall()

    def set_candidate_status(self, url: str, status: str, endpoint_id: str | None = None) -> None:
        self.db.execute(
            """UPDATE candidates SET status = ?, endpoint_id = COALESCE(?, endpoint_id),
                 probed_at = CASE WHEN ? = 'classified' THEN probed_at ELSE ? END
               WHERE url = ?""",
            (status, endpoint_id, status, utcnow(), url),
        )

    # -- endpoints -----------------------------------------------------------------

    def upsert_endpoint(
        self, ep: Endpoint, *, via: str | None = None, found_on: str | None = None
    ) -> str:
        self.db.execute(
            """INSERT INTO endpoints (id, platform, url, vendor, label, discovered_via, discovered_from, first_seen)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET vendor = COALESCE(endpoints.vendor, excluded.vendor),
                 label = COALESCE(endpoints.label, excluded.label)""",
            (ep.id, ep.platform.value, ep.url, ep.vendor, ep.label, via, found_on, utcnow()),
        )
        return ep.id

    def link_jurisdiction(
        self, endpoint: Endpoint, jurisdiction_id: str, evidence: str | None
    ) -> None:
        """Record that a jurisdiction points at this endpoint.

        `same_domain` when the endpoint sits on the jurisdiction's own domain (likely
        theirs); `linked` otherwise. Fifty towns linking to MassGIS doesn't make it theirs.
        """
        row = self.db.execute(
            "SELECT website FROM jurisdictions WHERE id = ?", (jurisdiction_id,)
        ).fetchone()
        site, host = _bare_host(row["website"] if row else None), _bare_host(endpoint.url)
        same = bool(site) and (host == site or host.endswith("." + site))
        self.db.execute(
            """INSERT INTO endpoint_jurisdictions (endpoint_id, jurisdiction_id, relation, evidence_url)
               VALUES (?, ?, ?, ?) ON CONFLICT DO NOTHING""",
            (endpoint.id, jurisdiction_id, "same_domain" if same else "linked", evidence),
        )

    def endpoints(
        self,
        *,
        platforms: Iterable[Platform] | None = None,
        ids: Iterable[str] | None = None,
        older_than: str | None = None,
        limit: int | None = None,
    ) -> list[Endpoint]:
        clauses, params = [], []
        if platforms:
            plats = [p.value for p in platforms]
            clauses.append(f"platform IN ({','.join('?' * len(plats))})")
            params += plats
        if ids:
            ids = list(ids)
            clauses.append(f"id IN ({','.join('?' * len(ids))})")
            params += ids
        if older_than:
            clauses.append("(last_harvest_at IS NULL OR last_harvest_at < ?)")
            params.append(older_than)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self.db.execute(
            f"""SELECT * FROM endpoints {where}
                ORDER BY last_harvest_at IS NOT NULL, last_harvest_at, first_seen LIMIT ?""",
            (*params, limit if limit else -1),
        ).fetchall()
        return [Endpoint(Platform(r["platform"]), r["url"], r["vendor"], r["label"]) for r in rows]

    # -- harvests and assets -------------------------------------------------------

    def start_harvest(self, endpoint_id: str, started_at: str | None = None) -> int:
        cur = self.db.execute(
            "INSERT INTO harvests (endpoint_id, started_at) VALUES (?, ?)",
            (endpoint_id, started_at or utcnow()),
        )
        self.db.commit()
        return cur.lastrowid

    def finish_harvest(
        self, harvest_id: int, endpoint_id: str, *, status: str, counts: dict, n_assets: int,
        n_candidates: int, errors: list[str], raw_path: str | None, finished_at: str | None = None,
    ) -> None:  # fmt: skip
        now = finished_at or utcnow()
        self.db.execute(
            """UPDATE harvests SET finished_at=?, status=?, n_assets=?, n_new=?, n_changed=?,
                 n_removed=?, n_candidates=?, errors=?, raw_path=? WHERE id=?""",
            (
                now, status, n_assets, counts["new"], counts["changed"], counts["removed"],
                n_candidates, json.dumps(errors[:50]) if errors else None, raw_path, harvest_id,
            ),
        )  # fmt: skip
        self.db.execute(
            "UPDATE endpoints SET last_harvest_at=?, last_harvest_status=? WHERE id=?",
            (now, status, endpoint_id),
        )
        self.db.commit()

    def apply_assets(
        self, endpoint_id: str, harvest_id: int, assets: Iterable[Asset], *, complete: bool,
        observed_at: str | None = None,
    ) -> dict[str, int]:  # fmt: skip
        """Write one harvest's assets and record what changed since the last one.

        Only a complete harvest can mark assets removed; after a partial one, missing
        assets may just be behind a request that failed. Even then, the first complete
        harvest to miss an asset only records the miss (`missed_at`): it's removed when
        the next complete harvest misses it too, and the miss is cleared if that one
        finds it. Servers behind out-of-sync load balancers otherwise report layers
        removed and returned on back-to-back runs. Partial harvests leave misses
        alone. `observed_at` defaults to now;
        a replay passes the original harvest's time so history keeps its dates.
        """
        now = observed_at or utcnow()
        existing = {
            r["key"]: (r["content_hash"], r["removed_at"], r["missed_at"])
            for r in self.db.execute(
                "SELECT key, content_hash, removed_at, missed_at FROM assets WHERE endpoint_id = ?",
                (endpoint_id,),
            )
        }
        counts = {"new": 0, "changed": 0, "removed": 0, "returned": 0}
        seen: set[str] = set()
        rows, versions = [], []
        for a in assets:
            if a.key in seen:
                continue
            seen.add(a.key)
            digest = a.content_hash()
            prev = existing.get(a.key)
            if prev is None:
                change = "new"
            elif prev[1] is not None:
                change = "returned"
            elif prev[0] != digest:
                change = "changed"
            else:
                change = None
            box = a.bbox or (None, None, None, None)
            rows.append(
                (
                    endpoint_id, a.key, a.kind, a.name, a.title, a.url, a.parent_key,
                    a.service_type, a.geometry_type, a.format, a.srid, *box, a.bbox_srid,
                    a.description,
                    a.source_updated_at.isoformat() if a.source_updated_at else None,
                    json.dumps(a.extra, default=str) if a.extra else None,
                    digest, now, now, None,
                )
            )  # fmt: skip
            if change:
                counts[change] += 1
                snapshot = json.dumps(a.as_dict(), default=str)
                versions.append((endpoint_id, a.key, harvest_id, now, change, digest, snapshot))

        updates = ", ".join(
            f"{c}=excluded.{c}"
            for c in ASSET_COLUMNS.replace(" ", "").split(",")
            if c not in ("endpoint_id", "key", "first_seen")
        )
        self.db.executemany(
            f"""INSERT INTO assets ({ASSET_COLUMNS}) VALUES ({",".join("?" * 23)})
                ON CONFLICT(endpoint_id, key) DO UPDATE SET {updates}""",
            rows,
        )
        if complete:
            found = [k for k, (_, _, missed) in existing.items() if missed and k in seen]
            missing = [
                (k, missed)
                for k, (_, removed, missed) in existing.items()
                if removed is None and k not in seen
            ]
            gone = [k for k, missed in missing if missed]
            self.db.executemany(
                "UPDATE assets SET missed_at = NULL WHERE endpoint_id = ? AND key = ?",
                [(endpoint_id, k) for k in found],
            )
            self.db.executemany(
                "UPDATE assets SET missed_at = ? WHERE endpoint_id = ? AND key = ?",
                [(now, endpoint_id, k) for k, missed in missing if not missed],
            )
            self.db.executemany(
                """UPDATE assets SET removed_at = ?, missed_at = NULL
                   WHERE endpoint_id = ? AND key = ?""",
                [(now, endpoint_id, k) for k in gone],
            )
            versions += [(endpoint_id, k, harvest_id, now, "removed", None, None) for k in gone]
            counts["removed"] = len(gone)
        self.db.executemany(
            "INSERT OR REPLACE INTO asset_versions VALUES (?, ?, ?, ?, ?, ?, ?)", versions
        )
        self.db.commit()
        return counts

    def forget_harvests(self, endpoint_id: str) -> None:
        """Drop an endpoint's harvests, assets and history, ready to rebuild them."""
        for table in ("asset_versions", "assets", "harvests"):
            self.db.execute(f"DELETE FROM {table} WHERE endpoint_id = ?", (endpoint_id,))
        self.db.execute(
            "UPDATE endpoints SET last_harvest_at = NULL, last_harvest_status = NULL WHERE id = ?",
            (endpoint_id,),
        )
        self.db.commit()

    def record_check(self, endpoint_id: str, resp, detail: str | None) -> None:
        self.db.execute(
            "INSERT INTO checks VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                endpoint_id, utcnow(), resp.status, int(resp.ok), int(resp.elapsed * 1000),
                int(resp.tls_insecure), detail,
            ),
        )  # fmt: skip

    # -- reporting -----------------------------------------------------------------

    def coverage(self) -> list[sqlite3.Row]:
        """Per state, how many jurisdictions fall in each discovery outcome.

        Each jurisdiction counts once, in the first group that fits: an endpoint on its
        own domain, only links to endpoints elsewhere, crawl failed, crawled and
        nothing found, not crawled yet.
        """
        return self.db.execute(
            """WITH found AS (
                 SELECT jurisdiction_id, MAX(relation = 'same_domain') AS own
                 FROM endpoint_jurisdictions GROUP BY 1)
               SELECT j.state, COUNT(*) AS total,
                 SUM(COALESCE(f.own = 1, 0)) AS own_domain,
                 SUM(COALESCE(f.own = 0, 0)) AS linked_only,
                 SUM(f.jurisdiction_id IS NULL AND j.crawl_failure IS NOT NULL) AS crawl_failed,
                 SUM(f.jurisdiction_id IS NULL AND j.last_crawled_at IS NOT NULL
                     AND j.crawl_failure IS NULL) AS nothing_found,
                 SUM(f.jurisdiction_id IS NULL AND j.last_crawled_at IS NULL) AS not_crawled
               FROM jurisdictions j LEFT JOIN found f ON f.jurisdiction_id = j.id
               GROUP BY 1 ORDER BY 2 DESC"""
        ).fetchall()

    def query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        return self.db.execute(sql, params).fetchall()
