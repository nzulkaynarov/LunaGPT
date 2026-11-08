"""Document ingestion service with chunking and pgvector storage."""

import io
import re
import time
import uuid
import xml.etree.ElementTree as ET
import zipfile
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from docx import Document as DocxDocument
from pypdf import PdfReader
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from lunagpt.models import Document, DocumentChunk
from lunagpt.services.embeddings import get_embedding, get_embeddings_batch


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
            return str(conv_result.document.export_to_markdown())
        finally:
            if temp_path.exists():
                temp_path.unlink()
    except Exception:
        return None


def extract_text(filename: str, content_bytes: bytes) -> str:
    """Extract text from PDF, DOCX, PPTX, XLSX, ODT, HTML, Markdown, or plain text."""
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

    # 3. DOCX extraction (.docx only, python-docx does not support binary .doc)
    if suffix == ".docx":
        try:
            doc = DocxDocument(io.BytesIO(content_bytes))
            paras = [p.text for p in doc.paragraphs if p.text.strip()]
            for table in doc.tables:
                for row in table.rows:
                    paras.append(" | ".join(cell.text.strip() for cell in row.cells))
            return "\n\n".join(paras)
        except Exception:
            pass

    # 4. PPTX / XLSX / ODT (XML-in-ZIP archives with zip-bomb safeguards)
    if suffix in (".pptx", ".xlsx", ".odt") or zipfile.is_zipfile(io.BytesIO(content_bytes)):
        try:
            with zipfile.ZipFile(io.BytesIO(content_bytes)) as zf:
                infolist = zf.infolist()
                if len(infolist) > 500:
                    return ""
                total_uncompressed = sum(info.file_size for info in infolist)
                if total_uncompressed > 100 * 1024 * 1024:  # 100MB limit
                    return ""

                extracted_parts: list[str] = []
                for name in sorted(zf.namelist()):
                    if name.endswith(".xml") and (
                        "slides/slide" in name
                        or "sharedStrings" in name
                        or "worksheets/sheet" in name
                        or name == "content.xml"
                        or name == "word/document.xml"
                    ):
                        xml_bytes = zf.read(name)
                        root = ET.fromstring(xml_bytes)
                        texts = [t.strip() for t in root.itertext() if t and t.strip()]
                        if texts:
                            extracted_parts.append(" ".join(texts))
            if extracted_parts:
                return "\n\n".join(extracted_parts)
        except Exception:
            pass

    # 5. HTML extraction
    if suffix in (".html", ".htm"):
        parser = _HTMLTextExtractor()
        try:
            html_content = content_bytes.decode("utf-8", errors="replace")
            parser.feed(html_content)
            return "".join(parser.text_parts)
        except Exception:
            pass

    # 6. Markdown / Plain Text
    try:
        return content_bytes.decode("utf-8")
    except UnicodeDecodeError:
        try:
            return content_bytes.decode("cp1251")
        except UnicodeDecodeError:
            return content_bytes.decode("latin-1", errors="replace")


_extract_text = extract_text


def _chunk_text(text: str, chunk_size: int = 1000, overlap: int = 150) -> list[str]:
    """Split text into overlapping semantic chunks preserving structure and sentence boundaries."""
    raw_paragraphs = re.split(r"\n\s*\n", text)
    # Further split any oversized paragraphs by sentence boundaries
    normalized_blocks: list[str] = []
    for p in raw_paragraphs:
        p_clean = p.strip()
        if not p_clean:
            continue
        if len(p_clean) <= chunk_size:
            normalized_blocks.append(p_clean)
        else:
            sentences = re.split(r"(?<=[.!?\n])\s+", p_clean)
            sub_block = ""
            for s in sentences:
                s_clean = s.strip()
                if not s_clean:
                    continue
                if len(sub_block) + len(s_clean) + 1 > chunk_size and sub_block:
                    normalized_blocks.append(sub_block)
                    sub_block = s_clean
                else:
                    sub_block = f"{sub_block} {s_clean}".strip()
            if sub_block:
                normalized_blocks.append(sub_block)

    chunks: list[str] = []
    current_chunk: list[str] = []
    current_len = 0

    for block in normalized_blocks:
        if current_len + len(block) > chunk_size and current_chunk:
            chunks.append("\n\n".join(current_chunk))
            if len(current_chunk) > 1:
                current_chunk = [current_chunk[-1], block]
                current_len = len(current_chunk[0]) + len(block)
            else:
                current_chunk = [block]
                current_len = len(block)
        else:
            current_chunk.append(block)
            current_len += len(block)

    if current_chunk:
        chunks.append("\n\n".join(current_chunk))

    return chunks if chunks else [text[:chunk_size]]


async def ingest_document(db: AsyncSession, filename: str, content_bytes: bytes) -> tuple[uuid.UUID, int, float]:
    """Ingest document, chunk it, compute embeddings in batch, and store in pgvector."""
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

    embeddings = await get_embeddings_batch(chunks)
    for idx, (chunk_content, emb) in enumerate(zip(chunks, embeddings, strict=True)):
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


async def search_document_chunks(
    db: AsyncSession,
    query: str,
    limit: int = 3,
    threshold: float = 0.65,
    query_emb: list[float] | None = None,
) -> list[DocumentChunk]:
    """
    Retrieve top-K most relevant chunks using cosine similarity.
    Only returns chunks where cosine similarity >= threshold (distance <= 1 - threshold).
    Supports precomputed embedding to avoid redundant vectorization.
    """
    if query_emb is None:
        query_emb = await get_embedding(query)
    dist_expr = DocumentChunk.embedding.cosine_distance(query_emb)

    max_distance = 1.0 - threshold
    stmt = select(DocumentChunk).where(dist_expr <= max_distance).order_by(dist_expr).limit(limit)
    result = await db.execute(stmt)
    chunks = result.scalars().all()
    return list(chunks)


async def list_documents(db: AsyncSession) -> list[dict[str, Any]]:
    """List all ingested documents and their chunk counts."""
    stmt = (
        select(
            Document.id,
            Document.title,
            Document.file_type,
            Document.created_at,
            func.count(DocumentChunk.id).label("chunks_count"),
        )
        .outerjoin(DocumentChunk, Document.id == DocumentChunk.document_id)
        .group_by(Document.id)
        .order_by(Document.created_at.desc())
    )
    result = await db.execute(stmt)
    docs = []
    for row in result.all():
        docs.append(
            {
                "id": str(row.id),
                "title": row.title,
                "file_type": row.file_type,
                "created_at": row.created_at.isoformat() if row.created_at else "",
                "chunks_count": int(row.chunks_count),
            }
        )
    return docs


async def delete_document(db: AsyncSession, document_id: uuid.UUID) -> bool:
    """Delete a document and its cascading chunks."""
    stmt = select(Document).where(Document.id == document_id)
    result = await db.execute(stmt)
    doc = result.scalar_one_or_none()
    if not doc:
        return False
    await db.delete(doc)
    await db.commit()
    return True
