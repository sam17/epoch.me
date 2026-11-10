# lifesheet

Standalone Discord bot that prompts for and records daily life-metrics into
Postgres. It is the **sole writer** to the epoch.me `raw_data` table (the WHOOP
collector is the only other importer). Replaces the retired TypeScript
`telegram_bot/`.

## Files

- `lifesheet.py` — the bot: posts the daily metric questions to a Discord
  channel, parses replies, writes answers to Postgres.
- `service.py` — long-running supervisor (scheduling + restart) that runs
  `lifesheet.py`; the container's entrypoint (`CMD ["python", "service.py"]`).
- `lifesheet.json` — metric/question definitions (ids, prompts, categories).
- `Dockerfile` — `python:3.11-slim` + `discord.py` + `pg8000`.
- `docker-compose.yml` — single `lifesheet` service, `restart: unless-stopped`.

## Run

```bash
cp .env.example .env   # fill in DISCORD_BOT_TOKEN + DATABASE_URL
docker compose up -d --build
docker compose logs -f
```

### Environment

| var | where | meaning |
|-----|-------|---------|
| `DISCORD_BOT_TOKEN` | `.env` | Discord bot token |
| `DATABASE_URL` | `.env` | `postgres://user:pass@db:5432/dbname` (the epoch-db) |
| `LIFESHEET_JSON_URL` | `.env` | optional: fetch `lifesheet.json` from a URL |
| `LIFESHEET_CHANNEL_ID` | compose | Discord channel the bot posts to |
| `LIFESHEET_TZ` | compose | timezone for the daily schedule (e.g. `Asia/Kolkata`) |

## Deployment

Runs on eevee at `/opt/claw/lifesheet`, joined to the `epoch` network so it can
reach `epoch-db`. Not committed: `.env` (secrets), `data/` (runtime state).
