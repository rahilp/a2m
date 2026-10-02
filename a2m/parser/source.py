"""The files of one bundle root, read safely.

:class:`BundleFiles` lists every file under a bundle root (``apiproxy/`` or
``sharedflowbundle/``) without following links, and loads XML with defusedxml
so untrusted bundle XML can never declare or expand entities or reach outside
the bundle. Every problem is a :class:`a2m.errors.BundleError` whose one-line
message names the bundle and the file.
"""

from __future__ import annotations

import codecs
import os
import stat
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from xml.parsers import expat

from defusedxml import DefusedXmlException
from defusedxml import ElementTree as DefusedET

from a2m import safefs
from a2m.errors import BundleError, UnsafeBundleError
from a2m.ir import XmlElement

# Apigee XML is a few levels deep; a document nested deeper is refused so the
# recursive conversion to the IR (and its JSON) cannot exhaust the stack.
MAX_XML_DEPTH = 100

_READ_FLAGS = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)


@dataclass(frozen=True, slots=True)
class XmlFile:
    """A parsed XML file: ``rel`` relative to the bundle root, ``text`` its verbatim text without a BOM."""

    rel: str
    root: ET.Element
    text: str


def one_line(text: str) -> str:
    """``text`` with every run of whitespace (newlines included) folded to one space."""
    return " ".join(text.split())


class BundleFiles:
    """Every plain file under one bundle root, as sorted paths relative to it."""

    def __init__(self, root_dir: Path, label: str) -> None:
        self.root_dir = root_dir
        self.root_name = root_dir.name
        self.label = label
        self.files: tuple[str, ...] = tuple(sorted(self._walk()))

    def error(self, message: str) -> BundleError:
        return BundleError(one_line(f"{self.label}: {message}"))

    def shown(self, rel: str) -> str:
        """``rel`` as the user sees it, from the bundle root folder: apiproxy/proxies/default.xml."""
        return f"{self.root_name}/{rel}"

    def in_folder(self, folder: str, suffix: str | None = None) -> list[str]:
        """Files directly inside ``folder`` (``""`` is the bundle root), optionally only those ending in ``suffix``."""
        parent = folder or "."
        return [
            rel
            for rel in self.files
            if str((path := PurePosixPath(rel)).parent) == parent and (suffix is None or path.suffix.lower() == suffix)
        ]

    def read_bytes(self, rel: str) -> bytes:
        try:
            fd = os.open(self.root_dir.joinpath(*rel.split("/")), _READ_FLAGS)
            with os.fdopen(fd, "rb") as fh:
                if not stat.S_ISREG(os.fstat(fh.fileno()).st_mode):
                    raise self.error(f"{self.shown(rel)} is no longer a plain file")
                return fh.read()
        except OSError as exc:
            raise self.error(f"cannot read {self.shown(rel)}: {exc.strerror or exc}") from None

    def load_xml(self, rel: str) -> XmlFile:
        """Parse ``rel`` as XML; entity declarations and external references are refused."""
        data = self.read_bytes(rel)
        shown = self.shown(rel)
        try:
            root = DefusedET.fromstring(data, forbid_dtd=False, forbid_entities=True, forbid_external=True)
        except DefusedXmlException as exc:
            raise self.error(
                f"{shown} declares XML entities or external references ({type(exc).__name__}); "
                "a2m does not expand them, so this bundle is not read"
            ) from None
        except ET.ParseError as exc:
            raise self.error(f"{shown} is not well-formed XML ({exc})") from None
        if _depth(root) > MAX_XML_DEPTH:
            raise self.error(f"{shown} nests XML elements more than {MAX_XML_DEPTH} levels deep")
        return XmlFile(rel, root, self._decode(rel, data))

    def _decode(self, rel: str, data: bytes) -> str:
        """The file's text, in its declared encoding (UTF-8 when none), without a byte order mark."""
        encoding = _declared_encoding(data) or "utf-8"
        if codecs.lookup(encoding).name == "utf-8":
            encoding = "utf-8-sig"
        try:
            return data.decode(encoding).removeprefix("﻿")
        except (LookupError, UnicodeDecodeError) as exc:
            raise self.error(f"{self.shown(rel)} cannot be read as {encoding} text ({exc})") from None

    def _walk(self) -> list[str]:
        """Every plain file under the bundle root; a link or special file refuses the bundle."""
        found: list[str] = []
        pending: list[tuple[Path, tuple[str, ...]]] = [(self.root_dir, ())]
        while pending:
            folder, parts = pending.pop()
            try:
                with os.scandir(folder) as entries:
                    listed = [(entry.name, os.lstat(entry.path)) for entry in entries]
            except OSError as exc:
                shown = self.shown("/".join(parts)) if parts else self.root_name
                raise self.error(f"cannot read {shown}: {exc.strerror or exc}") from None
            for name, info in listed:
                rel = (*parts, name)
                if safefs.is_link_like(info):
                    raise UnsafeBundleError(
                        one_line(f"{self.label}: {self.shown('/'.join(rel))} is a symbolic link; a2m does not follow links")
                    )
                if stat.S_ISDIR(info.st_mode):
                    pending.append((folder / name, rel))
                elif stat.S_ISREG(info.st_mode):
                    found.append("/".join(rel))
                else:
                    raise UnsafeBundleError(
                        one_line(f"{self.label}: {self.shown('/'.join(rel))} is not a plain file or folder")
                    )
        return found


class _Declaration(Exception):
    """Stops the prolog scan once the XML declaration (or the first element) is reached."""

    def __init__(self, encoding: str | None) -> None:
        super().__init__(encoding)
        self.encoding = encoding


def _declared_encoding(data: bytes) -> str | None:
    """The encoding named in the XML declaration, or None.

    Only called on bytes defusedxml already accepted (no entity declarations),
    and the scan stops at the declaration or the first element, whichever
    comes first.
    """
    parser = expat.ParserCreate()

    def declaration(_version: str | None, encoding: str | None, _standalone: int) -> None:
        raise _Declaration(encoding)

    def first_element(_name: str, _attributes: dict[str, str]) -> None:
        raise _Declaration(None)

    parser.XmlDeclHandler = declaration
    parser.StartElementHandler = first_element
    try:
        parser.Parse(data, True)
    except _Declaration as found:
        return found.encoding
    except expat.ExpatError:
        return None
    return None


def _depth(root: ET.Element) -> int:
    deepest = 0
    pending = [(root, 1)]
    while pending:
        element, depth = pending.pop()
        deepest = max(deepest, depth)
        pending.extend((child, depth + 1) for child in element)
    return deepest


def _kept(text: str | None) -> str | None:
    return text if text is not None and text.strip() else None


def to_element(element: ET.Element) -> XmlElement:
    """``element`` and everything inside it as an :class:`XmlElement` (depth is bounded by :meth:`load_xml`)."""
    return XmlElement(
        tag=element.tag,
        attributes=dict(element.attrib),
        text=_kept(element.text),
        children=tuple(to_element(child) for child in element),
        tail=_kept(element.tail),
    )


# The lexical forms of xs:boolean, which Apigee's schemas use for flags such as enabled and AlwaysEnforce.
XML_BOOLEANS = {"true": True, "1": True, "false": False, "0": False}


def text_of(element: ET.Element | None) -> str | None:
    """The stripped text of ``element``, or None when it is missing or empty."""
    if element is None or element.text is None:
        return None
    return element.text.strip() or None
