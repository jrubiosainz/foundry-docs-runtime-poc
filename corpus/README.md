# Synthetic corpus · runtime documentation

This directory contains a local generator for synthetic Demo Insurance documents for a Microsoft Foundry agent demo. It uses no real data, uploads nothing to Azure, and rebuilds `out/` from scratch.

Generated structure:

- `out/general/`: 24 general PDF documents, per-document JSON and `tables.json`.
- `out/customer/<customer_id>/`: customer documents by customer.
- `out/customers.json`: customers, products and document metadata. The `source` field shows the simulated origin: `dms` (document management system) or `bank` (bank channel).
- `out/ground_truth.json`: expected evaluation questions.
- `out/image_facts.json`: facts that appear only inside rasterized images.
- `out/corpus_stats.json`: global statistics.

Regenerate from the repository root:

```bash
.venv/bin/python corpus/generate_corpus.py
.venv/bin/python corpus/verify_corpus.py
```

- `out/` is fully versioned (PDFs included, about 47 MB), so it does not need to be regenerated for deployment.
- Regeneration requires Microsoft Edge (or Chrome, with `EDGE_BIN`): `pdf_renderer.py` uses it in headless mode to convert HTML to PDF.
- The generator uses a fixed seed, so it produces the same customers, documents and questions again.
- `scripts/deploy.sh` uploads the corpus to Blob with `scripts/upload_corpus.py`.
