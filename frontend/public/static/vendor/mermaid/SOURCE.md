# Mermaid 11.17.2

官方 npm 包：https://registry.npmjs.org/mermaid/-/mermaid-11.17.2.tgz

完整包 SHA-512：`sha512-V6K3C8EBdEsPFZXSKMJe6ppQOENxuHARr9GvHX4hh47lAbhMRD9qf4oEK7LoaRQxULMa80/qt5gHO73aCleBBg==`。仅提取浏览器 IIFE 与 MIT 许可证；本地托管，不向第三方发送图表。

本项目在独立、不透明 origin iframe 中使用官方包。图表配置不能覆盖安全级别及资源限额；额外清理使用本地 DOMPurify。Iconify 资源仅在用户确认后通过服务器获取路径数据，不加载外部 JavaScript。安全配置参考：https://mermaid.js.org/config/usage 。
