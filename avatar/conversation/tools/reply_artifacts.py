"""Keep executable source in attachments or the execution log, outside final prose."""
import re

FENCE = re.compile(
    r'(?ms)^ {0,3}(?P<marker>(?P<char>`|~)(?P=char){2,})(?P<info>[^\r\n]*)\r?\n'
    r'(?P<code>.*?)(?P<close>^ {0,3}(?P=marker)(?P=char)*[ \t]*(?:\r?\n|$)|\Z)'
)
PAGE_START = re.compile(r'(?i)<!doctype\s+html\b|<html(?=[\s>])')
HTML_LANGUAGES = {'html', 'htm'}
PYTHON_LANGUAGES = {'python', 'python3', 'py', 'py3'}
HTML_SOURCE_LANGUAGES = HTML_LANGUAGES | PYTHON_LANGUAGES | {'css', 'javascript', 'js', 'typescript', 'ts'}
RAW_PAGE = re.compile(r'(?ims)^ {0,3}(?=<!doctype\s+html\b|<html(?=[\s>])).*?(?:</html\s*>|\Z)')


def is_html_output(item):
    return not item.get('public_material_id') and item['filename'].lower().endswith(('.html', '.htm'))


def reply_with_artifacts(text, outputs, events, *, saved=()):
    html_files = [item for item in [*saved, *outputs] if is_html_output(item)]
    sandbox = [event for event in events if event.get('group') == 'sandbox']
    scripts = [code.strip() for event in sandbox if isinstance(event.get('input'), dict)
               and isinstance(code := event['input'].get('code'), str) and code.strip()]
    removed = False

    def drop_code(match):
        nonlocal removed
        info = match['info'].strip().lower().split()
        language = info[0] if info else ''
        code = match['code'].strip()
        if language == 'avanim':
            removed = True
            return '\n'
        if language == 'mermaid':
            return match[0]
        if language in HTML_LANGUAGES or PAGE_START.search(code):
            removed = True
            return '\n'
        elif not (html_files and language in HTML_SOURCE_LANGUAGES
                  or sandbox and (language in PYTHON_LANGUAGES
                                  or code and any(code in script or script in code for script in scripts))):
            return match[0]
        removed = True
        return '\n'

    def drop_raw_page(match):
        nonlocal removed
        removed = True
        return '\n'

    if '<' in text or sandbox or html_files or re.search(r'(?im)^ {0,3}(?:`{3,}|~{3,})\s*(?:html?|avanim)\b', text):
        text = FENCE.sub(drop_code, text)
        text = RAW_PAGE.sub(drop_raw_page, text)
        for script in scripts:
            if '\n' in script and script in text:
                text = text.replace(script, '')
                removed = True
            elif '\n' not in script:
                escaped = re.escape(script)
                text, count = re.subn(r'(?m)^ {0,3}' + escaped + r'[ \t]*$|`+' + escaped + r'`+', '', text)
                removed |= bool(count)

    markers = []
    for file in html_files:
        marker = '[[attachment:' + file['id'] + ']]'
        if not re.search(r'(?m)^ {0,3}' + re.escape(marker) + r'[ \t]*\r?$', text):
            markers.append(marker)
    if markers:
        return '\n\n'.join([*markers, text.strip()]).strip()
    if removed:
        return text.strip() or '本轮没有可展示的正文，请查看执行过程。'
    return text
