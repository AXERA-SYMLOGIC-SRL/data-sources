# Loading reports into a table

Google Analytics and Meta Ads answer queries; they don't hand over "all the data". To have it in
your own database, `ReportLoader` runs a query and keeps the result in the `report_rows` table,
a first load for the history and short refreshes after that.

```python
from data_sources import StoreConfig, create_connector, create_store
from data_sources.ingest import ReportLoader

store = create_store(StoreConfig(driver="postgresql", url="postgresql+asyncpg://..."))
await store.connect()

async with create_connector(config) as connector:   # Analytics or Meta Ads
    loader = ReportLoader(store, "google_analytics:my-site")
    result = await loader.load(
        "daily-traffic",                              # the report's name in the table
        connector,
        ReportSpec(dimensions=["date", "country"], metrics=["sessions", "totalUsers"]),
        backfill_days=365,
    )
    result.rows, result.since, result.until
```

Run the same call on a schedule (daily is enough) and the table stays current.

## What a load does

- **Splitting by day is what makes it incremental.** A query with a daily split (Analytics: the
  `date` dimension; Meta: `time_increment=1`) is loaded by day. The first load backfills
  `backfill_days` (default 365) up to **yesterday**, because today is still filling in, in chunks of
  31 days so large histories are many small requests. Every later load starts a few days before
  the last day held and **replaces** that stretch, since the APIs revise recent days: 3 days for
  Analytics, 28 for Meta (conversions are attributed up to 28 days later).
- **A query with no daily split** (for example sessions per country over a fixed period) is
  replaced whole each time.
- **A period is replaced in one transaction.** If a load fails part-way, the periods that
  finished stay, the one that failed is untouched, and loading again resumes from the last day
  held. Loading twice never duplicates rows.
- **One table for everything.** The `connector` string you give `ReportLoader` is stored on each
  row, so several properties or ad accounts and several reports share `report_rows`.
- **The query decides the columns**, not the loader: put the dimensions and metrics you want in
  the spec. For Analytics a different `date_ranges` is ignored while loading by day (the loader sets
  the period); for Meta, `date_preset`, `since` and `until` likewise.

## The table

| Column | Meaning |
|---|---|
| `connector` | The name you gave `ReportLoader` |
| `report` | The report's name |
| `day` | The row's date, `NULL` for a report with no daily split |
| `data` | The row as JSON: `{"date": "20261022", "country": "Romania", "sessions": "104"}`. `jsonb` on PostgreSQL |
| `loaded_at` | When the row was written |

Every value in `data` is the API's string. Cast in SQL:

```sql
select day,
       data->>'country'                  as country,
       (data->>'sessions')::int          as sessions
from report_rows
where connector = 'google_analytics:my-site' and report = 'daily-traffic'
order by day desc;
```

For a report you query constantly, make a view over this, or copy it into a typed table of your
own. The loader stays generic on purpose: it works for any query without a schema per report.

## Notes

- The table is created by `ReportLoader.load` (`store.ensure_tables`); no migration is needed.
- `data_sources.ingest.TabularSource` is what a connector provides to be loadable
  (`revision_days`, `day_column(spec)`, `read_table(spec, since, until)`). Both connectors do.
- Quotas apply as always: a long backfill of a wide query is many API calls. Load a narrow query
  first, and expect Meta to throttle large ad accounts.
- Rows for days the API no longer has are kept until their period is reloaded and comes back
  empty, which then removes them.
