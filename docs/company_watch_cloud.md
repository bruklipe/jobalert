# Company Watch Cloud Setup

This is the free cloud version of the job-watch workflow. It uses GitHub Actions for scheduling and direct SMTP for email delivery, so it does not depend on your laptop staying open.

## What is already prepared

- Scheduled workflow: `.github/workflows/company-watch.yml`
- Main script: `scripts/company_job_watch.py`
- Config: `config/company_watch.json`

## Recommended free host

- GitHub Actions

This is the simplest free-tier option for a twice-daily Python job like this one.

## What you still need

1. Put this project in a GitHub repository.
2. Add GitHub Actions secrets for SMTP sending.
3. Turn on GitHub Actions and allow workflow write access.
4. Run the workflow once from GitHub to test delivery.

## Required GitHub secrets

- `JOB_WATCH_FROM_ADDRESS`
  Use: `bruklipe@yahoo.com`
- `JOB_WATCH_SMTP_HOST`
  Use: `smtp.mail.yahoo.com`
- `JOB_WATCH_SMTP_PORT`
  Use: `465`
- `JOB_WATCH_SMTP_USE_SSL`
  Use: `true`
- `JOB_WATCH_SMTP_USERNAME`
  Use: `bruklipe@yahoo.com`
- `JOB_WATCH_SMTP_PASSWORD`
  Use: your Yahoo app password

## Important note about Yahoo

Do not use your normal Yahoo password here. Use a Yahoo app password for SMTP.

## Important note about cloud scoring

GitHub Actions does not have your local Ollama models. The cloud workflow disables Ollama and uses the rule-based scorer only.

## Schedule

The workflow is set up for:

- 8:30 AM America/Detroit
- 8:05 PM America/Detroit

GitHub Actions can delay or skip individual scheduled runs, so the workflow now checks every 30 minutes and only sends once per Detroit morning slot and once per Detroit evening slot. That gives it a retry window instead of relying on one exact minute.

The night run is a few minutes after 8 PM to avoid the busiest GitHub Actions queue window at the top of the hour.

## State persistence

The GitHub workflow commits `output/company_watch/state.json` back to the repository after a successful run. That keeps the "new jobs" memory across runs so you do not get the same postings re-emailed every day.

It also commits `output/company_watch/digest_state.json` so the schedule knows whether the current morning or evening digest has already been sent.

If email delivery fails, the state is not committed. That means the next run can retry the same new jobs instead of silently skipping them.

## GitHub repository setting to check

In your repository settings, open `Settings -> Actions -> General -> Workflow permissions` and make sure `Read and write permissions` is enabled so the workflow can push the updated state file.

## How to test in GitHub

After pushing to GitHub and adding secrets:

1. Open the repository on GitHub.
2. Go to `Actions`.
3. Open `Company Job Watch`.
4. Click `Run workflow`.

## Result

When it runs in GitHub Actions, the email is sent directly over SMTP. Your laptop can be closed and the email can still be delivered. The latest summary and report are also uploaded as workflow artifacts in the Actions run.
