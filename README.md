# Pravothai backend

FastAPI backend for the pravothai.org chat widget and its existing CRM.
Forked from AidarSig/axolotl-backend.

## Legal answers

Set `OPENAI_VECTOR_STORE_ID` to the existing Thai Law Bot vector store.
This enables Responses API with mandatory file search for each question.
An answer is shown only when the model reports sufficient information and
its evidence quotations are present in the returned search passages.
Missing information or a lookup failure offers the contact form instead.
This does not verify that the uploaded source materials are legally current.

The original general-purpose Axoloti mode is preserved when no vector store
is configured. **Always configure the vector store for the legal website.**

## Render configuration

- Docker, repository root, Singapore, Free compute.
- Health check: `/api/v1/health`; auto-deploy: Off.
- Required secrets: `DATABASE_URL`, `OPENAI_API_KEY`, `JWT_SECRET_KEY`,
  `DEFAULT_ADMIN_PASSWORD`; store these only in Render environment variables.
- Set `APP_ENV=production`, `USE_NULL_POOL=true`, `ENABLE_TELEGRAM_EXPORT=false`,
  `TELEGRAM_WEBHOOK_AUTOMATION_ENABLED=false`.
- Set `OPENAI_VECTOR_STORE_ID` to the existing store ID and
  `OPENAI_CHAT_MODEL=gpt-4o`; the old assistant ID is not used in this mode.
- Set `BASE_URL` to the actual deployed service URL.

Render Free may sleep when idle. OpenAI API usage is charged separately.
Back up the existing PostgreSQL database before first startup: this application
runs its existing startup migrations and operator seed.

## Tilda widget

Replace the old widget block with `pravothai-widget-tilda.html` after backend
verification. Update `API_BASE_URL` in that file to the verified service URL.
The widget offers required name, phone, email and question fields, retains
values on a failed submission and confirms only a saved CRM request.

`POST /api/v1/webhooks/web` accepts an ordinary message or a `contact` object
containing those four fields, plus the existing visitor ID and optional thread ID.
A contact submission saves the CRM card and question, flags the conversation
for a specialist and switches to manual mode. It also sends the saved name, phone,
email and question to the channel configured in `TELEGRAM_CHAT_ID`, using
`TELEGRAM_TOKEN`. These contact notifications work even with full conversation
export disabled. Telegram failures leave the request saved in CRM; pending
notifications retry on submission retry or application startup.

Delivery is at least once: a process crash after Telegram accepts a message but
before the database commit can cause a duplicate on retry. No messages are lost
from CRM when Telegram is unavailable.

## Checks

Python 3.11 or newer:

```sh
python -m pip install -r requirements.txt
python -m unittest discover -v
```

The checks use an in-memory database and simulated OpenAI HTTP responses.
They do not access the working CRM or require an API key.
