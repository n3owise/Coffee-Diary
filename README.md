# Coffee Diary

Daily automation for the Coffee Diary Google Sheet.

The workflow reads the `Roster Checklist` tab, scrapes enabled roaster source URLs, updates `Master Sheet`, and writes to `Change Log`, `Run Log`, and `Errors`.

## Google Sheet

- Sheet ID: `1q7mjRmjI8ywrSXe1OU6oZ2jNOib0KHNoEK7i8rUH0vg`
- Daily schedule: `09:00 IST`
- GitHub Actions cron: `30 3 * * *`

## Required Google Setup

Only Google Sheets API is required. Google Drive API is not needed for this automation.

1. In Google Cloud, enable `Google Sheets API`.
2. Create a service account.
3. Create a JSON key for that service account.
4. Copy the `client_email` value from the JSON key.
5. Share the Coffee Diary Google Sheet with that `client_email` as `Editor`.
6. Add the full JSON key as a GitHub secret named `GOOGLE_SERVICE_ACCOUNT_JSON`.

Do not commit or paste the JSON key into the repository.

## Required GitHub Secrets

Add these in GitHub under `Settings` > `Secrets and variables` > `Actions`.

- `GOOGLE_SERVICE_ACCOUNT_JSON`: full service account JSON key.

Notifications are disabled. The workflow only updates the Google Sheet and writes run details into `Run Log` and `Errors`.

## Local Checks

```bash
python3 -m py_compile coffee_diary_automation.py
```

With credentials available locally:

```bash
export GOOGLE_SERVICE_ACCOUNT_JSON='{"type":"service_account",...}'
python3 coffee_diary_automation.py --check-config
python3 coffee_diary_automation.py --dry-run
```

## Repository Setup

```bash
git init
git add .
git commit -m "add coffee diary automation"
git branch -M main
git remote add origin https://github.com/n3owise/Coffee-Diary.git
git push -u origin main
```
