# Job Alert Watch

This project watches official company career pages for entry-level software, solutions engineering, and technical sales engineering roles in your target states, then emails a daily digest for manual review.

It also includes a separate painting bid watch that checks official procurement data for recent painting-related project leads and emails a daily digest for manual review.

## Current company sources

- Stellantis
- Ford
- General Motors
- Magna
- Toyota
- Honda
- KEYENCE

## Cloud run

The free cloud setup uses GitHub Actions with SMTP email delivery, so it keeps running even when your laptop is closed.

Key files:

- `.github/workflows/company-watch.yml`
- `.github/workflows/painting-bid-watch.yml`
- `config/company_watch.json`
- `config/painting_bid_watch.json`
- `scripts/company_job_watch.py`
- `scripts/painting_bid_watch.py`
- `docs/company_watch_cloud.md`
- `docs/painting_bid_watch.md`

## GitHub secrets

Add these repository secrets before you run the workflow:

- `JOB_WATCH_FROM_ADDRESS`
- `JOB_WATCH_SMTP_HOST`
- `JOB_WATCH_SMTP_PORT`
- `JOB_WATCH_SMTP_USERNAME`
- `JOB_WATCH_SMTP_PASSWORD`
- `JOB_WATCH_SMTP_USE_SSL`
- `PAINTING_WATCH_SAM_API_KEY`

## Local run

```bash
python3 scripts/company_job_watch.py --config config/company_watch.json
```

Run without email:

```bash
python3 scripts/company_job_watch.py --config config/company_watch.json --no-email
```

Painting bid watch:

```bash
python3 scripts/painting_bid_watch.py --config config/painting_bid_watch.json
```

Painting bid watch without email:

```bash
python3 scripts/painting_bid_watch.py --config config/painting_bid_watch.json --no-email
```
