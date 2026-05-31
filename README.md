# My Diet Telegram Bot

Telegram bot for daily nutrition tracking using pyTelegramBotAPI, Gemini Flash, and Supabase.

## Features

- Analyze food text descriptions.
- Analyze meal photos with Gemini Flash.
- Return structured nutrition estimates in the bot flow.
- Store daily totals in Supabase.
- Support weekday calorie target logic and Saturday cheat day logic.
- Deploy on Render with Telegram webhook mode.

## Files

- `main.py` - bot logic, Gemini integration, Supabase integration, Render webhook app.
- `schema.sql` - Supabase table creation script.
- `requirements.txt` - Python dependencies.
- `render.yaml` - Render infrastructure configuration.

## Supabase SQL

Run `schema.sql` in the Supabase SQL editor.

## Environment Variables

Set these in Render:

- `TELEGRAM_BOT_TOKEN`
- `GEMINI_API_KEY`
- `SUPABASE_URL`
- `SUPABASE_KEY`
- `WEBHOOK_BASE_URL`
- `APP_TIMEZONE`
- `BOT_MODE=webhook`

`WEBHOOK_BASE_URL` should be your Render app URL, for example:

`https://my-diet-telegram-bot.onrender.com`

## Local Run

Use polling locally:

```powershell
$env:BOT_MODE="polling"
python main.py
```

## Render Deploy

1. Push this project to GitHub.
2. In Render, create a new Blueprint or Web Service from the GitHub repository.
3. Render will detect `render.yaml`.
4. Add the required environment variables.
5. After deploy, verify `/healthz` returns `{"status":"ok","mode":"webhook"}`.
6. Send a message to the bot in Telegram.

## Push To GitHub

```powershell
git init
git add .
git commit -m "Initial nutrition bot"
git branch -M main
git remote add origin https://github.com/<your-user>/<your-repo>.git
git push -u origin main
```

## Notes About Render Free Plan

Render free web services can cold start after inactivity. For a Telegram bot this may still work, but delivery can be slower after idle periods.