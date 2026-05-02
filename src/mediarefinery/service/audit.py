"""Audit log writer.

Every Immich-mutating action and every authentication event becomes an
audit_log row. Required by the service-mode invariants documented in ADR-0010.
PR 1 stub only; concrete writer lands alongside state v2 in PR 2.
"""
