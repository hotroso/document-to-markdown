"""Email (.eml and Outlook .msg) → Document adapter."""

from __future__ import annotations

import email
import re
from email import policy
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .html_adapter import html_to_blocks
from .models import Block, Document

EMAIL_SUFFIXES = {".eml", ".msg"}


def _safe_filename(name: str, fallback: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", (name or "").strip()).strip("._")
    return cleaned or fallback


def _plain_text_to_blocks(text: str, source: Dict[str, object]) -> List[Block]:
    blocks: List[Block] = []
    para_lines: List[str] = []
    quote_lines: List[str] = []

    def flush_para() -> None:
        if para_lines:
            joined = " ".join(line.strip() for line in para_lines if line.strip())
            if joined:
                blocks.append(Block("paragraph", joined, source=dict(source)))
            para_lines.clear()

    def flush_quote() -> None:
        if quote_lines:
            blocks.append(Block("blockquote", "\n".join(quote_lines), source=dict(source)))
            quote_lines.clear()

    for raw_line in text.replace("\r\n", "\n").replace("\r", "\n").splitlines():
        stripped = raw_line.strip()
        if not stripped:
            flush_para()
            flush_quote()
            continue
        if stripped.startswith(">"):
            flush_para()
            quote_lines.append(stripped.lstrip(">").strip())
        else:
            flush_quote()
            para_lines.append(stripped)
    flush_para()
    flush_quote()
    return blocks


def _add_email_headers_and_body(
    document: Document,
    headers: List[Tuple[str, str]],
    plain_body: str,
    html_body: str,
    attachments: List[Tuple[str, Optional[bytes], str]],
    output_assets: Optional[Path] = None,
) -> None:
    header_rows = [["Field", "Value"]] + [[k, v] for k, v in headers if v]
    if len(header_rows) > 1:
        document.add(Block("table", rows=header_rows, source={"section": "headers"}))

    use_html = bool(html_body.strip()) and (
        not plain_body.strip() or "<table" in html_body.lower()
    )
    if use_html:
        _, body_blocks = html_to_blocks(html_body, source_base={"section": "body"})
        for blk in body_blocks:
            document.add(blk)
    elif plain_body.strip():
        for blk in _plain_text_to_blocks(plain_body, {"section": "body"}):
            document.add(blk)
    else:
        document.warn("Email contains no readable text body.")

    if attachments:
        document.add(
            Block("heading", "Attachments", source={"section": "attachments"}, metadata={"level": 2})
        )
        rows = [["Filename", "Type", "Size"]]
        exported_images: List[Tuple[str, str]] = []
        for idx, (filename, payload, content_type) in enumerate(attachments, start=1):
            size_str = f"{len(payload):,} bytes" if payload is not None else "unknown"
            rows.append([filename, content_type or "application/octet-stream", size_str])
            if output_assets is not None and payload is not None:
                output_assets.mkdir(parents=True, exist_ok=True)
                safe = _safe_filename(filename, f"attachment-{idx:02d}.bin")
                dest = output_assets / safe
                dest.write_bytes(payload)
                if (content_type or "").lower().startswith("image/"):
                    exported_images.append((safe, filename))
        document.add(Block("table", rows=rows, source={"section": "attachments"}))
        for rel_name, alt in exported_images:
            document.add(
                Block(
                    "image",
                    rel_name,
                    source={"section": "attachments"},
                    metadata={"alt": alt},
                )
            )


def _convert_eml(path: Path, output_assets: Optional[Path] = None) -> Document:
    raw = path.read_bytes()
    msg = email.message_from_bytes(raw, policy=policy.default)

    subject = str(msg.get("subject") or "").strip()
    sender = str(msg.get("from") or "").strip()
    recipients = str(msg.get("to") or "").strip()
    cc = str(msg.get("cc") or "").strip()
    date_val = str(msg.get("date") or "").strip()

    plain_parts: List[str] = []
    html_parts: List[str] = []
    attachments: List[Tuple[str, Optional[bytes], str]] = []

    if msg.is_multipart():
        for part in msg.walk():
            if part.is_multipart():
                continue
            disp = (part.get_content_disposition() or "").lower()
            ctype = (part.get_content_type() or "").lower()
            filename = part.get_filename()
            if disp == "attachment" or filename:
                payload = part.get_payload(decode=True)
                attachments.append((filename or "attachment", payload, ctype))
                continue
            try:
                content = part.get_content()
            except Exception:
                payload = part.get_payload(decode=True)
                content = payload.decode("utf-8", errors="replace") if payload else ""
            if isinstance(content, bytes):
                content = content.decode("utf-8", errors="replace")
            if ctype == "text/plain":
                plain_parts.append(str(content))
            elif ctype == "text/html":
                html_parts.append(str(content))
    else:
        ctype = (msg.get_content_type() or "text/plain").lower()
        try:
            content = msg.get_content()
        except Exception:
            payload = msg.get_payload(decode=True)
            content = payload.decode("utf-8", errors="replace") if payload else ""
        if isinstance(content, bytes):
            content = content.decode("utf-8", errors="replace")
        if ctype == "text/html":
            html_parts.append(str(content))
        else:
            plain_parts.append(str(content))

    document = Document(
        str(path),
        "eml",
        subject or path.stem,
        metadata={
            "from": sender,
            "to": recipients,
            "date": date_val,
            "attachments": len(attachments),
        },
    )
    _add_email_headers_and_body(
        document,
        headers=[("From", sender), ("To", recipients), ("Cc", cc), ("Date", date_val)],
        plain_body="\n\n".join(plain_parts),
        html_body="\n".join(html_parts),
        attachments=attachments,
        output_assets=output_assets,
    )
    return document


def _ole_read_stream(ole, path_parts: List[str]) -> Optional[bytes]:
    if not ole.exists(path_parts):
        return None
    try:
        return ole.openstream(path_parts).read()
    except Exception:
        return None


def _ole_read_text_prop(ole, prefix: List[str], prop_id: str) -> str:
    """Read a MAPI Unicode (001F) or ANSI (001E) string property from an OLE2 .msg file."""
    data = _ole_read_stream(ole, prefix + [f"__substg1.0_{prop_id}001F"])
    if data is not None:
        return data.decode("utf-16-le", errors="replace").rstrip("\x00").strip()
    data = _ole_read_stream(ole, prefix + [f"__substg1.0_{prop_id}001E"])
    if data is not None:
        for enc in ("utf-8", "cp1252", "latin-1"):
            try:
                return data.decode(enc).rstrip("\x00").strip()
            except UnicodeDecodeError:
                continue
    return ""


def _ole_read_html_prop(ole, prefix: List[str]) -> str:
    data = _ole_read_stream(ole, prefix + ["__substg1.0_10130102"])
    if data is not None:
        for enc in ("utf-8", "cp1252", "latin-1"):
            try:
                return data.decode(enc).rstrip("\x00").strip()
            except UnicodeDecodeError:
                continue
    return _ole_read_text_prop(ole, prefix, "1013")


def _parse_transport_headers(raw_headers: str) -> Dict[str, str]:
    result: Dict[str, str] = {}
    if not raw_headers.strip():
        return result
    try:
        parsed = email.message_from_string(raw_headers, policy=policy.default)
        for key in ("from", "to", "cc", "date", "subject"):
            val = str(parsed.get(key) or "").strip()
            if val:
                result[key] = val
    except Exception:
        pass
    return result


def _convert_msg(path: Path, output_assets: Optional[Path] = None) -> Document:
    """Parse Outlook .msg (OLE2/MAPI) using permissive BSD-licensed olefile."""
    try:
        import olefile
    except ImportError as exc:
        raise RuntimeError(
            "Outlook .msg support requires olefile. "
            "Install it with: pip install 'document-to-markdown[outlook]' (or pip install olefile)"
        ) from exc

    with olefile.OleFileIO(str(path)) as ole:
        transport = _parse_transport_headers(_ole_read_text_prop(ole, [], "007D"))
        subject = _ole_read_text_prop(ole, [], "0037") or transport.get("subject", "")
        sender_name = _ole_read_text_prop(ole, [], "0C1A")
        sender_addr = (
            _ole_read_text_prop(ole, [], "0C1F")
            or _ole_read_text_prop(ole, [], "5D01")
            or _ole_read_text_prop(ole, [], "0065")
        )
        if sender_name and sender_addr and sender_addr.lower() not in sender_name.lower():
            sender = f"{sender_name} <{sender_addr}>"
        else:
            sender = sender_name or sender_addr or transport.get("from", "")

        recipients = _ole_read_text_prop(ole, [], "0E04") or transport.get("to", "")
        cc = _ole_read_text_prop(ole, [], "0E03") or transport.get("cc", "")
        date_val = transport.get("date", "")
        plain_body = _ole_read_text_prop(ole, [], "1000")
        html_body = _ole_read_html_prop(ole, [])

        # Discover attachment storages (__attach_version1.0_#XXXXXXXX)
        attach_dirs = sorted(
            {
                entry[0]
                for entry in ole.listdir(streams=True, storages=False)
                if entry and entry[0].startswith("__attach_version1.0_")
            }
        )
        attachments: List[Tuple[str, Optional[bytes], str]] = []
        for idx, folder in enumerate(attach_dirs, start=1):
            prefix = [folder]
            fname = (
                _ole_read_text_prop(ole, prefix, "3707")
                or _ole_read_text_prop(ole, prefix, "3704")
                or f"attachment-{idx:02d}"
            )
            mime = _ole_read_text_prop(ole, prefix, "370E") or "application/octet-stream"
            payload = _ole_read_stream(ole, prefix + ["__substg1.0_37010102"])
            attachments.append((fname, payload, mime))

    document = Document(
        str(path),
        "msg",
        subject or path.stem,
        metadata={
            "from": sender,
            "to": recipients,
            "date": date_val,
            "attachments": len(attachments),
        },
    )
    _add_email_headers_and_body(
        document,
        headers=[("From", sender), ("To", recipients), ("Cc", cc), ("Date", date_val)],
        plain_body=plain_body,
        html_body=html_body,
        attachments=attachments,
        output_assets=output_assets,
    )
    return document


def convert_email(path: Path, output_assets: Optional[Path] = None) -> Document:
    """Convert an .eml or Outlook .msg email file to a Document."""
    suffix = path.suffix.lower()
    if suffix == ".msg":
        return _convert_msg(path, output_assets=output_assets)
    return _convert_eml(path, output_assets=output_assets)
