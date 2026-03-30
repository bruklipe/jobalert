# Company Job Watch

This workflow collects public job listings from official company career pages, filters them for entry-level fit, keeps only recent postings, and writes a digest for manual review.

## Current sources

- Stellantis
- Ford
- General Motors
- Magna
- Toyota
- Honda
- KEYENCE

## Run it

```bash
python3 /Users/ilvipeshku/Documents/Playground/scripts/company_job_watch.py --config /Users/ilvipeshku/Documents/Playground/config/company_watch.json
```

Run without sending mail:

```bash
python3 /Users/ilvipeshku/Documents/Playground/scripts/company_job_watch.py --config /Users/ilvipeshku/Documents/Playground/config/company_watch.json --no-email
```

## Outputs

- Summary: `/Users/ilvipeshku/Documents/Playground/output/company_watch/latest_summary.md`
- Full report: `/Users/ilvipeshku/Documents/Playground/output/company_watch/latest_report.md`
- JSON: `/Users/ilvipeshku/Documents/Playground/output/company_watch/latest_results.json`
- Raw collected jobs: `/Users/ilvipeshku/Documents/Playground/output/company_watch/collected_jobs.json`
- Seen-job state: `/Users/ilvipeshku/Documents/Playground/output/company_watch/state.json`

## Notes

- The workflow never applies to jobs. It only collects and ranks them for manual review.
- Ollama is enabled, but only runs on jobs that already look promising by rule-based screening.
- The digest is limited to jobs that are no more than 14 days old when a posting date or posting age is available from the source.
- A digest email is sent through Apple Mail to the addresses configured in `/Users/ilvipeshku/Documents/Playground/config/company_watch.json`.
- If you want the next run to treat every currently known job as new again, delete `/Users/ilvipeshku/Documents/Playground/output/company_watch/state.json` before rerunning.
- Toyota currently shows jobs in Saline, Michigan on its careers site, but this workflow only keeps roles that match the configured software/solutions/application-engineering profile.
