from datetime import UTC, datetime

from gis_observer import changes, cli
from gis_observer.models import Asset, Endpoint, Platform
from gis_observer.store import Store

EP = Endpoint(Platform.ARCGIS_REST, "https://gis.town.gov/arcgis/rest/services")
OTHER = Endpoint(Platform.CKAN, "https://data.county.gov")


def _apply(store, ep, assets, at):
    hid = store.start_harvest(ep.id, at)
    store.apply_assets(ep.id, hid, assets, complete=True, observed_at=at)


def _check(store, ep, at, ok, status=200, detail=None):
    store.db.execute(
        "INSERT INTO checks VALUES (?, ?, ?, ?, ?, ?, ?)",
        (ep.id, at, status, int(ok), 10, 0, detail),
    )


def _catalog():
    store = Store(":memory:")
    store.upsert_endpoint(EP)
    store.upsert_endpoint(OTHER)
    parcels = Asset(key="p", kind="layer", name="Parcels", extra={"fields": [{"name": "OWNER"}]})
    zoning = Asset(key="z", kind="layer", name="Zoning")
    svc = Asset(key="s", kind="service", name="Town")
    _apply(store, EP, [parcels, zoning, svc], "2026-09-01T00:00:00+00:00")
    parcels2 = Asset(
        key="p", kind="layer", name="Parcels", bbox=(0, 0, 1, 1),
        source_updated_at=datetime(2026, 11, 1, tzinfo=UTC),
        extra={"fields": [{"name": "OWNER"}, {"name": "ZONE"}]},
    )  # fmt: skip
    _apply(store, EP, [parcels2, svc, Asset(key="t", kind="table", name="Owners")],
           "2026-11-02T00:00:00+00:00")  # fmt: skip
    _apply(store, OTHER, [Asset(key=str(i), kind="dataset") for i in range(5)],
           "2026-11-03T00:00:00+00:00")  # fmt: skip
    return store


def test_parse_since():
    now = datetime(2026, 11, 16, 12, tzinfo=UTC)
    assert changes.parse_since("30d", now) == datetime(2026, 10, 17, 12, tzinfo=UTC)
    assert changes.parse_since("12h", now) == datetime(2026, 11, 16, tzinfo=UTC)
    assert changes.parse_since("2w", now) == datetime(2026, 11, 2, 12, tzinfo=UTC)
    assert changes.parse_since("2026-11-01") == datetime(2026, 11, 1, tzinfo=UTC)
    assert changes.parse_since("lately") is None


def test_changes_by_endpoint_with_field_diffs_and_first_harvests():
    store = _catalog()
    result = changes.asset_changes(store, "2026-10-17T00:00:00+00:00")
    by_url = {e.endpoint_url: e for e in result}

    town = by_url[EP.url]
    assert town.first_harvest is None  # first harvest was in September
    rows = [(a["change"], a["kind"], a["name"], a["fields"]) for a in town.assets]
    assert rows == [
        ("changed", "layer", "Parcels", ["extent", "fields", "source_updated_at"]),
        ("new", "table", "Owners", []),
        ("removed", "layer", "Zoning", []),
    ]  # fmt: skip
    # Keys that are equal on both sides (bbox_srid here) are not listed.
    assert "extent_srid" not in changes.changed_fields(
        '{"bbox": null, "bbox_srid": null}', '{"bbox": [0, 0, 1, 1], "bbox_srid": null}'
    )

    county = by_url[OTHER.url]
    assert county.first_harvest == ("2026-11-03T00:00:00+00:00", 5)
    assert county.assets == []


def test_changes_filters():
    store = _catalog()
    since = "2026-08-01T00:00:00+00:00"
    layers = changes.asset_changes(store, since, kinds=["layer"])
    assert [e.endpoint_url for e in layers] == [EP.url]
    assert layers[0].first_harvest[1] == 2  # Parcels and Zoning, not the service
    assert {a["kind"] for a in layers[0].assets} == {"layer"}
    only = changes.asset_changes(store, since, platforms=["ckan"])
    assert [e.endpoint_url for e in only] == [OTHER.url]
    assert changes.asset_changes(store, since, ids=[EP.id])[0].endpoint_id == EP.id


def test_health_reports_down_and_recovered():
    store = _catalog()
    _check(store, EP, "2026-11-01T00:00:00+00:00", True)
    _check(store, EP, "2026-11-08T00:00:00+00:00", False, 503)
    _check(store, EP, "2026-11-15T00:00:00+00:00", False, 503)
    _check(store, OTHER, "2026-11-08T00:00:00+00:00", False, None, "timeout")
    _check(store, OTHER, "2026-11-15T00:00:00+00:00", True)

    found = changes.health(store, "2026-10-17T00:00:00+00:00")
    assert [(h.endpoint_url, h.state, h.failed, h.checks) for h in found] == [
        (EP.url, "down", 2, 3), (OTHER.url, "recovered", 1, 2),
    ]  # fmt: skip
    assert found[0].down_since == "2026-11-08T00:00:00+00:00"
    assert found[0].last_up_at == "2026-11-01T00:00:00+00:00"
    assert changes.health(store, "2026-11-16T00:00:00+00:00") == []


def test_changes_command(tmp_path, capsys):
    db = tmp_path / "c.sqlite"
    store = _catalog()
    _check(store, EP, "2026-11-08T00:00:00+00:00", False, 503)
    store.copy_to(db)
    assert cli.main(["--db", str(db), "changes", "--since", "2026-10-17"]) == 0
    out = capsys.readouterr().out
    assert "first harvest  5 assets" in out
    assert "changed  layer  Parcels  extent, fields, source_updated_at" in out
    assert "down" in out and "HTTP 503; never up" in out
    assert cli.main(["--db", str(db), "changes", "--since", "lately"]) == 2
