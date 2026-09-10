# 清账 · 财务对账系统前端 — 交付说明

## 完成内容
在 `frontend/` 下从零搭建了 **Vue 3 + TypeScript + Vite** 单页应用，Apple 官网风格（`#fbfbfd` 浅底、毛玻璃导航、大号紧凑字距标题、胶囊按钮、极简留白）。

## 页面结构（单页）
1. **毛玻璃导航栏** — 品牌标识 + 锚点导航 + 胶囊 CTA
2. **Hero 区** — 「两份账单。一次对清。」大标题 + 三个特性点（本地解析 / 秒级比对 / 一键报表）
3. **流程区** — 三步说明（上传账单 → 智能比对 → 下载报表）
4. **工作台** — 双上传卡片（拖拽/点选，校验 .xlsx/.xls/.csv ≤20MB）→ 开始对账 → 四阶段进度流（上传→解析→比对→生成）→ 结果面板（统计数字 + 下载按钮）
5. **页脚**

## 核心逻辑
- `src/composables/useReconcile.ts`：状态机 `idle → processing → success / error`
- 优先调用后端 `POST /api/reconcile`（FormData: `file_a` / `file_b`，Vite 已代理到 `http://localhost:8000`），返回 Excel Blob 直接下载
- **后端不可用时自动回退本地演示模式**：用 SheetJS（xlsx，已做动态导入分包）在浏览器端以每行第一列为键比对，生成含「完全一致 / 内容不一致 / 仅A存在 / 仅B存在」四个工作表的结果 Excel，并在结果页标注「演示模式」
- 完整状态覆盖：空态、校验错误、拖拽高亮、处理中、成功、失败重试；支持 `prefers-reduced-motion`

## 验证
- `npm run build`（vue-tsc + vite build）通过，零类型错误
- 主包 89.5 KB（gzip 35 KB），xlsx 独立分包按需加载

## 运行方式
```bash
cd frontend && npm install && npm run dev   # http://localhost:5173
```

## 后续建议
- 后端就绪后确认接口路径与字段名（`file_a` / `file_b`）是否一致
- 如需用户登录或历史记录，可在导航栏扩展入口
