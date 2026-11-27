"""Enforce upload policy on each file part before Starlette writes to its spool."""
from starlette.formparsers import MultiPartException, MultiPartParser


class FileTooLarge(MultiPartException):
    pass


class UploadParser(MultiPartParser):
    def __init__(self, headers, stream, *, max_fields, file_limit):
        super().__init__(headers, stream, max_files=1, max_fields=max_fields, max_part_size=65536)
        self.file_limit = file_limit
        self.part_limit = None
        self.file_bytes = 0
        self.header_bytes = 0

    def count_header(self, count):
        self.header_bytes += count
        if self.header_bytes > 65536:
            raise MultiPartException('上传表单头部过大')

    def on_header_field(self, data, start, end):
        self.count_header(end - start)
        super().on_header_field(data, start, end)

    def on_header_value(self, data, start, end):
        self.count_header(end - start)
        super().on_header_value(data, start, end)

    def on_headers_finished(self):
        super().on_headers_finished()
        file = self._current_part.file
        self.part_limit = None if file is None else self.file_limit(file.filename or '')
        self.file_bytes = 0

    def on_part_data(self, data, start, end):
        if self.part_limit is not None:
            self.file_bytes += end - start
            if self.file_bytes > self.part_limit:
                raise FileTooLarge('上传文件超过大小限制')
        super().on_part_data(data, start, end)
