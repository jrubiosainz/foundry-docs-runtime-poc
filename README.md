# Customer documentation retrieved at runtime · Microsoft Foundry prototype

Prototype of a customer-service agent for an insurer, built with Microsoft Foundry. When the customer greets the agent ("Good morning"), the agent:

- **retrieves at that moment** that customer's documents (contracts, endorsements, receipts, claim reports, loss adjuster reports, photos…) from two document sources;
- prepares them;
- answers by combining them with the general product documents.

There is no periodic reindexing and no customer documents preloaded by customer.

> **All data is synthetic.** The insurer ("Demo Insurance"), the 26 customers and the 374 PDFs in the corpus are fictional and are generated with [`corpus/generate_corpus.py`](corpus/).

Two options are compared over the same infrastructure and corpus:

| | Option 1 · Hosted agent (Microsoft Agent Framework) | Option 2 · Prompt agent + File Search |
|---|---|---|
| Where documents are prepared | In the hosted agent session sandbox | In a temporary vector store per session, created by the backend |
| How they reach the model | **CAG route** up to about 100,000 tokens: everything in context with prompt caching. Above that, **index route**: text in AI Search filtered by session and images in context | File Search (chunk retrieval: 8 per search, `max_num_results=8`) |
| Images | Yes, in both routes | No: File Search indexes text only |
| Benchmark accuracy | 38/40 according to the checker; 39/40 + 1 ambiguous after review | 22/26: the 4 image questions fail, and counts over 75 documents are not stable |
| Preparation | About 1–2 s (7–15 docs) and 8 s (75 docs, CAG) | 15–19 s (7–15 docs) and 49 s (75 docs) |
| Isolation | 22/22 checks | Good with one store per session if the backend sets the `vector_store_id`. A shared store mixes customers |

**Recommendation: Option 1.**

- Covers images (R6), counts over many documents (R5), and latency (R8).
- Implements the CAG pattern ("everything in context") with prompt caching (R10), and automatically routes to index when volume requires it.
- File Search remains a no-code option for customers with text-only documentation and few documents. Even then, it needs a backend-controlled vector store per session.

Measurement details: [`results/benchmark.md`](results/benchmark.md).

## Contents

