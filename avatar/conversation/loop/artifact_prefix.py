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


def _members(source, start):
    if source[start:start+1] != '{':
        return
    pos = start+1
    while pos < len(source):
        name,end = _DECODER.raw_decode(source,_SPACE.match(source,pos).end())
        colon = _SPACE.match(source,end).end()
        if not isinstance(name,str) or source[colon:colon+1] != ':':
            return
        value = _SPACE.match(source,colon+1).end()
        yield name,value
        _,end = _DECODER.raw_decode(source,value)
        comma = _SPACE.match(source,end).end()
        if source[comma:comma+1] != ',':
            return
        pos = comma+1


def edit_signature(edit):
    import hashlib
    result = {}
    for key,value in edit.items():
        if key in ('field','old','new') and isinstance(value,str):
            digest = hashlib.sha256()
            for pos in range(0,len(value),65536):
                digest.update(value[pos:pos+65536].encode())
            result[key] = digest.hexdigest()
        elif key=='offset' and type(value) is int:
            result[key] = value
    return result


def revision_evidence(source):
    """Bind a replacement to the visible original and every completed edit before the damaged byte."""
    from avatar.conversation.tools.html_revision import SourceEdit

    proof = {'edits':[]}
    try:
        for name,start in _members(source,_SPACE.match(source).end()):
            if name != 'revision':
                continue
            for name,start in _members(source,start):
                if name in ('file_id','sha256'):
                    value,_ = _DECODER.raw_decode(source,start)
                    if isinstance(value,str) and len(value)<=80:
                        proof[name] = value
                if name != 'edits' or source[start:start+1]!='[':
                    continue
                pos=_SPACE.match(source,start+1).end()
                while source[pos:pos+1]=='{' and len(proof['edits']) < 32:
                    try:
                        item,end=_DECODER.raw_decode(source,pos)
                    except json.JSONDecodeError:
                        partial={}
                        proof['partial']=partial
                        for key,field_start in _members(source,pos):
                            value,_=_DECODER.raw_decode(source,field_start)
                            partial.update(edit_signature({key:value}))
                        return proof
                    edit=SourceEdit.model_validate(item)
                    proof['edits'].append(edit_signature(edit.model_dump()))
                    comma=_SPACE.match(source,end).end()
                    if source[comma:comma+1]!=',':
                        break
                    pos=_SPACE.match(source,comma+1).end()
            break
    except (TypeError,ValueError,RecursionError):
        pass
    return proof
