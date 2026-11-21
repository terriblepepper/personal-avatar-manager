# 离线预览资源

PDF.js 的模块、worker、字库、字符映射与 WASM 解码器由文件预览按需加载。版本、来源及许可证见 [pdfjs/SOURCE.md](pdfjs/SOURCE.md) 和 [pdfjs/LICENSE](pdfjs/LICENSE)。

Mermaid 隔离页使用本地 [Mermaid 11.17.2](mermaid/SOURCE.md) 和 DOMPurify 3.4.17 净化导出的 SVG。许可证见 [Mermaid](mermaid/LICENSE) 与 [DOMPurify](dompurify-LICENSE.txt)。

主页面通过 npm 引入 Marked 与 DOMPurify，版本与完整性记录在前端依赖锁中。保留 [Marked 许可证](marked-LICENSE.txt)。所有渲染依赖均随构建产物提供。
