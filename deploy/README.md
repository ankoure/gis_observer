# Running `cycle` weekly

`gis-observer cycle` runs one turn of the discovery loop: probe new candidates, harvest
endpoints that are due, probe the leads those harvests produced, harvest the endpoints
that turned up, then `check`. Run it on a machine that keeps `data/observer.sqlite`
and `data/raw/` between runs.

GitHub Actions is a poor fit: the database has to persist between runs, and a full
harvest can run past the 6-hour job limit.

## Environment

Copy `gis-observer.env.example` to `/etc/gis-observer.env` and keep
`GIS_OBSERVER_USER_AGENT` pointing at a contact URL, so server operators can find out
who is crawling them.

## systemd

```sh
sudo cp deploy/gis-observer.env.example /etc/gis-observer.env
sudo cp deploy/systemd/gis-observer-cycle.{service,timer} /etc/systemd/system/
sudoedit /etc/systemd/system/gis-observer-cycle.service   # WorkingDirectory, User, uv path
sudo systemctl daemon-reload
sudo systemctl enable --now gis-observer-cycle.timer

systemctl list-timers gis-observer-cycle.timer     # next run
sudo systemctl start gis-observer-cycle.service    # run one now
journalctl -u gis-observer-cycle -e                # logs and the summary
```

The timer fires Sundays around 03:00 and catches up if the machine was off. systemd
won't start a second run while one is still going.

## cron

```cron
# m h dom mon dow
0 3 * * 0  cd /opt/gis_observer && set -a && . /etc/gis-observer.env && set +a && flock -n data/cycle.lock uv run --frozen gis-observer cycle >> data/cycle.log 2>&1
```

`flock -n` skips the week's run if last week's is somehow still going.

## Limits

With no limits, each stage takes everything it can. To cap a run, pass per-stage
limits, e.g. `cycle --probe-limit 500 --harvest-limit 300 --lead-harvest-limit 200`.
Endpoints a jurisdiction links to are harvested before unlinked ones (such as the
2024 server import), so a capped run reaches what the crawler found first.
