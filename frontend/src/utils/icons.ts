const paths: Record<string, string> = {
  public: 'M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18M3 12h18M12 3c-5 5-5 13 0 18 5-5 5-13 0-18',
  private: 'M6 10h12v11H6zM8 10V7a4 4 0 0 1 8 0v3',
  copy: 'M9 8h12v13H9zM5 16H3V3h12v2',
  'collapse-document': 'M9 3H5v18h4m6-18h4v18h-4M9 9h6m-6 6h6',
  home: 'M3 11l9-8 9 8M5 10v11h5v-7h4v7h5V10',
  person: 'M8 7a4 4 0 1 0 8 0 4 4 0 0 0-8 0M4 21v-3a8 8 0 0 1 16 0v3',
  people: 'M4 21v-3a6 6 0 0 1 12 0v3M7 6a3 3 0 1 0 6 0 3 3 0 0 0-6 0M17 3a3 3 0 0 1 0 6m2 4a5 5 0 0 1 3 5v3',
  agent: 'M4 5h16v15H4zM8 2v3m8-3v3M8 10h.01M16 10h.01M8 15h8',
  clock: 'M12 8v5l4 2|M22 12a10 10 0 1 1-20 0 10 10 0 0 1 20 0',
  book: 'M3 4h7l2 2 2-2h7v16h-7l-2 2-2-2H3zM12 6v16',
  mail: 'M3 5h18v14H3zM3 5l9 7 9-7',
  settings: 'M9 3h6l1 3 3 1 2 5-2 5-3 1-1 3H9l-1-3-3-1-2-5 2-5 3-1z|M16 12a4 4 0 1 1-8 0 4 4 0 0 1 8 0',
  forward: 'M5 12h14m-6-6 6 6-6 6',
  up: 'M12 20V4m-6 6 6-6 6 6',
  upload: 'M12 16V3m-5 5 5-5 5 5M4 15v6h16v-6',
  folder: 'M3 8V5h6l3 3h9v13H3z',
  chevron: 'm9 5 7 7-7 7',
  folderUpload: 'M3 8V5h6l3 3h9v13H3zM12 18v-7m-3 3 3-3 3 3',
  newFolder: 'M3 8V5h6l3 3h9v13H3zM12 11v7m-3-3h6',
  sync: 'M20 8a8 8 0 0 0-14-3L3 8m0-5v5h5m-4 8a8 8 0 0 0 14 3l3-3m0 5v-5h-5',
  rebuild: 'M5 4v6h6M5 10a8 8 0 1 1-1 7',
  save: 'M4 3h13l4 4v14H3V3zM7 3v7h10V3M7 21v-7h10v7',
  rename: 'M4 20h4L20 4l-4-3L3 16v4z',
  trash: 'M3 6h18M8 6V3h8v3M5 6l1 15h12l1-15M10 10v7m4-7v7',
  info: 'M12 17v-5m0-5h.01|M22 12a10 10 0 1 1-20 0 10 10 0 0 1 20 0',
  download: 'M12 3v13m-5-5 5 5 5-5M4 18v3h16v-3',
  import: 'M3 3h13v18H3zM11 12h10m-4-4 4 4-4 4',
  close: 'M5 5l14 14M19 5L5 19',
  maximize: 'M5 5h14v14H5z',
  restore: 'M8 5V3h13v13h-2M3 8h13v13H3z',
  panel: 'M3 4h18v16H3zM9 4v16',
  back: 'M19 12H5m6-6-6 6 6 6',
  search: 'M17 17l4 4|M19 11a8 8 0 1 1-16 0 8 8 0 0 1 16 0',
  more: 'M5 12h.01M12 12h.01M19 12h.01',
};
const themePaths: Record<string, string> = {
  system: 'M3 4h18v13H3zM8 21h8m-4-4v4',
  light: 'M12 8a4 4 0 1 0 0 8 4 4 0 0 0 0-8M12 2v2m0 16v2M2 12h2m16 0h2M5 5l1 1m12 12 1 1M5 19l1-1M18 6l1-1',
  dark: 'M20 15A9 9 0 0 1 9 4a9 9 0 1 0 11 11',
};
export function iconSvg(name: string, size = 18): string {
  const segments = (paths[name] || paths.more).split('|');
  return `<svg viewBox="0 0 24 24" width="${size}" height="${size}" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${segments.map(d => `<path d="${d}"/>`).join('')}</svg>`;
}
export function themeIconSvg(name: string): string {
  return `<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="${themePaths[name] || themePaths.system}"/></svg>`;
}
