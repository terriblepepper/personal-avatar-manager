# 模块说明：识别文本、ZIP 和 TAR 技能包，递归发现技能；文件只作为数据解析，不运行安装脚本。
import io
import re
import stat
import tarfile
import zipfile
import zlib
from pathlib import PurePosixPath
import yaml
from fastapi import HTTPException

MAX_UPLOAD = 50 * 1024 * 1024
MAX_EXPANDED = 100 * 1024 * 1024
MAX_FILES = 4096
MAX_SKILL = 256 * 1024
MAX_SKILLS = 100
MAX_STORAGE = 200 * 1024 * 1024


# 职责：读取可选 YAML 元数据及完整 Markdown 正文，兼容无 frontmatter 的普通说明。
# 输入：content 为 UTF-8 文件，fallback 为来源目录或文件名，用于补齐未填写的名称。
# 结果与约束：保留长正文；只安全解析 YAML，非法元数据明确报错，不执行扩展字段中的命令。
def parse_skill(content, fallback='skill'):
    if len(content) > MAX_SKILL:
        raise HTTPException(422, '单个技能说明最多 256 KiB')
    try:
        text = content.decode('utf-8-sig').strip()
        lines = text.splitlines()
        metadata = {}
        body = text
        if lines and lines[0].strip() == '---':
            end = next(i for i in range(1, len(lines)) if lines[i].strip() == '---')
            metadata = yaml.safe_load('\n'.join(lines[1:end])) or {}
            if not isinstance(metadata, dict):
                raise ValueError()
            body = '\n'.join(lines[end+1:]).strip()
        title = next((line.lstrip('# ').strip() for line in body.splitlines() if line.startswith('# ')), body.splitlines()[0] if body else fallback)
        name = metadata.get('name') or fallback
        description = metadata.get('description') or title[:1200]
        if (not isinstance(name, str) or not name.strip() or len(name) > 100
                or re.search(r'[/\\\x00-\x1f]', name) or not isinstance(description, str)
                or not description.strip() or len(description) > 4096 or not body or '\x00' in body):
            raise ValueError()
    except (UnicodeError, ValueError, StopIteration, AttributeError, RecursionError, yaml.YAMLError):
        raise HTTPException(422, '技能说明须为非空 UTF-8 文本；可选 YAML name/description 必须为字符串，frontmatter 须闭合') from None
    return name.strip(), description.strip(), body


# 职责：检查归档条目的路径和解压预算，再读取有限文件内容。
# 输入：entries 为归档适配后的路径、大小、读取函数序列。
# 结果与约束：拒绝越界、链接、重复、大小欺骗和文件目录冲突，完全不使用 extract/extractall。
def read_entries(entries):
    files, seen, total = {}, set(), 0
    for index, (filename, size, reader) in enumerate(entries):
        path = PurePosixPath(filename)
        if not path.parts and reader is None:
            continue
        if (index >= MAX_FILES*2 or not path.parts or path.is_absolute() or '..' in path.parts
                or '\\' in filename or ':' in filename or '\x00' in filename
                or len(filename) > 500 or len(path.parts) > 30):
            raise ValueError('归档路径无效或条目过多')
        canonical = str(path)
        if canonical.casefold() in seen:
            raise ValueError('归档含重复路径')
        seen.add(canonical.casefold())
        if reader is None:
            continue
        total += size
        if len(files) >= MAX_FILES or size < 0 or total > MAX_EXPANDED:
            raise ValueError('解压文件数量或容量超限')
        body = reader()
        if len(body) != size:
            raise ValueError('归档文件大小不一致')
        files[canonical] = body
    names = {name.casefold() for name in files}
    if any(str(parent).casefold() in names for name in files for parent in PurePosixPath(name).parents if parent.parts):
        raise ValueError('归档含文件与目录冲突')
    return files


