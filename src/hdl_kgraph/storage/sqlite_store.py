"""SQLite persistence (M1).

Single-file, local-first storage. Planned tables:

* ``nodes`` — id, kind, name, qualified_name, file, line_span, language, attrs (JSON)
* ``edges`` — src, dst, kind, confidence, attrs (JSON)
* ``files`` — path, language, content_hash (drives incremental rebuilds in M4),
  parse_error_count
* ``meta`` — schema_version (migration guard lands in M4)
"""
