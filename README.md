# Pravothai backend

FastAPI backend for the pravothai.org chat widget and its existing CRM.
Forked from AidarSig/axolotl-backend.

## Question routing and legal answers

The configured site mode first classifies the latest question in conversation context.
Clearly everyday questions use a general assistant limited to Thailand; live web
search is available for current weather, prices and schedules. Legal or mixed
questions, requests for a specialist and uncertain classification use the strict
legal path. Classification errors default to that stricter path.

The legal path checks official Thai government sources using live
Responses API web search at question time, with a restricted domain allowlist.
Royal Gazette and the Office of the Council of State provide legal texts;
MFA, Immigration Bureau and official embassies provide entry requirements.
The current date in Thailand is supplied to the model. It must check effective
dates, applicability and subsequent changes, rather than repeat an old summary.

- A supported simple answer is shown with verified official source links.
- A missing detail in a simple question triggers one clarification question.
- Unconfirmed information, unresolved contradictions or a complex individual
  case offers the existing contact form for a representative.

The backend rejects answers whose supplied URLs
are not official URLs actually returned by that question's web-search tool.
This validates provenance, not the legal correctness of every model statement;
live search cannot guarantee a complete search of all current Thai legislation.
The old static vector store is not used as authority for current rules.
For compatibility, a configured `OPENAI_VECTOR_STORE_ID` selects this legal mode.
The original general-purpose mode is preserved when that setting is empty.

The audience is Russian citizens: ordinary Russian passports are assumed for entry
questions unless a visitor states different circumstances; applicable conditions
must be explicit in the answer.

Web search is charged by OpenAI separately from free Render hosting. Legal
questions are capped at three search-tool calls and 2,500 output tokens (including reasoning). Legal search uses `gpt-5-mini`
for supported source metadata. Classification uses a short `gpt-4o` call; general
answers use `gpt-5-mini` with up to two search calls and 2,000 output tokens.
Other existing AI utilities retain their model setting.

## Render configuration

- Docker, repository root, Singapore, Free compute.
- Health check: `/api/v1/health`; auto-deploy: Off.
- Required secrets: `DATABASE_URL`, `OPENAI_API_KEY`, `JWT_SECRET_KEY`,
  `DEFAULT_ADMIN_PASSWORD`; store these only in Render environment variables.
- Set `APP_ENV=production`, `USE_NULL_POOL=true`, `ENABLE_TELEGRAM_EXPORT=true`,
  `TELEGRAM_WEBHOOK_AUTOMATION_ENABLED=false`.
- Keep `OPENAI_VECTOR_STORE_ID` configured to select legal mode and
  `OPENAI_CHAT_MODEL=gpt-4o`; the old assistant ID is not used in this mode.
- Set `BASE_URL` to the actual deployed service URL.

Render Free may sleep when idle. OpenAI API usage is charged separately.
Back up the existing PostgreSQL database before first startup: this application
runs its existing startup migrations and operator seed.

With `ENABLE_TELEGRAM_EXPORT=true`, all web dialogues are sent to the configured
Telegram channel after 10 minutes without a new message, even without a contact
submission. Already exported messages are skipped; a failed send stays pending
and startup retries pending dialogues. Render Free may pause background work.

## Tilda widget

Replace the old widget block with `pravothai-widget-tilda.html` after backend
verification. Update `API_BASE_URL` in that file to the verified service URL.
The contact form is inserted directly into the message history, below the bot
reply. The existing chat scrolls to reach every field and the submit button;
opening the form keeps the conversation and chat input visible. The widget offers required name,
phone, email and question fields, retains
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
