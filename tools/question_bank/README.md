# 题库准备与导入

保留 `prepare.py`（原表解析、清洗、分类、去重、导出）和 `translate.py`（本地离线翻译）；平台运行时不执行爬虫、翻译或访问原始网站。

`bank.json` 为可导入的 1248 道中文题、12 类；重新导出时可指定本地报告路径，记录校验和、排除原因及抽检。旧 datasets 中的中间文件、重复压缩包和原始网页快照已删除，已导入数据库的题目不受影响。

来源为 [IPIP 官方原表](https://ipip.ori.org/AlphabeticalItemList.htm)，[授权声明](https://ipip.ori.org/newPermission.htm)将原题项置于公有领域。中文内容为机器翻译、规则改写及抽检结果，不能用于临床评估或当作经过验证的心理量表。

在管理后台“soul 题库 → 导入题库”选择本目录 `bank.json`。重复导入按编号和指纹去重；导入不调用付费模型。

## 重新准备

先从上述官方页面另存原表为 `/tmp/ipip-items.html`；保留脚本仅读输入文件，不会在生产平台中自动抓取。

```bash
python3 tools/question_bank/prepare.py prepare /tmp/ipip-items.html /tmp/ipip-candidates.json
python3 tools/question_bank/prepare.py export /tmp/ipip-candidates.json /tmp/ipip-translations.json /tmp/ipip-bank.json /tmp/ipip-report.json
```

export 需要先完成译文；可运行保留的 `translate.py`，其参数为候选 JSON、本地翻译模型目录、译文 JSON。该可选离线工具需要 ctranslate2 和 sentencepiece，不是平台或 EverOS 的运行依赖。重新生成后应抽检，再通过管理网页导入。
