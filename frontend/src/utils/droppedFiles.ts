export interface UploadFile { file: File; path: string }

async function readEntry(entry: FileSystemEntry, path: string, files: UploadFile[], directories: string[]) {
  if (entry.isFile) {
    const file = await new Promise<File>((resolve, reject) => (entry as FileSystemFileEntry).file(resolve, reject))
    files.push({ file, path: path + file.name })
  } else if (entry.isDirectory) {
    const reader = (entry as FileSystemDirectoryEntry).createReader()
    const next = path + entry.name + '/'
    directories.push(next.slice(0, -1))
    while (true) {
      const batch = await new Promise<FileSystemEntry[]>((resolve, reject) => reader.readEntries(resolve, reject))
      if (!batch.length) break
      for (const child of batch) await readEntry(child, next, files, directories)
    }
  }
}

export async function droppedFiles(transfer: DataTransfer) {
  const list: UploadFile[] = [], dirs: string[] = []
  // Capture entry handles and files before the browser releases the drop event.
  const entries = Array.from(transfer.items).filter(item => item.kind === 'file')
    .map(item => ({ entry: item.webkitGetAsEntry?.(), file: item.getAsFile() }))
  const fallback = Array.from(transfer.files)
  for (const { entry, file } of entries) {
    if (entry) await readEntry(entry, '', list, dirs)
    else if (file) list.push({ file, path: file.webkitRelativePath || file.name })
  }
  if (!list.length && !dirs.length) {
    for (const file of fallback) list.push({ file, path: file.webkitRelativePath || file.name })
  }
  return { files: list, directories: dirs }
}
