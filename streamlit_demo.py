#!/usr/bin/env python3
"""Phase 2 retrieval demo UI.

A THIN VISUAL WRAPPER around the existing retrieval layer. It imports
`retrieval.Retriever` and calls it; it contains no ranking, scoring, chunking
or embedding logic of its own, and it never rebuilds or writes the index.

    streamlit run streamlit_demo.py

This demonstrates what retrieval returns. It is not evidence that the results
are correct - judge that from the results themselves.
"""
from __future__ import annotations

import html
import sys
from pathlib import Path

import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent))
from retrieval import Retriever          # noqa: E402  (the only retrieval entry point)

DEFAULT_INDEX_DIR = "data/retrieval"

EXAMPLE_QUERIES = [
    ("en", "What are the eligibility requirements for a Banque Misr credit card?"),
    ("en", "What documents do I need to open an account at Banque Misr?"),
    ("en", "What is the annual fee on Banque Misr cards?"),
    ("ar", "ما هي رسوم بطاقات بنك مصر؟"),
    ("en", "Compare Banque Misr credit cards"),
]

LANGUAGE_CHOICES = {
    "Auto (no filter)": None,     # leaves existing retrieval behaviour unchanged
    "English": "en",
    "Arabic": "ar",
}

CSS = """
<style>
  .bm-card {
    border: 1px solid rgba(128,128,128,0.25);
    border-radius: 10px;
    padding: 1rem 1.1rem;
    margin-bottom: 0.9rem;
    background: rgba(128,128,128,0.04);
  }
  .bm-card-head {
    display: flex; align-items: center; gap: 0.6rem;
    flex-wrap: wrap; margin-bottom: 0.35rem;
  }
  .bm-rank {
    font-weight: 700; font-size: 0.95rem;
    background: rgba(128,128,128,0.18);
    border-radius: 6px; padding: 0.05rem 0.5rem;
  }
  .bm-title { font-weight: 600; font-size: 1.03rem; line-height: 1.35; }
  .bm-badge {
    font-size: 0.72rem; letter-spacing: 0.04em; text-transform: uppercase;
    border: 1px solid rgba(128,128,128,0.4); border-radius: 999px;
    padding: 0.05rem 0.55rem; opacity: 0.85;
  }
  .bm-section { font-size: 0.88rem; opacity: 0.8; margin-bottom: 0.5rem; }
  .bm-snippet { font-size: 0.95rem; line-height: 1.55; margin: 0.5rem 0 0.6rem; }
  .bm-snippet[dir="rtl"] { text-align: right; }
  .bm-meter-track {
    height: 7px; border-radius: 4px; background: rgba(128,128,128,0.2);
    overflow: hidden; margin: 0.1rem 0 0.15rem;
  }
  .bm-meter-fill { height: 100%; border-radius: 4px; background: currentColor; opacity: 0.65; }
  .bm-conf { font-size: 0.78rem; opacity: 0.75; }
  .bm-url { font-size: 0.82rem; word-break: break-all; }
</style>
"""


def _full_width(widget) -> dict:
    """Streamlit renamed `use_container_width` to `width` (deprecated after
    2025-12-31). Pick whichever this installation accepts, so the demo runs on
    old and new versions without a warning."""
    import inspect
    try:
        params = inspect.signature(widget).parameters
    except (TypeError, ValueError):
        return {}
    if "width" in params:
        return {"width": "stretch"}
    if "use_container_width" in params:
        return {"use_container_width": True}
    return {}


@st.cache_resource(show_spinner="Loading retrieval index…")
def load_retriever(index_dir: str) -> Retriever:
    """Loaded once per index path and reused across reruns (read-only)."""
    return Retriever(index_dir)


def is_rtl(text: str) -> bool:
    return any("؀" <= ch <= "ۿ" for ch in text or "")


def snippet(text: str, limit: int = 420) -> str:
    """Collapse whitespace and trim on a word boundary."""
    clean = " ".join((text or "").split())
    if len(clean) <= limit:
        return clean
    return clean[:limit].rsplit(" ", 1)[0] + " …"


