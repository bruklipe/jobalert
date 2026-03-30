# Indeed Entry-Level Triage Workflow

This starter is designed for a safe workflow:

- Pull in job leads from a folder, JSON export, or Indeed job alert emails via IMAP.
- Filter for entry-level roles and reject jobs that appear to require more than 1 year of experience.
- Optionally send the job text to your local Ollama model for a second-pass fit check.
- Save a Markdown report, a daily digest, JSON results, and a seen-job state file.
- Keep the final application step manual.
- Support macOS Keychain so your mail password does not need to live in the repo.

## Why this approach

As of March 27, 2026, Indeed's official job seeker guidelines say not to use third-party bots or other automated tools to apply for jobs, and they warn that using web forms on Indeed platforms is not allowed. The safe automation boundary is discovery, screening, and ranking.

Official sources:

- https://support.indeed.com/hc/en-in/articles/360028540531-Indeed-Jobseeker-Guidelines
- https://support.indeed.com/hc/en-in/articles/204488890-Starting-or-Stopping-Job-Alerts

## Quick start

1. Run the starter on sample data:

```bash
python3 /Users/ilvipeshku/Documents/Playground/scripts/indeed_job_triage.py --config /Users/ilvipeshku/Documents/Playground/config/job_triage.example.json
```

2. Open the generated report:

`/Users/ilvipeshku/Documents/Playground/output/job_triage/latest_report.md`

The daily digest lives at:

`/Users/ilvipeshku/Documents/Playground/output/job_triage/latest_summary.md`

3. To use real Indeed alerts safely on this Mac, run:

```bash
/Users/ilvipeshku/Documents/Playground/.venv/bin/python /Users/ilvipeshku/Documents/Playground/scripts/setup_job_triage_keychain.py
```

That script writes non-secret settings to `/Users/ilvipeshku/Documents/Playground/.env` and stores the password in macOS Keychain.

4. The current profile is tuned for these roles:

- Software Engineer
- Software Developer
- Frontend Developer
- Web Developer
- Solutions Engineer
- Technical Sales Engineer
- Sales Engineer

5. The current location filter includes:

- Michigan
- Texas
- Arizona
- Ohio
- North Carolina
- South Carolina
- Florida
- Remote

6. Local model screening is already enabled in `config/job_triage.json`. This machine already has local Ollama models available, including `llama3.1:8b`.

7. If IMAP is annoying with your mail provider, drop `.eml`, `.txt`, `.html`, or `.json` job alerts into:

`/Users/ilvipeshku/Documents/Playground/input/indeed_alerts`

## Notes

- The IMAP source works best if your job alerts are narrow enough that each email maps to a small set of relevant jobs.
- For U-M mail, `imap.gmail.com` is an inference based on U-M Google documentation. If direct IMAP auth fails, use the folder-based intake path above.
- If the email only includes a short snippet and not the full qualification block, we can add a second intake step later for saved job descriptions or pasted listing text.
- The workflow keeps a state file so it can surface only new jobs each run.
- The script never clicks Apply and never submits forms.
