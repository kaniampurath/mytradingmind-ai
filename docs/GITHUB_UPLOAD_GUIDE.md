# GitHub Upload Guide

Use this after creating an empty GitHub repository named `mytradingmind-ai`.

## Local Commit

```bash
git init
git add .
git status
git commit -m "Prepare mytradingmind.ai deployment"
```

Do not commit `.env`. It is already ignored.

## Connect Remote

```bash
git branch -M main
git remote add origin https://github.com/<your-org-or-user>/mytradingmind-ai.git
git push -u origin main
```

## After Push

On the GitHub repo page, confirm these files are present:

- `README.md`
- `docs/UBUNTU_DROPLET_DEPLOYMENT.md`
- `docs/INSTITUTIONAL_READINESS.md`
- `deploy/docker-compose.yml`
- `deploy/ubuntu.env.example`
- `deploy/Dockerfile`

Confirm these are absent:

- `.env`
- API keys
- local database passwords
- `logs/`
- `.venv/`

## Recommended GitHub Settings

- Enable branch protection on `main`.
- Require pull request review before merging.
- Add a GitHub Actions test workflow before live-money deployment.
- Store production secrets in DigitalOcean or GitHub Actions secrets, never in source files.
