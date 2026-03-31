# Painting Bid Watch

This workflow checks official procurement data for recent painting-related bid opportunities in Michigan, writes a report for manual review, and emails a daily digest.

## Current source

- SAM.gov Contract Opportunities public API

## Run it locally

Set a `PAINTING_WATCH_SAM_API_KEY` environment variable first, then run:

```bash
python3 /Users/ilvipeshku/Documents/Playground/scripts/painting_bid_watch.py --config /Users/ilvipeshku/Documents/Playground/config/painting_bid_watch.json
```

Run without sending mail:

```bash
python3 /Users/ilvipeshku/Documents/Playground/scripts/painting_bid_watch.py --config /Users/ilvipeshku/Documents/Playground/config/painting_bid_watch.json --no-email
```

## Outputs

- Summary: `/Users/ilvipeshku/Documents/Playground/output/painting_bid_watch/latest_summary.md`
- Full report: `/Users/ilvipeshku/Documents/Playground/output/painting_bid_watch/latest_report.md`
- JSON: `/Users/ilvipeshku/Documents/Playground/output/painting_bid_watch/latest_results.json`
- Raw collected matches: `/Users/ilvipeshku/Documents/Playground/output/painting_bid_watch/collected_opportunities.json`
- Seen-opportunity state: `/Users/ilvipeshku/Documents/Playground/output/painting_bid_watch/state.json`

## Notes

- The workflow only surfaces leads for review. It does not submit bids.
- The default scope is Michigan and the last 14 days of active notices.
- The current matching logic favors painting, repainting, coatings, wall covering, and NAICS `238320`.
- A daily email is sent to the same Yahoo and UMich addresses already used by the company job watch.
- To make GitHub Actions send this automatically from the cloud, add the `PAINTING_WATCH_SAM_API_KEY` repository secret.
- SAM.gov says its Opportunities API requires a public API key. Docs: `https://open.gsa.gov/api/get-opportunities-public-api/`
- As of March 30, 2026, GSA's public API docs say non-federal users with no SAM.gov role may only get 10 requests/day, so the default config is intentionally kept under that limit.