def render_card(hit: dict) -> None:
    conf = float(hit.get("confidence") or 0.0)
    pct = round(conf * 100)
    lang = (hit.get("language") or "??").upper()
    body = snippet(hit.get("text", ""))
    direction = "rtl" if is_rtl(body) else "ltr"
    title = html.escape(hit.get("title") or "(untitled)")
    heading = html.escape(hit.get("section_heading") or "")
    url = hit.get("citation") or hit.get("source_url") or ""
    doc_type = (hit.get("document_type") or "html").upper()

    bits = [f'<div class="bm-card">',
            f'<div class="bm-card-head">',
            f'<span class="bm-rank">#{hit.get("rank", "?")}</span>',
            f'<span class="bm-title">{title}</span>',
            f'<span class="bm-badge">{html.escape(lang)}</span>',
            f'<span class="bm-badge">{html.escape(doc_type)}</span>',
            f'</div>']
    if heading:
        bits.append(f'<div class="bm-section">Section: {heading}</div>')
    if hit.get("pdf_page"):
        bits.append(f'<div class="bm-section">Page ~{hit["pdf_page"]} (estimated)</div>')
    bits.append(f'<div class="bm-meter-track">'
                f'<div class="bm-meter-fill" style="width:{max(2, pct)}%"></div></div>')
    bits.append(f'<div class="bm-conf">Confidence {pct}%</div>')
    bits.append(f'<div class="bm-snippet" dir="{direction}">'
                f'{html.escape(body)}</div>')
    if url:
        safe = html.escape(url, quote=True)
        bits.append(f'<div class="bm-url">🔗 <a href="{safe}" target="_blank" '
                    f'rel="noopener noreferrer">{safe}</a></div>')
    bits.append("</div>")
    st.markdown("".join(bits), unsafe_allow_html=True)


def run_query(retriever: Retriever, query: str, language: str | None,
              top_k: int) -> list[dict]:
    """The only call into retrieval. No logic is reimplemented here."""
    return retriever.retrieve(query, top_k=top_k, language=language)


def main() -> None:
    st.set_page_config(page_title="Banque Misr Research Assistant — Phase 2",
                       page_icon="🔎", layout="centered")
    st.markdown(CSS, unsafe_allow_html=True)

    st.title("Banque Misr Research Assistant — Phase 2 Retrieval Demo")
    st.caption("A local bilingual retrieval system over Banque Misr website and "
               "document data, using multilingual embeddings and hybrid search.")

    with st.sidebar:
        st.subheader("Settings")
        index_dir = st.text_input("Index directory", DEFAULT_INDEX_DIR)
        top_k = st.slider("Results to show", 1, 15, 5)
        st.caption("This UI is a read-only wrapper. It never rebuilds the index "
                   "or changes ranking.")

    try:
        retriever = load_retriever(index_dir)
    except FileNotFoundError:
        st.error(f"No retrieval index found at `{index_dir}`.")
        st.code("python -m retrieval.build", language="bash")
        st.stop()
    except Exception as exc:                                   # noqa: BLE001
        st.error(f"Could not load the index: {exc}")
        st.stop()

    # One piece of state: the query to run. An example button sets it and
    # Streamlit reruns; the form seeds its input from it and overwrites it on
    # submit. No other branching needed.
    st.session_state.setdefault("query", "")

    st.write("**Example queries**")
    cols = st.columns(3)
    for i, (_lang, example) in enumerate(EXAMPLE_QUERIES):
        label = example if len(example) <= 46 else example[:44] + "…"
        if cols[i % 3].button(label, key=f"ex{i}", **_full_width(st.button)):
            st.session_state["query"] = example

    with st.form("search"):
        typed = st.text_input("Your question (English or Arabic)",
                              value=st.session_state["query"],
                              placeholder="e.g. What documents do I need to open "
                                          "an account?")
        language_label = st.selectbox("Language filter",
                                      list(LANGUAGE_CHOICES.keys()), index=0)
        submitted = st.form_submit_button("Search", type="primary")

    if submitted:
        st.session_state["query"] = typed

    query = st.session_state["query"].strip()
    if not query:
        st.info("Enter a question above, or pick one of the examples.")
        return

    language = LANGUAGE_CHOICES[language_label]

    try:
        with st.spinner("Searching…"):
            results = run_query(retriever, query, language, top_k)
    except Exception as exc:                                   # noqa: BLE001
        st.error(f"Retrieval failed: {exc}")
        return

    if not results:
        st.warning("No results found.\n\n"
                   "The corpus may not contain this information, or the "
                   "language filter may have excluded everything. "
                   "Try **Auto (no filter)** or rephrase the question.")
        return

    filter_note = f" · language filter: {language_label}" if language else ""
    st.success(f"{len(results)} result{'s' if len(results) != 1 else ''} "
               f"retrieved{filter_note}")
    for hit in results:
        render_card(hit)

    with st.expander("Raw scores (for debugging)"):
        st.dataframe(
            [{"rank": h["rank"], "confidence": h["confidence"],
              "score": h["score"], "dense": h.get("dense_score"),
              "bm25": h.get("bm25_score"), "page_type": h.get("page_type"),
              "segment": h.get("segment"), "lang": h.get("language"),
              "chunk_id": h.get("chunk_id")} for h in results],
            **_full_width(st.dataframe))

    st.caption("This demo shows what the retrieval layer returns. It is not "
               "evidence that the results are correct — judge that from the "
               "results themselves.")


if __name__ == "__main__":
    main()
