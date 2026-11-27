import { shallowRef } from 'vue'
import { normalize } from '../utils/previewMatches'

export interface PDFPage {
  getViewport(options: { scale: number }): { width: number; height: number; convertToViewportPoint(x: number, y: number): number[] }
  render(options: { canvasContext: CanvasRenderingContext2D; viewport: { width: number; height: number } }): { promise: Promise<void>; cancel(): void }
  getTextContent(): Promise<{ items: unknown[] }>
}
interface PDFDocument { numPages: number; getPage(page: number): Promise<PDFPage>; destroy(): Promise<void> }
interface PDFLibrary { GlobalWorkerOptions: { workerSrc: string }; getDocument(options: Record<string, unknown>): { promise: Promise<PDFDocument>; destroy(): Promise<void> } }
function isLibrary(value: unknown): value is PDFLibrary { return !!value && typeof value === 'object' && 'getDocument' in value && typeof value.getDocument === 'function' && 'GlobalWorkerOptions' in value }

export function usePdfDocument() {
  const pdf = shallowRef<PDFDocument>()
  type PageText = { page: PDFPage; items: unknown[]; text: string }
  const cache = new WeakMap<PDFDocument, Map<number, Promise<PageText>>>()

  async function loadPDF(bytes: Uint8Array, signal: AbortSignal) {
    const moduleURL = '/static/vendor/pdfjs/legacy/build/pdf.mjs', value: unknown = await import(/* @vite-ignore */ moduleURL)
    if (signal.aborted) return
    if (!isLibrary(value)) throw Error('PDF 组件加载失败')
    value.GlobalWorkerOptions.workerSrc = '/static/vendor/pdfjs/legacy/build/pdf.worker.mjs'
    const task = value.getDocument({ data: bytes, cMapUrl: '/static/vendor/pdfjs/cmaps/', cMapPacked: true, standardFontDataUrl: '/static/vendor/pdfjs/standard_fonts/', wasmUrl: '/static/vendor/pdfjs/wasm/', isEvalSupported: false, enableXfa: false, maxImageSize: 16000000, isOffscreenCanvasSupported: false })
    signal.addEventListener('abort', () => { void task.destroy().catch(() => {}) }, { once: true })
    const document = await task.promise
    if (signal.aborted) { await document.destroy(); return }
    pdf.value = document
    return document
  }

  function closePDF() {
    if (pdf.value) {
      cache.delete(pdf.value)
      void pdf.value.destroy().catch(() => {})
      pdf.value = undefined
    }
  }

  function readPage(document: PDFDocument, number: number) {
    let pages = cache.get(document)
    if (!pages) {
      pages = new Map()
      cache.set(document, pages)
    }
    const cached = pages.get(number)
    if (cached) return cached
    const result = document.getPage(number).then(async page => {
      const { items } = await page.getTextContent()
      const text = items.map(item => item && typeof item === 'object' && 'str' in item
        && typeof item.str === 'string' ? item.str : '').join('')
      return { page, items, text: normalize(text) }
    }).catch(error => {
      pages.delete(number)
      throw error
    })
    pages.set(number, result)
    return result
  }
  return { pdf, loadPDF, closePDF, readPage }
}
