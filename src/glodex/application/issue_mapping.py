"""Fail-safe mapping from domain diagnostics to the stricter public contract."""

from __future__ import annotations

import re

from glodex.contracts import Detail, Issue, IssueSeverity
from glodex.domain.issues import CatalogIssue, IssueDisposition

_SAFE_ENTITY_REF = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@\[\]-]{0,511}\Z")
_SAFE_DETAIL_KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}\Z")


def catalog_issue_to_public(issue: CatalogIssue) -> Issue:
    """Map only bounded, schema-safe issue data and never expose raw messages."""

    if type(issue) is not CatalogIssue:
        raise TypeError("issue must be a CatalogIssue")
    entity_ref = issue.entity_ref
    if entity_ref is not None and _SAFE_ENTITY_REF.fullmatch(entity_ref) is None:
        entity_ref = None

    details: list[Detail] = []
    for index, item in enumerate(issue.details):
        key = item.key if _SAFE_DETAIL_KEY.fullmatch(item.key) is not None else f"detail-{index}"
        value = item.value
        if type(value) is str and (
            len(value) > 512 or "\n" in value or "\r" in value or value.startswith(("/", "\\"))
        ):
            value = "[omitted]"
        details.append(Detail(key=key, value=value))

    return Issue(
        code=issue.code.value,
        stage=issue.stage.value,
        message=f"Catalog validation reported {issue.code.value}.",
        severity=(
            IssueSeverity.ERROR
            if issue.disposition is IssueDisposition.FATAL
            else IssueSeverity.WARNING
        ),
        entity_ref=entity_ref,
        details=tuple(details),
    )


__all__ = ["catalog_issue_to_public"]
