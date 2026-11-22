"""Recover confirmed fields from an unfinished tool argument object without executing it."""
import json
import re

_SPACE = re.compile(r'\s*')
_DECODER = json.JSONDecoder(strict=False)


def _partial_string(source, start, error):
    # Only complete Unicode scalars survive a cut inside a JSON escape or a surrogate pair.
    end = len(source) if error.msg.startswith('Unterminated string') else error.pos
    for stop in range(end, max(start, end-13), -1):
        try:
            value = _DECODER.decode(source[start:stop] + '"')
            if value and 0xd800 <= ord(value[-1]) <= 0xdbff:
                value = value[:-1]
            value.encode('utf-8')
            return value
        except (ValueError,RecursionError):
            continue
    return ''


def salvage_object(source, allowed):
    """Completed top-level values are authoritative. Conflicting duplicates must be supplied anew."""
    cursor = _SPACE.match(source).end()
    values, conflicts = {}, {}
    partial = None
    if source[cursor:cursor+1] == '{':
        cursor += 1
        while cursor < len(source):
            try:
                key, end = _DECODER.raw_decode(source,_SPACE.match(source,cursor).end())
                if not isinstance(key,str):
                    break
                colon = _SPACE.match(source,end).end()
                if source[colon:colon+1] != ':':
                    break
                start = _SPACE.match(source,colon+1).end()
                try:
                    value,end = _DECODER.raw_decode(source,start)
                    if isinstance(value,str):
                        value.encode('utf-8')
                except json.JSONDecodeError as error:
                    if key in allowed and key not in values and key not in conflicts and source[start:start+1]=='"':
                        prefix = _partial_string(source,start,error)
                        if prefix:
                            partial = (key,prefix)
                    break
                cursor = _SPACE.match(source,end).end()
                delimiter = source[cursor:cursor+1]
                if delimiter not in (',','}'):
                    # A parsed numeric prefix ("1e-") or a bare quote inside JS is not a closed field.
                    if isinstance(value,str) and key in allowed and key not in values and key not in conflicts:
                        partial = (key,value) if delimiter else None
                        if not delimiter:
                            values[key] = value
                    break
                if key in allowed and key not in conflicts:
                    if key in values and (type(value) is not type(values[key]) or value != values[key]):
                        conflicts[key] = [v for v in (values[key],value)
                                          if type(v) in (int,float) or isinstance(v,str) and len(v)<=64]
                        del values[key]
                    else:
                        values[key] = value
                if delimiter != ',':
                    break
                cursor += 1
            except (ValueError,RecursionError):
                break

    dump = lambda v: json.dumps(v, ensure_ascii=False, separators=(',',':'), allow_nan=False)
    # Scalar schema fields only; malformed nested data must not consume the reconstruction budget.
    values = {k:v for k,v in values.items() if isinstance(v,(str,int,float)) and not isinstance(v,bool)
              and not (isinstance(v,float) and (v != v or abs(v) == float('inf')))}
    prefix = dump(values)[:-1]
    if partial:
        prefix += (',' if values else '') + dump(partial[0]) + ':' + dump(partial[1])[:-1]
    return prefix, {'fields':list(values), 'conflicts':conflicts, 'partial':partial[0] if partial else None}
