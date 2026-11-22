import io
import re
from typing import List, Dict, Any, Tuple
import numpy as np
from pypdf import PdfReader
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity


def clean_text(text: str) -> str:
    """Normalize whitespace and remove non-printable characters."""
    text = re.sub(r'\s+', ' ', text)
    return text.strip()


def chunk_text(text: str, chunk_size: int = 600, overlap: int = 100) -> List[str]:
    """Splits a document text into overlapping segments."""
    cleaned = clean_text(text)
    if not cleaned:
        return []
    
    chunks = []
    start = 0
    total_len = len(cleaned)
    
    while start < total_len:
        end = min(start + chunk_size, total_len)
        # Try to break on a sentence or period if possible
        if end < total_len:
            last_period = cleaned.rfind('.', start, end)
            if last_period != -1 and last_period > start + (chunk_size // 2):
                end = last_period + 1
        
        chunk = cleaned[start:end].strip()
        if chunk:
            chunks.append(chunk)
        
        if end >= total_len:
            break
        start = max(start + 1, end - overlap)
        
    return chunks


class SingleDocumentRetriever:
    """
    Dedicated Vector Retriever for a single PDF document.
    Maintains independent vocabulary, TF-IDF weights, and chunk index.
    """
    def __init__(self, doc_name: str, raw_text: str, chunk_size: int = 600, overlap: int = 100):
        self.doc_name = doc_name
        self.raw_text = raw_text
        self.chunks: List[str] = chunk_text(raw_text, chunk_size=chunk_size, overlap=overlap)
        self.vectorizer: TfidfVectorizer | None = None
        self.tfidf_matrix = None
        self._build_index()

    def _build_index(self):
        """Builds TF-IDF vector index for document chunks."""
        if not self.chunks:
            return
        
        self.vectorizer = TfidfVectorizer(
            stop_words='english',
            ngram_range=(1, 2),
            lowercase=True
        )
        try:
            self.tfidf_matrix = self.vectorizer.fit_transform(self.chunks)
        except Exception:
            # Fallback for very small documents or punctuation-only text
            self.vectorizer = TfidfVectorizer(lowercase=True)
            self.tfidf_matrix = self.vectorizer.fit_transform(self.chunks)

    def retrieve(self, query: str, top_k: int = 3) -> List[Dict[str, Any]]:
        """
        Retrieves the top_k most relevant chunks for a given query from this document.
        """
        if not self.chunks or self.vectorizer is None or self.tfidf_matrix is None:
            return []
        
        cleaned_query = clean_text(query)
        if not cleaned_query:
            # Return first top_k chunks if empty query
            return [{"chunk": c, "score": 0.0, "doc_name": self.doc_name} for c in self.chunks[:top_k]]
        
        try:
            query_vec = self.vectorizer.transform([cleaned_query])
            scores = cosine_similarity(query_vec, self.tfidf_matrix).flatten()
            
            # Rank chunks by similarity score
            ranked_indices = np.argsort(scores)[::-1]
            results = []
            
            for idx in ranked_indices[:top_k]:
                score = float(scores[idx])
                results.append({
                    "chunk": self.chunks[idx],
                    "score": round(score, 4),
                    "doc_name": self.doc_name,
                    "chunk_id": int(idx)
                })
            
            # If all scores are 0.0, provide the first few chunks as fallback context
            if all(r["score"] == 0.0 for r in results) and len(self.chunks) > 0:
                results = [{
                    "chunk": self.chunks[i],
                    "score": 0.01,
                    "doc_name": self.doc_name,
                    "chunk_id": i
                } for i in range(min(top_k, len(self.chunks)))]
                
            return results
        except Exception:
            return [{"chunk": c, "score": 0.0, "doc_name": self.doc_name, "chunk_id": i} 
                    for i, c in enumerate(self.chunks[:top_k])]


class MultiPDFRetrieverCoordinator:
    """
    Coordinates separate retrievers across 2 to 3 uploaded PDF documents.
    Retrieves evidence from each document independently and prepares unified comparison prompts.
    """
    def __init__(self):
        self.retrievers: Dict[str, SingleDocumentRetriever] = {}
        self.doc_metadata: Dict[str, Dict[str, Any]] = {}

    def add_pdf(self, file_name: str, file_bytes: bytes, chunk_size: int = 600, overlap: int = 100) -> bool:
        """Parses a PDF from bytes, extracts text, and initializes a dedicated retriever."""
        try:
            reader = PdfReader(io.BytesIO(file_bytes))
            pages_text = []
            for i, page in enumerate(reader.pages):
                extracted = page.extract_text()
                if extracted:
                    pages_text.append(extracted)
            
            full_text = "\n\n".join(pages_text)
            if not full_text.strip():
                full_text = f"[Warning: Document '{file_name}' contains scanned or image-only content with no extractable text.]"
            
            retriever = SingleDocumentRetriever(
                doc_name=file_name,
                raw_text=full_text,
                chunk_size=chunk_size,
                overlap=overlap
            )
            
            self.retrievers[file_name] = retriever
            self.doc_metadata[file_name] = {
                "pages": len(reader.pages),
                "chars": len(full_text),
                "chunks": len(retriever.chunks)
            }
            return True
        except Exception as e:
            print(f"Error loading {file_name}: {e}")
            return False

    def retrieve_from_all(self, query: str, top_k: int = 3) -> Dict[str, List[Dict[str, Any]]]:
        """
        Executes query against each document's dedicated retriever.
        Returns a dictionary mapping document name to its retrieved chunks.
        """
        results_by_doc = {}
        for doc_name, retriever in self.retrievers.items():
            results_by_doc[doc_name] = retriever.retrieve(query=query, top_k=top_k)
        return results_by_doc

    def build_comparison_prompt(self, query: str, retrieved_data: Dict[str, List[Dict[str, Any]]]) -> str:
        """
        Constructs the unified comparison prompt requesting a structured comparison table.
        """
        prompt_sections = []
        doc_names = list(retrieved_data.keys())
        
        prompt_sections.append("### INSTRUCTIONS:")
        prompt_sections.append(
            "You are an expert technical and analytical document comparison specialist.\n"
            "Your objective is to compare the provided documents strictly based on the extracted context excerpts.\n"
            "Requirements:\n"
            "1. Output a comprehensive Markdown Comparison Table comparing the documents along all relevant dimensions, features, or metrics asked in the query.\n"
            "2. Table Header must contain: | Comparison Dimension / Criteria | " + " | ".join(f"{doc}" for doc in doc_names) + " |\n"
            "3. Every row should concisely contrast each document's stance, values, specifications, or policies.\n"
            "4. If a document does NOT mention a specific criterion, explicitly state 'Not Specified / Not Mentioned'.\n"
            "5. After the table, provide:\n"
            "   - **Key Similarities** (bullet points)\n"
            "   - **Key Differences & Trade-offs** (bullet points)\n"
            "   - **Overall Synthesis / Recommendation** (2-3 sentences summarizing the comparison)."
        )
        
        prompt_sections.append(f"\n### USER COMPARISON QUESTION:\n{query}\n")
        
        prompt_sections.append("### EXTRACTED DOCUMENT CONTEXTS:")
        for doc_name, chunks in retrieved_data.items():
            prompt_sections.append(f"\n--- [DOCUMENT: {doc_name}] ---")
            if not chunks:
                prompt_sections.append("(No relevant context found in this document for the given query)")
            else:
                for idx, c in enumerate(chunks, 1):
                    prompt_sections.append(f"[Excerpt {idx} (Relevance Score: {c['score']})]:\n{c['chunk']}")
        
        prompt_sections.append("\n### YOUR STRUCTURED COMPARISON REPORT (Markdown Table + Analysis):")
        return "\n".join(prompt_sections)
