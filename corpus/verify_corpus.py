from __future__ import annotations

import json
import re
from pathlib import Path

from pypdf import PdfReader


ROOT = Path(__file__).resolve().parent
OUT = ROOT / "out"


def load(name: str):
    return json.loads((OUT / name).read_text(encoding="utf-8"))


def norm(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip().lower()


def pdf_text(path: Path) -> tuple[int, str]:
    reader = PdfReader(str(path))
    text = "\n".join((p.extract_text() or "") for p in reader.pages)
    return len(reader.pages), text


def assert_true(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)


def main() -> None:
    customers = load("customers.json")
    gt = load("ground_truth.json")
    facts = load("image_facts.json")
    stats = load("corpus_stats.json")
    general_docs = json.loads((OUT / "general" / "general_docs.json").read_text(encoding="utf-8"))

    errors: list[str] = []
    def check(cond: bool, msg: str) -> None:
        if not cond:
            errors.append(msg)

    check(len(general_docs) == 24, f"general_docs count is {len(general_docs)}, expected 24")
    doc_ids: set[str] = set()
    pdf_texts: dict[tuple[str, str], str] = {}
    customer_pdf_text: dict[str, str] = {}

    for gd in general_docs:
        p = OUT / gd["relpath"]
        check(p.exists(), f"missing general PDF {gd['relpath']}")
        if p.exists():
            try:
                pages, text = pdf_text(p)
                check(pages == gd["pages"], f"page mismatch {gd['doc_id']}: json {gd['pages']} pdf {pages}")
                check(p.stat().st_size == gd["bytes"], f"byte mismatch {gd['doc_id']}")
                doc_ids.add(gd["doc_id"])
            except Exception as exc:
                errors.append(f"cannot open general PDF {gd['doc_id']}: {exc}")
        jp = p.with_suffix(".json")
        check(jp.exists(), f"missing general JSON {jp.relative_to(OUT)}")
        if jp.exists():
            data = json.loads(jp.read_text(encoding="utf-8"))
            check(len(data.get("pages", [])) == gd.get("pages"), f"general page json mismatch {gd['doc_id']}")

    regular = [c for c in customers if c["customer_id"] != "CLI-0099"]
    stress = next((c for c in customers if c["customer_id"] == "CLI-0099"), None)
    check(len(regular) == 25, f"regular customer count is {len(regular)}, expected 25")
    check(stress is not None, "missing CLI-0099")

    total_customer = 0
    total_pages = sum(g.get("pages", 0) for g in general_docs)
    total_bytes = sum(g.get("bytes", 0) for g in general_docs)
    for c in customers:
        cid = c["customer_id"]
        docs = c.get("docs", [])
        if cid == "CLI-0099":
            check(len(docs) == 75, f"CLI-0099 doc count {len(docs)}, expected 75")
        else:
            check(7 <= len(docs) <= 15, f"{cid} doc count {len(docs)} outside 7-15")
        c_pages = 0
        c_bytes = 0
        texts = []
        for d in docs:
            total_customer += 1
            doc_ids.add(d["doc_id"])
            p = OUT / d["relpath"]
            check(p.exists(), f"missing customer PDF {d['relpath']}")
            if p.exists():
                try:
                    pages, text = pdf_text(p)
                    pdf_texts[(cid, d["doc_id"])] = text
                    texts.append(text)
                    check(pages == d["pages"], f"page mismatch {d['doc_id']}: json {d['pages']} pdf {pages}")
                    check(p.stat().st_size == d["bytes"], f"byte mismatch {d['doc_id']}")
                    check(p.stat().st_size <= 1_500_000, f"{d['doc_id']} exceeds 1.5 MB")
                    if d.get("image_facts"):
                        n_images = sum(len(list(pg.images)) for pg in PdfReader(str(p)).pages)
                        check(n_images > 0, f"{d['doc_id']} has image facts but no embedded image")
                    c_pages += pages
                    c_bytes += p.stat().st_size
                except Exception as exc:
                    errors.append(f"cannot open customer PDF {d['doc_id']}: {exc}")
        customer_pdf_text[cid] = "\n".join(texts)
        if cid != "CLI-0099":
            check(15 <= c_pages <= 40, f"{cid} pages {c_pages} outside regular 15-40")
            check(c_bytes <= 12_000_000, f"{cid} bytes {c_bytes} exceeds 12 MB")
        else:
            check(c_pages >= 220, f"CLI-0099 pages {c_pages}, expected >=220")
        st = stats["per_customer"].get(cid)
        check(st is not None, f"missing stats for {cid}")
        if st:
            check(st["docs"] == len(docs), f"stats doc mismatch {cid}")
            check(st["pages"] == c_pages, f"stats page mismatch {cid}")
            check(st["bytes"] == c_bytes, f"stats byte mismatch {cid}")
        total_pages += c_pages
        total_bytes += c_bytes

    check(total_customer == stats["n_customer_docs"], "total customer docs mismatch")
    check(stats["n_general_docs"] == 24, "stats n_general_docs mismatch")
    check(stats["n_customers"] == 26, "stats n_customers mismatch")
    check(stats["total_pages"] == total_pages, "stats total_pages mismatch")
    check(stats["total_bytes"] == total_bytes, "stats total_bytes mismatch")

    facts_by_customer: dict[str, list[dict]] = {}
    for f in facts:
        facts_by_customer.setdefault(f["customer_id"], []).append(f)
        text = norm(customer_pdf_text.get(f["customer_id"], ""))
        check(norm(f["value"]) not in text, f"image-only fact leaks to text: {f['customer_id']} {f['fact_label']}={f['value']}")
        check(f["doc_id"] in doc_ids, f"image fact references unknown doc {f['doc_id']}")
    for c in regular:
        check(len(facts_by_customer.get(c["customer_id"], [])) >= 2, f"{c['customer_id']} has fewer than 2 image-only facts")

    for c in customers:
        owner_hits = 0
        for d in c["docs"]:
            if c["canary"] in pdf_texts.get((c["customer_id"], d["doc_id"]), ""):
                owner_hits += 1
        check(owner_hits == 1, f"{c['customer_id']} canary owner hits {owner_hits}, expected 1")
        for other in customers:
            if other["customer_id"] == c["customer_id"]:
                continue
            check(c["canary"] not in customer_pdf_text.get(other["customer_id"], ""), f"{c['customer_id']} canary appears in {other['customer_id']}")

    for q in gt:
        check(q.get("customer_id") in {c["customer_id"] for c in customers}, f"GT unknown customer {q.get('qid')}")
        for did in q.get("source_docs", []):
            check(did in doc_ids, f"GT {q.get('qid')} references unknown doc {did}")
        check(q.get("type") in {"single_doc", "cross_doc", "image_only", "general_plus_customer", "canary", "negative_other_customer"}, f"GT invalid type {q.get('qid')}")
        check(q.get("expected"), f"GT missing expected {q.get('qid')}")

    bank = 0
    for c in customers:
        for d in c["docs"]:
            bank += d["source"] == "bank"
    ratio = bank / max(1, total_customer)
    check(0.25 <= ratio <= 0.35, f"bank source ratio {ratio:.2%} outside 25-35%")

    if errors:
        print("FAILED")
        for err in errors:
            print("-", err)
        raise SystemExit(1)
    print("OK")
    print(f"general_docs=24 customers={len(customers)} regular=25 customer_docs={total_customer}")
    print(f"CLI-0099 docs=75 pages={stats['per_customer']['CLI-0099']['pages']}")
    print(f"total_pages={total_pages} total_bytes={total_bytes} bank_ratio={ratio:.2%}")
    print(f"ground_truth={len(gt)} image_facts={len(facts)}")


if __name__ == "__main__":
    main()
