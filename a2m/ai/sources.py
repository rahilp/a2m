"""Find the code of a custom code policy (JavaScript, Python, JavaCallout) in its bundle.

:func:`callout_kind` says which policies are custom code the AI may translate.
:func:`callout_source` returns the code to send, or the reason nothing can be
sent (the step is then unsupported and the AI is never asked):

* JavaScript: the ``ResourceURL`` script (``jsc://name.js``) or the inline
  ``<Source>``, plus each ``IncludeURL`` script as context;
* Python (``<Script>``): the ``ResourceURL`` script (``py://name.py``);
* JavaCallout: the ``.java`` source of its ``ClassName`` under
  ``resources/java/``. A callout that ships only a compiled ``.jar`` is never
  sent: the AI would have to guess what the class does.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from a2m.ai.provider import ItemKind
from a2m.ir import Policy, Resource, XmlElement

POLICY_KINDS = {"Javascript": ItemKind.JAVASCRIPT, "Script": ItemKind.PYTHON, "JavaCallout": ItemKind.JAVA}
# A script longer than this is not sent (a prompt that size is not a translation request any more).
MAX_SOURCE_CHARS = 200_000


@dataclass(frozen=True, slots=True)
class CalloutSource:
    """The code of one callout: ``original`` verbatim, ``file`` where it came from, ``includes`` (file, text) pairs."""

    kind: ItemKind
    original: str
    file: str
    includes: tuple[tuple[str, str], ...] = ()


def callout_kind(policy: Policy) -> ItemKind | None:
    """The kind of custom code ``policy`` holds, or None when it is not a callout the AI may translate."""
    return POLICY_KINDS.get(policy.type)


def _child_text(settings: XmlElement, tag: str) -> str | None:
    child = next((c for c in settings.children if c.tag == tag), None)
    return (child.text or "").strip() if child is not None else None


def _resource(url: str, resources: Sequence[Resource]) -> Resource | None:
    scheme, sep, name = url.partition("://")
    if not sep:
        return None
    return next((r for r in resources if r.kind == scheme and r.name == name), None)


def _text_of(url: str, resources: Sequence[Resource], what: str) -> tuple[str, str] | str:
    """(file, text) of the resource ``url`` names, or the reason it cannot be sent."""
    found = _resource(url, resources)
    if found is None:
        return f"its {what} {url} is not in the bundle"
    if found.binary or found.text is None:
        return f"its {what} {url} is not a text file"
    if not found.text.strip():
        return f"its {what} {url} is empty"
    if len(found.text) > MAX_SOURCE_CHARS:
        return f"its {what} {url} is longer than {MAX_SOURCE_CHARS} characters"
    return found.file, found.text


def callout_source(policy: Policy, resources: Sequence[Resource]) -> CalloutSource | str:
    """The code of callout ``policy`` (see the module docstring), or the reason it is not sent to the AI."""
    kind = callout_kind(policy)
    if kind is None:
        return f"{policy.type} policy {policy.name} is not custom code"
    settings = policy.settings
    if kind is ItemKind.JAVA:
        return _java_source(policy, resources)
    url = _child_text(settings, "ResourceURL")
    inline = _child_text(settings, "Source") if kind is ItemKind.JAVASCRIPT else None
    if url:
        found = _text_of(url, resources, "script")
        if isinstance(found, str):
            return f"{policy.type} policy {policy.name}: {found}"
        file, original = found
    elif inline:
        file, original = f"policies/{policy.name}.xml <Source>", inline
    else:
        return f"{policy.type} policy {policy.name} names no script (no ResourceURL)"
    includes: list[tuple[str, str]] = []
    for child in settings.children:
        if child.tag == "IncludeURL" and (child.text or "").strip():
            included = _text_of((child.text or "").strip(), resources, "included script")
            if isinstance(included, str):
                return f"{policy.type} policy {policy.name}: {included}"
            includes.append(included)
    return CalloutSource(kind, original, file, tuple(includes))


def _java_source(policy: Policy, resources: Sequence[Resource]) -> CalloutSource | str:
    class_name = _child_text(policy.settings, "ClassName")
    if not class_name:
        return f"JavaCallout {policy.name} names no ClassName, so its Java source cannot be found"
    simple = class_name.rsplit(".", 1)[-1]
    package_path = class_name.replace(".", "/") + ".java"
    sources = [r for r in resources if r.kind == "java" and not r.binary and r.text is not None]
    exact = [r for r in sources if r.name == package_path or r.name == f"{simple}.java"]
    named = exact or [r for r in sources if r.name.rsplit("/", 1)[-1] == f"{simple}.java"]
    if not named:
        jar = _child_text(policy.settings, "ResourceURL") or "no ResourceURL"
        return (
            f"JavaCallout {policy.name}: no Java source for class {class_name} ({simple}.java) is in the bundle, "
            f"only {jar}; a2m sends a Java callout to the AI only with its .java source"
        )
    if len(named) > 1:
        files = ", ".join(sorted(r.file for r in named))
        return f"JavaCallout {policy.name}: several Java sources could hold class {class_name} ({files})"
    found = named[0]
    text = found.text or ""
    if len(text) > MAX_SOURCE_CHARS:
        return f"JavaCallout {policy.name}: its Java source {found.file} is longer than {MAX_SOURCE_CHARS} characters"
    return CalloutSource(ItemKind.JAVA, text, found.file)
