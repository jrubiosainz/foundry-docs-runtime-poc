# Prototype benchmark · 2026-10-01

Each run is a real conversation against an environment deployed with `scripts/deploy.sh` and its defaults: Sweden Central, `gpt-5.6-terra` (GlobalStandard, 150,000 TPM). `scripts/converse.py` runs it: first the greeting ("Good morning") and then the questions in `corpus/out/ground_truth.json` for that customer. Runs are sequential, one conversation at a time. There is one JSON line per run in `results/*.jsonl`, and the console **Measurements** tab reads those same files.

Answers are first scored by `check()`. It searches for the expected terms after normalizing case, accents, thousands separators, and English dates, and numbers must match whole numbers ("20" is not found in "2026"). I then manually reviewed every answer marked incorrect, and every count answer, because the checker does not recognize paraphrases.

**What each column measures**

- **Session setup.**
  - In the hosted agent, creating the session (the sandbox) and uploading `session.json` from the backend. In production, this can be done when the customer signs in, before the greeting.
  - In File Search, downloading the PDFs, creating the vector store, and waiting for it to be indexed.
- **Context ready.** The time the agent takes, from the greeting, to retrieve and prepare the customer documents (and index them in the index route).
- **Greeting first token.** Time to the first token of the greeting response, measured on the client. In the hosted agent it includes preparation, because it is triggered by "Good morning" (R4).
- **First token, median / p90.** First token of the questions that follow the greeting. The p90 is the value at position ⌊0.9·(n−1)⌋ of the sorted list; with 6 questions that is the second highest, so a single outlier does not show there and is reported in the notes.
- **Input (cache).** Input tokens for the last question and, in parentheses, how many were served from the prompt cache.

## Option 1 · Hosted agent (MAF) with documentation prepared at runtime

| Customer | Route | Docs · pages · MB | Est. tokens | Session setup | Context ready | Greeting first token | First token, median / p90 | Input (cache) | Correct by checker | After manual review |
|---|---|---|---|---|---|---|---|---|---|---|
| CLI-0001 | CAG | 11 · 22 · 1.3 | 6,915 | 11.5 s | 1.5 s | 3.9 s | 3.2 / 4.3 s ¹ | 8,662 (7,409) | 5/6 | 6/6 ² |
| CLI-0002 | CAG | 15 · 24 · 1.7 | 6,530 | 8.8 s | 1.6 s | 6.8 s | 3.8 / 5.8 s | 8,281 (7,062) | 5/6 | 5/6 + 1 ambiguous ³ |
| CLI-0003 | CAG | 7 · 16 · 0.9 | 3,718 | 9.9 s | 1.2 s | 7.0 s | 3.3 / 3.7 s | 5,410 (4,228) | 6/6 | 6/6 |
| CLI-0016 | CAG | 13 · 27 · 1.6 | 7,700 | 11.2 s | 1.7 s | 5.2 s | 2.2 / 2.7 s | 9,408 (8,216) | 6/6 | 6/6 |
| CLI-0099 | CAG (automatic) ⁴ | 75 · 252 · 9.8 | 41,861 | 11.7 s | 8.7 s | 13.8 s | 3.8 / 4.4 s | 40,389 (39,205) | 8/8 | 8/8 |
| CLI-0099 | index (forced) | 75 · 252 · 9.8 | 41,861 | 10.3 s | 11.9 s ⁵ | 13.9 s | 2.9 / 3.7 s | 9,443 (7,323) | 8/8 | 8/8 |

**Total: 38/40 by the checker and 39/40 after review.** The remaining one is an ambiguous question that the agent answered with correct facts, but from a different image.

Notes:

1. **Q0002** took 30.4 s to the first token (31.8 s in total), with a correct answer. It is the only outlier: the other 39 questions finished in 8.0 s or less.
2. **Q0003/Q0004** is a false negative. The question "What information appears in the attached graphic document in my case file?" covers two images. The agent described both: the authorization stamp (AUT-46VQP) and the odontogram in the estimate, "root canal on tooth 36" and "zirconia crown on tooth 46". The checker required the literal "36: molar root canal".
3. **Q0010/Q0011** is the same ambiguous question: this customer also has two graphic documents. The agent read the loss adjuster table (plumbing €420, painting €280, €700 net), facts that are only in that image, but the expected answer was the kitchen leak photo.
4. Run with `--pause 20` (20 s between questions); see **Capacity**. In a back-to-back run, the first six questions were answered correctly (first token median 3.0 s, context ready 7.3 s), and the last two failed with 429 (token rate limit).
5. 252 chunks indexed in AI Search in 3.9 s, out of the 11.9 s.

## Option 2 · Prompt agent with File Search over one vector store per session

