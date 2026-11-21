"""SQLite-compatible scalar helpers needed by existing application queries."""

STATEMENTS = [
    """CREATE FUNCTION json_extract(document text, path text) RETURNS text LANGUAGE sql IMMUTABLE AS $$
      SELECT CASE WHEN path LIKE '$.%' THEN document::jsonb #>> string_to_array(substr(path, 3), '.') END
    $$""",
    """CREATE FUNCTION json_valid(document text) RETURNS bigint LANGUAGE plpgsql IMMUTABLE AS $$
      BEGIN PERFORM document::jsonb; RETURN 1; EXCEPTION WHEN invalid_text_representation THEN RETURN 0; END
    $$""",
    """CREATE FUNCTION json_each(document text) RETURNS TABLE(value text) LANGUAGE sql IMMUTABLE AS $$
      SELECT CASE WHEN jsonb_typeof(item) = 'string' THEN item #>> '{}' ELSE item::text END
      FROM jsonb_array_elements(document::jsonb) AS items(item)
    $$""",
    """CREATE FUNCTION julianday(value text, modifier text DEFAULT NULL) RETURNS double precision
      LANGUAGE plpgsql STABLE SET TIME ZONE 'UTC' AS $$
      DECLARE result timestamptz;
      BEGIN
        result := CASE WHEN value = 'now' THEN CURRENT_TIMESTAMP ELSE value::timestamptz END;
        IF modifier IS NOT NULL THEN result := result + modifier::interval; END IF;
        RETURN extract(epoch FROM result) / 86400.0 + 2440587.5;
      EXCEPTION WHEN invalid_datetime_format OR datetime_field_overflow THEN RETURN NULL; END
    $$""",
    """CREATE FUNCTION instr(value text, search text) RETURNS integer LANGUAGE sql IMMUTABLE AS $$
      SELECT position(search IN value)
    $$""",
    """CREATE INDEX IF NOT EXISTS rag_lexical_records_tokens ON rag_lexical_records
      USING gin (to_tsvector('simple', tokens))""",
    """CREATE FUNCTION datetime(value text, modifier text DEFAULT NULL) RETURNS text
      LANGUAGE plpgsql STABLE SET TIME ZONE 'UTC' AS $$
      DECLARE result timestamptz;
      BEGIN
        result := CASE WHEN value = 'now' THEN CURRENT_TIMESTAMP ELSE value::timestamptz END;
        IF modifier IS NOT NULL THEN result := result + modifier::interval; END IF;
        RETURN to_char(result AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS');
      EXCEPTION WHEN invalid_datetime_format OR datetime_field_overflow THEN RETURN NULL; END
    $$""",
]
