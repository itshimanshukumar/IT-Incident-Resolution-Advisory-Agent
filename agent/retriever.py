
import re
from pathlib import Path

from pypdf import PdfReader
from docx import Document
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

DATA_DIR = Path(__file__).parent.parent / "data"
SUPPORTED_EXTENSIONS = {".txt", ".pdf", ".docx"}

# Cached search index. Restart the server after changing the guide.
_chunks = None
_vectorizer = None
_tfidf_matrix = None
_embedding_model = None
_embeddings = None
_semantic_error = None


def read_document(path):
    """Extract text from a TXT, PDF, or DOCX file."""
    suffix = path.suffix.lower()

    if suffix == ".txt":
        return path.read_text(encoding="utf-8")

    if suffix == ".pdf":
        reader = PdfReader(str(path))
        return "\n\n".join(
            page.extract_text() or "" for page in reader.pages
        )

    if suffix == ".docx":
        document = Document(str(path))
        return "\n\n".join(
            paragraph.text
            for paragraph in document.paragraphs
            if paragraph.text.strip()
        )

    return ""


def split_into_chunks(text, source, max_chars=1000):
    """Split a document into sections without losing their source."""
    sections = re.split(r"\n\s*\n", text.strip())
    chunks = []

    for section in sections:
        section = section.strip()
        if not section:
            continue

        # Keep short sections together; split unusually long sections.
        parts = [
            section[i:i + max_chars]
            for i in range(0, len(section), max_chars)
        ]

        for part in parts:
            chunks.append({
                "source": source,
                "text": part,
                "service": _metadata_value(part, "SERVICE"),
                "os": _metadata_value(part, "OPERATING SYSTEM"),
            })

    return chunks


def _metadata_value(text, label):
    """Read a metadata line such as SERVICE: Wi-Fi."""
    pattern = rf"(?im)^\s*{re.escape(label)}\s*:\s*(.+?)\s*$"
    match = re.search(pattern, text)
    return match.group(1).strip() if match else ""


def load_chunks():
    """Load and split all supported IT guide files."""
    chunks = []

    if not DATA_DIR.exists():
        return chunks

    for path in sorted(DATA_DIR.glob("it_guide.*")):
        if path.suffix.lower() not in SUPPORTED_EXTENSIONS:
            continue

        try:
            text = read_document(path)
            if text.strip():
                chunks.extend(
                    split_into_chunks(text, path.name)
                )
        except Exception as exc:
            print(f"Could not read {path.name}: {exc}")

    return chunks


def _build_index():
    """Build the keyword index and try to load semantic embeddings."""
    global _chunks, _vectorizer, _tfidf_matrix
    global _embedding_model, _embeddings, _semantic_error

    if _chunks is not None:
        return

    _chunks = load_chunks()

    if not _chunks:
        return

    texts = [chunk["text"] for chunk in _chunks]

    _vectorizer = TfidfVectorizer(
        lowercase=True,
        ngram_range=(1, 2),
        stop_words="english",
    )
    _tfidf_matrix = _vectorizer.fit_transform(texts)

    # Semantic search is optional if the model cannot load.
    try:
        from sentence_transformers import SentenceTransformer

        _embedding_model = SentenceTransformer(
            "sentence-transformers/all-MiniLM-L6-v2"
        )
        _embeddings = _embedding_model.encode(
            texts,
            normalize_embeddings=True,
            show_progress_bar=False,
        )
    except Exception as exc:
        _semantic_error = str(exc)
        _embedding_model = None
        _embeddings = None
        print("Semantic search unavailable; using keyword search.")
        print(_semantic_error)


def retrieve_troubleshooting_steps(
    service,
    operating_system,
    symptom,
    top_k=5,
):
    """Return the most relevant troubleshooting sections."""
    if not service or not service.strip():
        return {
            "status": "validation_error",
            "message": "Service is required.",
            "results": [],
        }

    if not operating_system or not operating_system.strip():
        return {
            "status": "validation_error",
            "message": "Operating system is required.",
            "results": [],
        }

    if not symptom or not symptom.strip():
        return {
            "status": "validation_error",
            "message": "Symptom is required.",
            "results": [],
        }

    # 1. Build index MUST be called here so it loads chunks on test run
    _build_index()

    if not _chunks:
        return {
            "status": "no_guide",
            "message": "No readable it_guide.txt, PDF, or DOCX was found.",
            "results": [],
        }
        
    query = (
        f"SERVICE: {service}\n"
        f"OPERATING SYSTEM: {operating_system}\n"
        f"SYMPTOM: {symptom}"
    )

    query_vector = _vectorizer.transform([query])
    keyword_scores = cosine_similarity(
        query_vector, _tfidf_matrix
    ).ravel()

    semantic_scores = None
    if _embedding_model is not None and _embeddings is not None:
        query_embedding = _embedding_model.encode(
            [query],
            normalize_embeddings=True,
            show_progress_bar=False,
        )[0]
        semantic_scores = _embeddings @ query_embedding

    ranked = []

    for index, chunk in enumerate(_chunks):
        # Prefer sections explicitly labeled for another service or OS less.
        service_match = (
            not chunk["service"]
            or chunk["service"].casefold() == service.strip().casefold()
        )
        os_match = (
            not chunk["os"]
            or chunk["os"].casefold()
            == operating_system.strip().casefold()
        )

        keyword_score = float(keyword_scores[index])

        if semantic_scores is not None:
            semantic_score = max(0.0, float(semantic_scores[index]))
            score = 0.4 * keyword_score + 0.6 * semantic_score
            mode = "hybrid"
        else:
            semantic_score = None
            score = keyword_score
            mode = "keyword_only"

        if not service_match:
            score *= 0.25
        if not os_match:
            score *= 0.65

        ranked.append({
            "source": chunk["source"],
            "text": chunk["text"],
            "score": round(score, 4),
            "keyword_score": round(keyword_score, 4),
            "semantic_score": (
                round(semantic_score, 4)
                if semantic_score is not None else None
            ),
            "service_match": service_match,
            "os_match": os_match,
            "retrieval_mode": mode,
        })

    ranked.sort(key=lambda item: item["score"], reverse=True)
    results = ranked[:max(1, min(top_k, 10))]

    if not results or results[0]["score"] < 0.05:
        return {
            "status": "not_found",
            "message": "No sufficiently relevant guide section was found.",
            "results": [],
            "retrieval_mode": (
                "hybrid" if semantic_scores is not None
                else "keyword_only"
            ),
        }

    return {
        "status": "found",
        "message": "Relevant guide sections found.",
        "retrieval_mode": (
            "hybrid" if semantic_scores is not None
            else "keyword_only"
        ),
        "results": results,
    }