# 职责：把 ZIP 成员转换为统一的受限读取条目。
# 输入：archive 为已经打开的 ZIP。
# 结果与约束：拒绝加密、符号链接及特殊文件；声明大小先校验，读取仍受全包预算约束。
def zip_entries(archive):
    for item in archive.infolist():
        mode = item.external_attr >> 16
        if item.flag_bits & 1 or (stat.S_IFMT(mode) and not (stat.S_ISREG(mode) or stat.S_ISDIR(mode))):
            raise ValueError('不支持加密或链接文件')
        yield item.filename, item.file_size, None if item.is_dir() else lambda item=item: archive.read(item)


# 职责：把 TAR 成员转换为统一的受限读取条目。
# 输入：archive 支持普通 TAR、gzip、bzip2、xz 压缩流。
# 结果与约束：拒绝硬链接、符号链接、设备及稀疏文件；不写入归档给出的路径。
def tar_entries(archive):
    for item in archive:
        if not (item.isfile() or item.isdir()) or item.issparse():
            raise ValueError('不支持链接或特殊文件')
        yield item.name, item.size, None if item.isdir() else lambda item=item: archive.extractfile(item).read(item.size+1)


# 职责：按文件内容识别归档，并从任意目录层级发现一个或多个技能。
# 输入：filename 是上传名称，content 最多 50 MiB；可上传完整仓库，无需手动拆分目录。
# 结果与约束：以每个 SKILL.md 的最近目录划分资源；嵌套技能各自隔离，重复技能名整包拒绝，未识别格式给出明确提示。
def unpack_skills(filename, content):
    if not content or len(content) > MAX_UPLOAD:
        raise HTTPException(413, '技能包不能为空且不能超过 50 MiB')
    name = str(filename or 'skill.md')
    try:
        if zipfile.is_zipfile(io.BytesIO(content)):
            with zipfile.ZipFile(io.BytesIO(content)) as archive:
                files = read_entries(zip_entries(archive))
        elif name.lower().endswith(('.md', '.markdown', '.txt')):
            files = {'SKILL.md': content}
        else:
            with tarfile.open(fileobj=io.BytesIO(content), mode='r:*') as archive:
                files = read_entries(tar_entries(archive))
    except (zipfile.BadZipFile, tarfile.TarError, zlib.error, RuntimeError, ValueError, OSError, EOFError, NotImplementedError) as exc:
        raise HTTPException(422, '无法解析技能包：支持 Markdown/TXT、ZIP、TAR、TAR.GZ/TGZ、TAR.BZ2、TAR.XZ；拒绝越界路径、链接、重复条目及超过 100 MiB/4096 文件的归档') from None
    roots = {PurePosixPath(path).parent: path for path in files if PurePosixPath(path).name.casefold() == 'skill.md'}
    if not roots:
        candidates = [path for path in files if PurePosixPath(path).suffix.lower() in ('.md', '.markdown', '.txt')]
        if len(candidates) == 1:
            source = candidates[0]
            roots = {PurePosixPath(source).parent: source}
        else:
            raise HTTPException(422, '未找到 SKILL.md；多份普通说明无法确定技能入口，请在每个技能目录提供 SKILL.md')
    if len(roots) > MAX_SKILLS:
        raise HTTPException(422, '一次最多发现 100 个技能')
    packages, names = [], set()
    for root, source in sorted(roots.items(), key=lambda item: str(item[0])):
        fallback = root.name or PurePosixPath(name).stem or 'skill'
        try:
            skill_name, description, instructions = parse_skill(files[source], fallback)
        except HTTPException as exc:
            raise HTTPException(exc.status_code, str(source)+': '+exc.detail) from None
        if skill_name.casefold() in names:
            raise HTTPException(422, '包内技能名称重复：'+skill_name)
        names.add(skill_name.casefold())
        resources = {}
        for path, body in files.items():
            entry = PurePosixPath(path)
            if entry.is_relative_to(root) and not any(other != root and other.is_relative_to(root) and entry.is_relative_to(other) for other in roots):
                relative = str(entry.relative_to(root))
                resources['SKILL.md' if path == source else relative] = body
        packages.append((skill_name, description, instructions, resources))
    return packages