- [One-step deployment](#one-step-deployment)
- [Demo walkthrough](#demo-walkthrough)
- [Architecture](#architecture)
- [Scenario requirements and how the prototype covers them](#scenario-requirements-and-how-the-prototype-covers-them)
- [Customer isolation](#customer-isolation)
- [Scripts](#scripts)
- [Limitations and next steps](#limitations-and-next-steps)
- [Layout](#layout)

## One-step deployment

`scripts/deploy.sh` creates the resource group with everything needed and leaves both agents running. The first run takes about 19 minutes, more than half of it (about 11 minutes) creating AI Search. Rerunning it on an existing group takes about 5 minutes.

### Requirements

- An **Azure subscription** where you can create resources and **assign roles** on the resource group: Owner, or Contributor plus User Access Administrator (or Role Based Access Control Administrator).
- **Model quota** in the region: `gpt-5.6-terra` (GlobalStandard, 150,000 TPM by default) and `text-embedding-3-large` (GlobalStandard, 150,000 TPM). You can check it in the Foundry portal (**Quota**) or with `az cognitiveservices usage list -l <region>`.
- A **region with hosted agents** and those models. Default: `swedencentral`.
- [Azure CLI](https://learn.microsoft.com/cli/azure/install-azure-cli) signed in (`az login`).
- [Azure Developer CLI](https://learn.microsoft.com/azure/developer/azure-developer-cli/install-azd) 1.34 or later, signed in (`azd auth login`). The script installs the `azure.ai.agents` extension if missing.
- **Python 3.10–3.13** (3.12 recommended). If none is on `PATH` and you have [uv](https://docs.astral.sh/uv/), the script obtains Python 3.12 through it.
- macOS, Linux, or WSL (bash).

### Deploy

```bash
git clone https://github.com/<user>/foundry-docs-runtime-poc.git
cd foundry-docs-runtime-poc
az login
azd auth login
./scripts/deploy.sh
```

The script performs these steps, in order:

1. Creates the resource group.
2. Deploys [`infra/main.bicep`](infra/main.bicep): Foundry with its project and the two models, Storage, AI Search, Application Insights, the project connections, and roles.
3. Writes `.env` with the deployment outputs and creates the Python environment (`.venv`).
4. Uploads the synthetic corpus to Blob Storage.
5. Creates the AI Search indexes and indexes the general documents.
6. Deploys the hosted agent (Option 1) with `azd deploy` and assigns roles to its identity.
7. Creates the File Search prompt agent (Option 2) and its `customer_id` filter variant, which is used only by the isolation test.
8. Runs a smoke test with both agents.

It is idempotent: if a step fails (quota, role propagation…), fix the cause and run it again.

**Parameters** (environment variables, all optional):

| Variable | Default | Purpose |
|---|---|---|
| `RESOURCE_GROUP` | `rg-foundry-docs-runtime` | Resource group (created if it does not exist) |
| `LOCATION` | `swedencentral` | Region for all resources |
| `AZURE_SUBSCRIPTION_ID` | The active subscription in `az` | Subscription |
| `AZD_ENV_NAME` | `docs-poc` | azd environment |
| `CHAT_MODEL` / `CHAT_MODEL_VERSION` | `gpt-5.6-terra` / `2026-07-09` | Model for both agents |
| `CHAT_SKU` / `CHAT_CAPACITY` | `GlobalStandard` / `150` | Deployment type and thousands of TPM. `DataZoneStandard` keeps processing in the data zone (EU in European regions) |
| `EMBEDDING_SKU` / `EMBEDDING_CAPACITY` | `GlobalStandard` / `150` | Embeddings for AI Search |
| `TAGS` | `{}` | Additional JSON tags for the group and all resources |
| `NAME_SUFFIX` | Random, 12 characters | Suffix for resource names. Generated on the first run and kept in the group tag `docs-name-suffix`, so reruns reuse it |
| `PYTHON` | `python3.12`, if present | Interpreter used to create `.venv` |
| `SKIP_SMOKE_TEST` | `0` | `1` to skip the smoke test |

For example:

```bash
RESOURCE_GROUP=rg-docs-demo CHAT_CAPACITY=250 TAGS='{"owner":"demo-team"}' ./scripts/deploy.sh
```

> If your organization applies policies that require tags, pass them with `TAGS`. The prototype uses public endpoints with Entra ID and no keys: if a policy or automation disables public network access for Storage or AI Search, the agent will no longer be able to read the documents (you will see 403 errors).

### What is created

| Resource | Name | Notes |
|---|---|---|
| Foundry (AIServices) and project | `aif-docs-<suffix>` / `proj-docs` | No local authentication: everything uses Entra ID |
| Model deployments | `gpt-5.6-terra`, `text-embedding-3-large` | |
| Hosted agent | `docs-runtime-agent` | Option 1, deployed with `azd deploy` (code, no ACR) |
| Prompt agents | `docs-filesearch`, `docs-filesearch-shared` | Option 2. The `-shared` variant filters by `customer_id` and is used only by the F5 isolation test |
| Storage | `stdocs<suffix>`: containers `docs-dms` and `docs-bank` | Simulate the two document sources; no shared keys or anonymous access |
| AI Search (Basic, free semantic ranker) | `srch-docs-<suffix>`: `idx-docs-general`, `idx-docs-customer` | The customer index is empty between sessions |
| Log Analytics + Application Insights | `log-docs-<suffix>`, `appi-docs-<suffix>` | Connected to the project, without capturing message content |

The suffix is random and stored in the group tag `docs-name-suffix`: reruns reuse it, and a new deployment after `destroy.sh` gets new names. Reusing the name of a just-purged Foundry account is avoided on purpose, because its endpoint can keep answering 404 for a long time. **Cost:** the main fixed cost is AI Search Basic, which is billed hourly even when unused; models and agents are usage-based. Delete the group when finished.

### Delete

```bash
./scripts/destroy.sh
```

Deletes the resource group, purges the Foundry account (otherwise it remains in soft delete for 48 hours while retaining model quota), and removes local state (`.env` and the azd environment).

### Use an existing deployment

If the resources already exist (for example, in another team), copy `.env.example` to `.env`, fill in the values, create the environment with `python3.12 -m venv .venv && .venv/bin/pip install -r requirements.txt`, and use the scripts directly. To redeploy the hosted agent after changing its code: `azd deploy docs-runtime-agent --no-prompt`. The console pins the agent version when it starts, so restart it afterwards.

## Demo walkthrough

Start the console from the repository root and open http://127.0.0.1:8780:

```bash
.venv/bin/python demo/server.py
```

The console acts as the backend: it authenticates the customer (you choose one from a list), creates the agent session, and shows what happens on the server. It has three tabs:

- **Sessions:** open sessions by choosing the customer, engine (Option 1 or Option 2), and route (automatic, CAG, or index). Each session is a strip in the **Session bay** with step-by-step preparation, answers with sources, per-turn metrics, and the server timing breakdown.
- **Isolation:** results from the latest `scripts/test_isolation.py` run.
- **Measurements:** the benchmark (reads `results/*.jsonl`).

A walkthrough takes about 20 minutes:

1. **Typical customer, Option 1.** Open **CLI-0001** (Lucía Martín Serrano: Dental Plus and Health Complete, 11 documents) with Option 1 and the automatic route.
   - Session setup (creating the session sandbox) takes about 7–12 s. In production it would be launched when the customer signs in, before the greeting.
   - Click **Good morning**. You will see live preparation: documents retrieved in parallel from the two sources (document management system and bank channel), 11 documents and 22 pages prepared, CAG route, and context ready in 1.5–2 s. Nothing was preloaded.
   - Click the **General + customer** suggestion ("How much will the root canal in my dental estimate cost…?"). It combines the general conditions with the customer's estimate: €110 with Dental Plus. In **Cached** you will see that around 90% of the input comes from the prompt cache.
   - Ask "Which teeth are marked in the odontogram in my estimate?" The answer (teeth 36 and 46) exists only in an image inside the PDF.
2. **Live isolation.** Without closing the previous session, open **CLI-0002** (Javier García Núñez) and greet the agent. Return to Lucía's strip and click **Other customer**: the agent cannot provide Javier's data because there is none in Lucía's sandbox. Close Javier's session: the backend deletes the session and its sandbox.
3. **Volume.** Open **CLI-0099** (Talleres Hermanos Ruiz, S.L.: 75 documents, 252 pages, 9.8 MB) with the automatic route and greet the agent.
   - About 42,000 tokens: it still fits in CAG, and the context is ready in about 7–9 s.
   - "How many loss adjuster reports are there?" → 10. This is a count over the whole case file, with 98% of the input cached.
   - **Image fact** ("What license plate appears immobilized…?") → 3812-KLM.
   - Optional: open it again with **Force index**. It indexes the 252 chunks in AI Search with a session filter and answers the same with about 9,000 tokens per turn (about 4.3 times fewer).
4. **Option 2.** Open **CLI-0001** with **Option 2 · Prompt agent + File Search** and greet the agent. Ingestion (downloading the PDFs, creating the vector store, and indexing it) happens when greeting and takes 15–20 s, sometimes much longer depending on service load. Repeat the odontogram question: File Search cannot see the image and does not answer it.
5. **Isolation tab** and **Measurements tab**, to close with the test and benchmark results.

Before presenting, rehearse: `.venv/bin/python scripts/demo_rehearsal.py` runs these scenarios against the console (about 3 minutes), checks answers, and verifies that no sessions, chunks, or vector stores remain at the end. `--quick` only warms up the hosted agent; after hours without use, the first session is cold and takes longer.

## Architecture

```
 External customer (no Entra ID)        Backend  (in the prototype: demo/server.py + scripts/sessions.py)
 "Good morning" ───────────────────▶   authenticates the customer → customer_id
                                         1. creates the hosted agent session (own sandbox, persistent $HOME)
                                         2. uploads session.json {customer_id, name, products}
                                            └─ identity is NEVER taken from chat
                                         3. on close: deletes the session and its chunks from the customer index
                                                   │ Responses API (streaming)
                                                   ▼
 ┌──────────────── Foundry · hosted agent "docs-runtime-agent" (MAF, Python 3.13, 1 vCPU / 2 GiB) ────────────────┐
 │ First message in the session:                                                                                  │
 │   a) downloads the customer's PDFs in parallel ──▶ Blob docs-dms/customer/<id>   (document management system) │
 │                                                ──▶ Blob docs-bank/customer/<id>  (bank channel,          │
 │                                                                                           +350 ms simulated)   │
 │   b) prepares each PDF: text per page + embedded images (full PDF when pages have no text)                     │
 │   c) route by estimated tokens:                                                                                │
 │        CAG   ≤ 100,000 → document block before the conversation + prompt-cache breakpoint                      │
 │        index > 100,000 → chunked text to AI Search idx-docs-customer (session_id + customer_id)                │
 │                           and images in the cached block (up to INDEX_MAX_IMAGE_TOKENS)                        │
 │ Each turn: automatic retrieval of the general conditions for the customer's lines ──▶ AI Search idx-docs-general│
 │            (and, in the index route, customer chunks with a session filter set by code)                        │
 │            + tools search_general_conditions / search_customer_documents                                      │
 │            + model gpt-5.6-terra (low reasoning)                                                              │
 │ Telemetry: OpenTelemetry → Application Insights (spans docs.init.*, docs.tool.*), no message content           │
 └────────────────────────────────────────────────────────────────────────────────────────────────────────────────┘

 Option 2: the backend downloads the PDFs → creates a temporary vector store (expires after one day of inactivity) → waits for
           indexing → prompt agent "docs-filesearch" with File Search over ["{{vector_store_id}}"] (structured input set by
           the backend) + Azure AI Search tool over idx-docs-general → on close, deletes the store and files.
```

Design decisions:

- **Documents are not in the system prompt (R7).**
  - They are passed as a data block before the conversation. They are not stored in history: the block is prepended on every turn with a stable prefix, which enables prompt caching.
  - The cache key is per customer: `docs:<customer>:<document hash>` (`prompt_cache_key`).
  - The instructions explicitly state that document content is data, never commands.
- **Prepared format.** Text per page plus embedded images.
  - It costs about 150–300 tokens per page, versus about 1,600 if the raw PDF is sent as `input_file` (the model renders each page). With CLI-0001's 22 pages, that is about 6,100 input tokens versus 35,700 (`scripts/exp_prepared.py`).
  - This lets a 75-document customer fit in about 42,000 tokens and be served 97–98% from cache.
- **Automatic route with threshold.** `CAG_MAX_TOKENS=100000` and `CAG_MAX_MB=40`, configurable through environment variables in `azure.yaml`. The demo can force the route with the `route` field in `session.json`, which is only for testing.
- **Isolation in the index route.** The filter `session_id eq … and customer_id eq …` is built by agent code from session state; the model never chooses it. When the session closes, the backend deletes the chunks (`sessions.purge_session_chunks`).
- **Sensitive data outside telemetry.** `OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=false` and `ENABLE_SENSITIVE_DATA=false`: traces carry timings and sizes, not document content.

## Scenario requirements and how the prototype covers them

| # | Requirement | How it is covered | Status |
|---|---|---|---|
| R1 | Stable general documents plus customer documents | 24 general conditions documents in `idx-docs-general`; customer documents are retrieved per session | ✅ |
| R2 | No periodicity: no scheduled refreshes (hourly, every 5 minutes…) | There is no indexer or prior load; everything happens when the customer connects | ✅ |
| R3 | External customers without Entra ID; several document sources, one of them a bank channel | The backend authenticates and deposits `session.json`; two sources (containers `docs-dms` and `docs-bank`, the latter with simulated latency) are retrieved in parallel | ✅ (simulated sources) |
| R4 | Early retrieval when greeting | "Good morning" triggers retrieval and preparation: about 1–2 s for 7–15 documents | ✅ |
| R5 | 5–15 documents per customer, with cases up to 75 | 25 synthetic customers with 7–15 documents, plus CLI-0099 with 75 documents and 252 pages: 8/8 in CAG and 8/8 in index | ✅ up to 75 documents (not tested in the millions-of-tokens range) |
| R6 | PDFs with images | Option 1 reads images in CAG and index (cards, photos, adjuster valuations); File Search does not | ✅ Option 1 · ❌ Option 2 |
| R7 | Do not put documents in the system prompt | Data block before the conversation, outside instructions and history | ✅ |
| R8 | Latency and breakdown with OpenTelemetry / Application Insights | Per question, first token in 2.2–3.8 s and full answer in 3.2–5.7 s (medians per conversation). The first contact (sandbox setup plus greeting with preparation) totals 15–26 s if setup is not advanced to sign-in. Phase breakdown in the console and spans in Application Insights, which arrive incomplete in hosted (see limitations) | ✅ |
| R9 | Customer isolation | Option 1: 22/22. Option 2: good with one store per session; with the wrong store or a shared store there is a leak. See below | ✅ Option 1 · ⚠️ Option 2 |
| R10 | A CAG approach (everything in context) and a decision tree | CAG with prompt cache and explicit breakpoint, plus automatic routing; decision tree below | ✅ |
| R11 | Specific region, private network (BYO VNet), and internal MCP tools | **Not covered.** The prototype uses public endpoints. Hosted agents support BYO VNet | ⏳ Next step |

### Decision tree by use case

1. **Is there relevant information only in images** (cards, photos, odontograms, scanned tables)?
   - Yes: Option 1, because File Search cannot see them.
2. **How many tokens does the customer's prepared documentation add up to?**
   - Up to about 100,000: **CAG** with cache. This is best for counts and cross-document questions ("How many receipts do I have?"), because the model sees everything.
   - More: **session index**, with images in context up to a budget.
     - If there were very many images, the next step would be describing them as text during indexing.
     - Cross-document questions depend on retrieval, although they worked here (8/8).
3. **Text-only documentation, few documents, and a preference for no agent code?**
   - Option 2 with one vector store per session, assuming:
     - 15–49 s ingestion;
     - counts that are not stable over many documents;
     - the backend sets the `vector_store_id`.
4. **Concurrency.**
   - The CAG route sends the full context on every turn, even when served from cache.
   - TPM or PTU must be sized for peak simultaneous conversations. The index route greatly reduces tokens per turn (about 4.3 times fewer with CLI-0099).

## Customer isolation

The results come from `scripts/test_isolation.py` (about 3 minutes; latest run in `results/isolation-latest.*`). They are also shown in the console **Isolation** tab.

| Test | What it checks | Result |
|---|---|---|
| H1–H2 | 3 concurrent hosted sessions in CAG: questions about own data, another customer, and instruction injection | 12/12 with no other-customer data |
| H3 | 2 sessions in the index route over the **same** index, plus a direct AI Search query with the session filter | 8/8 and 2/2: only the customer's own chunks |
| F1–F2 | File Search with one vector store per session, concurrent sessions | 8/8 with no other-customer data |
| F3 | Simulated programming error: the backend passes **another** customer's store | **LEAK in retrieval** in 2 of 2 questions: 16 and 8 chunks from the other customer. The model did not show them, but that is not a control |
| F4 | Shared store without filter | **MIX in retrieval** in 2 of 4 questions: 1 and 2 of the 8 retrieved chunks belong to another customer. Not shown in the answer |
| F5 | Shared store with `customer_id` filter (template in the prompt agent) | **FAIL** in 1 of 4 questions: despite the filter, 1 of the 8 retrieved chunks belongs to another customer. It varies between runs: in earlier runs (September 2026), other-customer data reached the **answer** in 1 of 4 questions, in both F4 and F5 |

Why a shared store with a filter is not enough: [`scripts/exp_filter.py`](scripts/exp_filter.py), result in [`results/filter-experiment.json`](results/filter-experiment.json).

- The project endpoint accepts attributes per file, but when reading them back it returns `null` for all 26 files, and `files.update` returns 404.
- A filter with a nonexistent value **still returns chunks**: the filter is ignored without an error.
- `vector_stores.search` is not supported with an Entra ID token.

This is what was observed in September and October 2026 in Sweden Central projects and may be a preview limitation, but it cannot be trusted today.

Conclusions:

- Vector stores are **project** resources and do not have permissions per end customer. The `x-agent-user-id` header identifies the caller (the backend, with its Entra ID identity), not the external customer. The application must guarantee isolation.
- **Option 1:** each session has its sandbox and its preparation. In the index route, the filter is applied by code and the backend deletes chunks on close.
- **Option 2:** use **one store per session**. The backend creates it and sets the `vector_store_id` from the authenticated identity, never from the channel, and deletes it on close (in addition to automatic expiration).

## Scripts

All are run from the repository root with the Python in `.venv`; they read the `.env` generated by `deploy.sh`.

| Script | Usage |
|---|---|
| `scripts/deploy.sh`, `scripts/destroy.sh` | Complete one-step deployment and deletion |
| `demo/server.py [--port 8780]` | Demo console |
| `scripts/converse.py CLI-0099 --engine hosted\|filesearch [--route cag\|index] [--max N] [--pause S] [--out results/x.jsonl]` | Scripted conversation with the customer's questions; feeds the benchmark. `--pause` waits S seconds before each question to stay under the deployment TPM limit |
| `scripts/test_isolation.py [--skip-hosted] [--skip-filesearch]` | Isolation tests H1–H3 and F1–F5 |
| `scripts/demo_rehearsal.py [--quick] [--scenarios typical,isolation,volume,index,filesearch] [--cleanup]` | Demo rehearsal against the console; `--cleanup` closes anything left open |
| `scripts/chat.py --session <sid> "…"` | Minimal hosted-agent client over the Responses protocol |
| `scripts/filesearch_agent.py create\|create-shared\|chat CLI-0002` | Creates an Option 2 prompt-agent version (or its `customer_id` filter variant for the F5 test), or chats from the console |
| `scripts/exp_filter.py` | Filter experiment in a shared vector store |
| `scripts/exp_prepared.py`, `scripts/exp_cag.py` | Experiments: raw PDF versus prepared format, and prompt caching |
| `corpus/generate_corpus.py`, `scripts/upload_corpus.py`, `scripts/index_general.py` | Generate the synthetic corpus, upload it to Blob, and create the indexes |
| `scripts/grant_agent_rbac.py` | Minimum roles for the hosted agent identity |

## Limitations and next steps

- **R11 is not covered.** Deployment with BYO VNet and private endpoints, plus integration with internal MCP tools, remains to be validated.
- **Hosted agents and File Search are in preview.** The filter conclusions come from what was observed in September and October 2026.
- **Hosted agent session setup takes about 7–12 s** (sandbox creation). It should be launched when the customer signs in, before the greeting. If not advanced, the first contact (setup plus greeting) totals 15–26 s.
- **Capacity.**
  - The default deployment uses 150,000 TPM: enough for the demo, not for concurrent load.
  - The TPM limit counts every input token, including those served from the cache. In the CAG route with 75 documents, each turn sends about 40,000 tokens.
  - Back to back, CLI-0099 got 429 errors (token rate limit) both in CAG and in File Search, so the benchmark runs it with `--pause 20`. In an earlier environment with 333,000 TPM (DataZoneStandard), five parallel conversations, one of them CAG with 75 documents, also exceeded the limit.
  - Size TPM or PTU for peak concurrency times tokens per turn. The index route sends about 4.3 times fewer tokens per turn.
- **Incomplete traces in hosted.**
  - In Application Insights, `docs.init` arrives in all sessions and `docs.turn` in almost every turn. However, phase spans (`docs.fetch.*`, `docs.init.prepare`) are missing in many.
  - The agent SDK (`azure-ai-agentserver-core`, `flush_spans`) warns that the sandbox may freeze after sending the response and before exporting the trace batch.
  - Pending: force export at the end of each turn. In the meantime, the complete breakdown is the console breakdown, measured inside the agent.
- **Synthetic data.** Repeat the benchmark with an anonymized sample of real documents, with their formats.
- **Retention in production.**
  - The backend deletes chunks when the session closes. As a safety net, add a `created_at` purge in the customer index; that is retention, not reindexing.
  - The hosted agent session sandbox is deleted after 30 days of inactivity, or earlier if the backend deletes it.
- **Evaluation.** The benchmark uses a checker based on expected terms. The next step is an evaluation set in Foundry (for example, `azd ai agent eval`).

## Layout

```
agents/docs-runtime-agent/   hosted agent (main.py, requirements.txt); azure.yaml at the root
corpus/                      synthetic corpus generator; corpus/out (PDF + JSON) is fully versioned
demo/                        demo console (server.py + static/)
infra/main.bicep             complete infrastructure (deployed by scripts/deploy.sh)
results/                     benchmark.md, *.jsonl (measurements), isolation-*, filter-experiment.json
scripts/                     deployment, utilities, tests, and experiments
requirements.txt             root environment: console and scripts
.env.example                 .env template (deploy.sh generates .env, which is not versioned)
```
