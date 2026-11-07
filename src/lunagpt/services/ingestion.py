"""Document ingestion service with chunking and pgvector storage."""

import io
import time
import uuid
from pathlib import Path

from pypdf import PdfReader
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from lunagpt.models import Document, DocumentChunk
from lunagpt.services.embeddings import get_embedding


def _extract_text(filename: str, content_bytes: bytes) -> str:
    """Extract text from PDF, Markdown, or plain text."""
    suffix = Path(filename).suffix.lower()

    if suffix == ".pdf":
        reader = PdfReader(io.BytesIO(content_bytes))
        pages_text = [page.extract_text() or "" for page in reader.pages]
        return "\n\n".join(pages_text)

    try:
        return content_bytes.decode("utf-8")
    except UnicodeDecodeError:
        return content_bytes.decode("latin-1", errors="replace")


def _chunk_text(text: str, chunk_size: int = 1000, overlap: int = 150) -> list[str]:
    """Split text into overlapping semantic chunks."""
    paragraphs = text.split("\n\n")
    chunks: list[str] = []
    current_chunk: list[str] = []
    current_len = 0

    for para in paragraphs:
        para_clean = para.strip()
        if not para_clean:
            continue

        if current_len + len(para_clean) > chunk_size and current_chunk:
            chunks.append("\n\n".join(current_chunk))
            # Keep overlap if possible
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
