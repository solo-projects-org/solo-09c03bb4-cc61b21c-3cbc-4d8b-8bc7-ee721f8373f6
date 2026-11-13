# Newspaper Phrase Locator

报刊档案引文核对服务：把编目的文章块顺序和 OCR 词/字框建成 SQLite 索引，按连续英文词组定位到原图上的每一处位置。一句话即使跨块、跨版面，也能返回它在各版上的字框；命中不会跨文章。

## 启动

```bash
docker compose up --build
```

打开 **http://localhost:8000/docs**（Swagger UI）。服务启动时若数据库为空，会自动装入 `/app/samples/demo.json` 本地样例。

- SQLite 文件：`/data/archive.sqlite3`，保存在 Compose volume `archive-data`
- 健康检查：`GET /health`
- 重建演示数据：`docker compose down -v && docker compose up --build`

也可本地运行：

```bash
python -m venv .venv
. .venv/bin/activate
pip install -r requirements-dev.txt
uvicorn app.main:app --reload
pytest
```

## Swagger 演示

### 1. 跨版检索

在 Swagger 执行：

```text
GET /api/search/phrase?q=the lighthouse keeper climbed
```

返回 A-100 的一处命中。`words` 中 `keeper` 在 `page-001` 的 block 2，`climbed` 在 `page-002` 的 block 1；每个词都带 `page_id`、`block_no`、`bbox`。样例版面图可从 `image_url` 打开（例如 <http://localhost:8000/scans/page-001.svg>）。再试：

```text
GET /api/search/phrase?q=BULLETIN the lighthouse keeper climbed
```

这个短语同时跨 `page-001` 的两个块和版面边界。把 `article_id=A-100` 传入可只看某篇文章。

文章边界测试：

```text
GET /api/search/phrase?q=dawn%20only
```

A-100 以 `dawn` 结束，A-200 以 `only` 开始，虽然二者同在 `page-002` 的 OCR 中，但不会命中。

### 2. 单版更正

在 Swagger 的 `PUT /api/pages/page-002/ocr` 中，请求体已预填
`samples/page-002-corrected.json` 的内容（也可直接复制该文件），执行：

- `page-002` OCR 被替换
- 只重建引用该版面的 A-100、A-200 索引
- 响应中的 `affected_articles` 为 `["A-100", "A-200"]`

然后：

| 查询 | 更正前 | 更正后 |
| --- | --- | --- |
| `the lighthouse keeper climbed` | 1 处，跨页 | 0 |
| `the lighthouse keeper walked` | 0 | 1 处，跨页 |
| `only in the archive edition` | 1 处 | 0 |
| `only in the archive supplement` | 0 | 1 处 |

### 3. 缺块时整版拒绝

把 `samples/page-002-missing-block.json` 作为
`PUT /api/pages/page-002/ocr` 的请求体。它只有 block 1，缺少编目引用的 block 2。响应为 **409**：

```json
{
  "detail": {
    "code": "missing_catalog_blocks",
    "message": "OCR retransmission rejected: ...",
    "page_id": "page-002",
    "missing_blocks": [2],
    "affected_articles": ["A-100", "A-200"],
    "unchanged": true
  }
}
```

整个版面的 OCR 替换和索引重建都不会提交；更正前的查询结果照常可查。

## API

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| `PUT` | `/api/pages/{page_id}/ocr` | 替换单版 OCR，原子重建涉及该版的文章索引 |
| `GET` | `/api/pages/{page_id}` | 查看版面 revision、块与词数 |
| `PUT` | `/api/catalog/articles/{article_id}` | 新建/替换文章的块引用顺序 |
| `GET` | `/api/articles/{article_id}` | 查看文章块引用、索引状态与词数 |
| `GET` | `/api/search/phrase?q=...` | 忽略大小写、连续词组的跨块/跨版检索 |
| `GET` | `/api/search?q=...` | 同上，未写入 Swagger 的兼容别名 |
| `GET` | `/health` | 健康检查与行数统计 |

### OCR 输入

```json
{
  "image_url": "/scans/page-002.svg",
  "width": 1600,
  "height": 2200,
  "blocks": [
    {
      "block_no": 1,
      "words": [
        {"text": "walked", "bbox": [100, 140, 220, 175]},
        {"text": "the", "bbox": [230, 140, 295, 175]}
      ]
    }
  ]
}
```

也接受不嵌套块的 OCR 导出：

```json
{
  "words": [
    {"block_no": 1, "text": "walked", "bbox": [100, 140, 220, 175]},
    {"block_no": 1, "text": "the", "bbox": [230, 140, 295, 175]}
  ]
}
```

`bbox` 输入支持：

- `[x0, y0, x1, y1]`
- `{"x0": ..., "y0": ..., "x1": ..., "y1": ...}`
- `{"x": ..., "y": ..., "width": ..., "height": ...}`
- `{"left": ..., "top": ..., "right": ..., "bottom": ...}`

输出统一为像素坐标对象 `{"x0": ..., "y0": ..., "x1": ..., "y1": ...}`。

### 编目输入

`blocks` 的顺序就是文章正文的连续顺序；`page_id + block_no` 指向 OCR 块，不要求物理块号连续：

```json
{
  "title": "The Lighthouse Keeper",
  "blocks": [
    {"page_id": "page-001", "block_no": 1},
    {"page_id": "page-001", "block_no": 2},
    {"page_id": "page-002", "block_no": 1}
  ]
}
```

- OCR 已到位的文章立即建索引。
- 新文章可以先编目、后到 OCR；此时 `status` 为 `pending_ocr`，`indexed_words` 为 0，不会以残缺块序列参与检索。
- 替换已有文章时若引用的 OCR 块缺失，返回 409，旧编目和旧索引保持不变。

### 查询语义

1. 以空白切分查询串和 OCR 词，做 Unicode NFKC + `casefold`，仅忽略大小写。
2. 在**同一文章**内按编目顺序把块展平，块内按 OCR 词序排列。
3. 只匹配连续词元；可以跨块、跨版面，但文章边界不可跨越。
4. 每处命中单独返回，包含文章编号、标题和每个词的位置。

## 一致性与事务设计

- SQLite 开启 WAL 和外键。
- 单版 OCR 重传使用一个 `BEGIN IMMEDIATE` 事务：
  1. 读取该版被哪些文章引用；
  2. 若请求缺少其中任何块，立即回滚整版；
  3. 替换该版 OCR 数据；
  4. 按编目顺序重建且仅重建这些文章的整篇索引（跨版位置一并更新）；
  5. 一次性提交。
- 查询使用单个 deferred read transaction，候选命中、词框和文章标题来自同一个快照。并发时只能看到提交前完整结果或提交后完整结果。
- 未引用该版面的文章索引行不会删除或重写。

## 目录

```text
app/
  main.py          FastAPI 路由、启动装载样例
  repository.py    SQLite schema、事务、重建与检索
  schemas.py       请求/响应模型与 Swagger 示例
samples/
  demo.json                    本地演示语料
  page-002-corrected.json      单版更正请求体
  page-002-missing-block.json  缺块拒绝请求体
static/scans/
  page-001.svg, page-002.svg   由样例字框生成的演示版面图
tests/
  test_api.py      检索、更正、回滚、输入形式
  test_atomic.py   写事务暂停时的读快照验证
```