| Customer | Files | Session setup (of which indexing) | Greeting first token | First token, median / p90 | Input (cache) | Correct by checker | After manual review |
|---|---|---|---|---|---|---|---|
| CLI-0001 | 11 | 16.2 s (10.9 s) | 2.3 s | 2.8 / 3.1 s | 18,324 (15,877) | 5/6 | 5/6: image fails |
| CLI-0002 | 15 | 19.1 s (13.5 s) | 2.6 s | 2.5 / 2.9 s | 17,507 (15,006) | 5/6 | 5/6: image fails |
| CLI-0003 | 7 | 15.0 s (9.7 s) | 8.9 s | 2.2 / 2.2 s | 18,073 (15,697) | 5/6 | 5/6: image fails |
| CLI-0099 ⁴ | 75 | 49.1 s (40.7 s) | 3.9 s | 5.5 / 7.6 s | 49,294 (41,079) | 7/8 | 7/8: image fails |

**Total: 22/26, and all 4 failures are real.**

- **Images (4 of 4 failed).** File Search indexes only text. For example, when asked about the photo with plate 3812-KLM, it answered that "the available document text does not identify the license plate shown".
- **Counts over 75 documents (3 of 3 correct in this run, but not stable).**
  - The first count, "How many fleet claim reports were there in 2026?" (20), needed 5 searches and 15.9 s to the first token.
  - In an earlier back-to-back run, aborted by a 429 at Q0182 and not included, the same question was answered "11".
  - Each search returns 8 chunks (`max_num_results=8` in the prototype; the tool supports up to 50), so the model does not see all documents at once.
- **Greeting first token.** It is low because ingestion already happened during setup. In the real flow, "Good morning" would trigger ingestion, and the first response would arrive after 15–49 s.
- **Input tokens.** They grow with conversation history: about 7,500 in the first question and 18,000 in the sixth; with CLI-0099, from 23,000 to 49,300. Between 64% and 68% of the input comes from the cache, compared with 97.6% in the CAG route with CLI-0099.

## Conclusions

- **Images (R6).**
  - The hosted agent reads images in both routes. In the index route, embedded images go in the cached prefix (up to `INDEX_MAX_IMAGE_TOKENS`, 30,000 by default), and the license-plate question works.
  - File Search does not see images.
- **Volume (R5).** With 75 documents and 252 pages:
  - The CAG route gets 8/8 right and serves 97.6% of the input from cache.
  - The index route gets 8/8 right with about 9,400 tokens per turn, 4.3 times fewer.
  - File Search gets 7/8 right; the image fails and counts are not stable.
- **Latency (R8).**
  - With the hosted agent, each question's first token arrives in 2.2–3.8 s and the full answer in 3.2–5.7 s (medians per conversation; maximum 8.0 s, apart from one 31.8 s outlier). For reference, the starting scenario mentioned 22–30 s per run in a previous proof of concept with another model, of which only 7–8 s were model time.
  - Document preparation costs 1.2–1.7 s for 7–15 documents, and 8.7 s (CAG) or 11.9 s (index) for 75.
  - File Search ingestion costs 15–19 s for 7–15 documents and 49 s for 75.
- **Hosted agent session setup (8.8–11.7 s).** This is session sandbox creation and is not part of the conversation. It can be launched when the customer signs in, before the greeting.
  - If not advanced, first contact (setup plus greeting first token, which includes preparation) takes 15.5–16.9 s with 7–15 documents and 24–26 s with 75: on the order of the 22 s reference.
  - From that point on, each question is answered in 3–6 s.
- **Capacity.**
  - The default deployment has 150,000 TPM. The TPM limit counts every input token, including those served from the cache: in the CAG route with 75 documents, each turn sends about 40,000 tokens even though only about 1,000 are new.
  - Back to back, the CAG run with CLI-0099 got a 429 on its last two turns, and a File Search run with CLI-0099 aborted with a 429. With 20 s between questions, both completed. That is why the CLI-0099 rows use `--pause 20`.
  - In an earlier environment with 333,000 TPM (DataZoneStandard), five parallel conversations, one of them in CAG with 75 documents, also exceeded the limit.
  - In production, TPM or PTU must be sized for real concurrency times tokens per turn. The index route reduces tokens per turn by about 4.3 times at the cost of about 3 s more preparation.

## Benchmark limitations

- There is a single run per configuration, so figures are indicative and not a statistical study.
- Timings were measured from a laptop in Spain against Sweden Central, and include network and laptop load.
- The corpus is synthetic ("Demo Insurance"): 24 general documents and 26 customers with 350 documents (744 pages), with generated PDFs that include images with facts that appear only in them.
- It has not been tested in West Europe or with a private network (R11).
