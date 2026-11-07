"""Document ingestion service with chunking and pgvector storage."""

import io
import re
import time
import uuid
from html.parser import HTMLParser
from pathlib import Path

from docx import Document as DocxDocument
from pypdf import PdfReader
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from lunagpt.models import Document, DocumentChunk
from lunagpt.services.embeddings import get_embedding


class _HTMLTextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.text_parts: list[str] = []
        self._skip = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in ("script", "style", "noscript"):
            self._skip = True
        elif tag in ("p", "div", "h1", "h2", "h3", "h4", "li", "tr"):
            self.text_parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style", "noscript"):
            self._skip = False

    def handle_data(self, data: str) -> None:
        if not self._skip and data.strip():
            self.text_parts.append(data)


def _extract_text_docling(filename: str, content_bytes: bytes) -> str | None:
    """Attempt extraction via Docling if installed, optimized for CPU."""
    try:
        from docling.datamodel.pipeline_options import PdfPipelineOptions
        from docling.document_converter import DocumentConverter, PdfFormatOption

        pipeline_options = PdfPipelineOptions(do_ocr=False)
        pipeline_options.ocr_options.num_threads = 6
        converter = DocumentConverter(
            format_options={
                PdfFormatOption.PIPELINE_OPTIONS: pipeline_options,
            }
        )
        temp_path = Path(f"/tmp/{uuid.uuid4()}_{filename}")
        temp_path.write_bytes(content_bytes)
        try:
            conv_result = converter.convert(temp_path)
            return conv_result.document.export_to_markdown()
        finally:
            if temp_path.exists():
                temp_path.unlink()
    except Exception:
        return None


def _extract_text(filename: str, content_bytes: bytes) -> str:
    """Extract text from PDF, DOCX, HTML, Markdown, or plain text."""
    # 1. Try Docling first if available
    docling_result = _extract_text_docling(filename, content_bytes)
    if docling_result:
        return docling_result

    suffix = Path(filename).suffix.lower()

    # 2. PDF extraction
    if suffix == ".pdf":
        reader = PdfReader(io.BytesIO(content_bytes))
        pages_text = [page.extract_text() or "" for page in reader.pages]
        return "\n\n".join(pages_text)

    # 3. DOCX extraction
    if suffix in (".docx", ".doc"):
        doc = DocxDocument(io.BytesIO(content_bytes))
        paras = [p.text for p in doc.paragraphs if p.text.strip()]
        for table in doc.tables:
            for row in table.rows:
                paras.append(" | ".join(cell.text.strip() for cell in row.cells))
        return "\n\n".join(paras)

    # 4. HTML extraction
    if suffix in (".html", ".htm"):
        parser = _HTMLTextExtractor()
        try:
            html_content = content_bytes.decode("utf-8", errors="replace")
            parser.feed(html_content)
            return "".join(parser.text_parts)
        except Exception:
            pass

    # 5. Markdown / Plain Text
    try:
        return content_bytes.decode("utf-8")
    except UnicodeDecodeError:
        return content_bytes.decode("latin-1", errors="replace")


def _chunk_text(text: str, chunk_size: int = 1000, overlap: int = 150) -> list[str]:
    """Split text into overlapping semantic chunks preserving structure."""
    paragraphs = re.split(r"\n\s*\n", text)
    chunks: list[str] = []
    current_chunk: list[str] = []
    current_len = 0

    for para in paragraphs:
        para_clean = para.strip()
        if not para_clean:
            continue

        if current_len + len(para_clean) > chunk_size and current_chunk:
            chunks.append("\n\n".join(current_chunk))
            if len(current_chunk) > 1:
                current_chunk = [current_chunk[-1], para_clean]
                current_len = len(current_chunk[0]) + len(para_clean)
            else:
                current_chunk = [para_clean]
                current_len = len(para_clean)
        else:
            current_chunk.append(para_clean)
            current_len += len(para_clean)

    if current_chunk:
        chunks.append("\n\n".join(current_chunk))

    return chunks if chunks else [text[:chunk_size]]


async def ingest_document(db: AsyncSession, filename: str, content_bytes: bytes) -> tuple[uuid.UUID, int, float]:
    """Ingest document, chunk it, compute embeddings, and store in pgvector."""
    start_time = time.time()
    raw_text = _extract_text(filename, content_bytes)
    chunks = _chunk_text(raw_text)

    doc_id = uuid.uuid4()
    doc = Document(
        id=doc_id,
        title=filename,
        file_type=Path(filename).suffix.lstrip("."),
        metadata_json={"chunks_count": len(chunks), "size_bytes": len(content_bytes)},
    )
    db.add(doc)
    await db.flush()

    for idx, chunk_content in enumerate(chunks):
        emb = await get_embedding(chunk_content)
        chunk_obj = DocumentChunk(
            id=uuid.uuid4(),
            document_id=doc_id,
            chunk_index=idx,
            content=chunk_content,
            embedding=emb,
        )
        db.add(chunk_obj)

    await db.commit()
    elapsed = time.time() - start_time
    return doc_id, len(chunks), round(elapsed, 2)


async def search_document_chunks(db: AsyncSession, query: str, limit: int = 3) -> list[DocumentChunk]:
    """Retrieve top-K most relevant chunks using cosine similarity."""
    query_emb = await get_embedding(query)

    stmt = select(DocumentChunk).order_by(DocumentChunk.embedding.cosine_distance(query_emb)).limit(limit)
    result = await db.execute(stmt)
    chunks = result.scalars().all()
    return list(chunks)
