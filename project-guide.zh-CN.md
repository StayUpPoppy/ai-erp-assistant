# AI ERP Assistant 项目导读

这份文档给第一次接触这个仓库的人看，重点不是讲每一行实现细节，而是先帮你建立一个整体地图：

- 这个项目是做什么的
- 前端和后端分别放在哪里
- 一次聊天、一次文件上传、一次生成 ERP 草稿，代码是怎么流转的
- 作为新手，应该先看哪些文件

## 1. 这个项目是做什么的

一句话理解：

这是一个“聊天式 ERP 助手”项目。用户可以像聊天一样提问，也可以上传 PDF/订单文件；系统会尝试识别文件内容、抽取字段、补全 ERP 需要的数据，然后生成 ERP 草稿单。

从仓库根目录看，它是一个 monorepo，主要分成两部分：

- `frontend`：前端，基于 Next.js
- `backend`：后端，基于 FastAPI + 异步 worker

项目主要支持三类能力：

1. 普通助手对话
2. ERP 主数据查询，比如查供应商、物料、客户、仓库、税码、销售订单
3. PDF/订单文件转 ERP 草稿的流程

## 2. 目录总览

仓库里最值得先认识的是这些目录：

- `frontend/`
  - 用户直接看到的页面
  - 负责聊天 UI、上传文件、轮询任务进度、展示补全表单和订单预览
- `backend/api/`
  - FastAPI 服务
  - 对外暴露 HTTP API
  - 负责会话、路由、上传、状态查询、字段补全、ERP 草稿创建
- `backend/worker/`
  - 异步处理进程
  - 从 Redis 队列取任务，再回调 API 推进文件处理流程
- `backend/packages/shared/`
  - 后端共享契约包
  - 放一些共享的数据结构和契约定义
- `backend/config/`
  - 配置目录
  - 放解析档案、报表配置等
- `backend/infra/`
  - 本地基础设施
  - 主要是 Redis、Postgres、MinIO 的 docker compose
- `deploy/`
  - 部署相关配置

## 3. 前端在做什么

### 3.1 前端技术栈

前端在 `frontend/`，使用的是：

- Next.js 14
- React 18
- TypeScript
- Tailwind CSS

它不是传统那种多页面 ERP，而更像一个“单页面工作台”。

### 3.2 前端主入口

最重要的页面文件是：

- `frontend/app/page.tsx`

这个文件非常大，但其实它集中做的是一件事：把“聊天 + 文件上传 + 任务进度 + 补全表单 + 订单预览 + 草稿创建”放到同一个页面里。

你可以把它理解成一个前端控制台，里面主要有这些职责：

1. 聊天消息管理
2. 会话列表管理
3. 文件拖拽上传
4. 轮询后端任务状态
5. 根据任务状态展示不同卡片
6. 在合适的时候调用“补全字段”“确认预览”“生成草稿”等 API

### 3.3 前端如何调用后端

调用逻辑主要封装在：

- `frontend/lib/api.ts`

这个文件是前端请求层，作用很清晰：

- 统一 API base URL
- 给每个请求附带 `x-request-id`
- 统一处理 JSON 解析和报错
- 封装不同业务接口

你会在这里看到这些常用函数：

- `getHealth()`
- `getIngestion()`
- `postAssistantMessage()`
- `streamAssistantMessage()`
- `postAssistantFile()`
- `postCancelIngestion()`

这说明前端并不是直接到处写 `fetch`，而是先通过这个薄封装再发请求。

### 3.4 为什么前端默认不直接连 127.0.0.1:8020

这里有两个关键文件：

- `frontend/next.config.mjs`
- `frontend/app/api/orchestrator/assistant/messages/stream/route.ts`

它们的作用是做代理和转发。

原因是：

- 如果浏览器页面是通过局域网 IP 打开的，前端代码里写死 `127.0.0.1` 容易变成“访问用户自己电脑的本地地址”
- 使用 Next.js 同源代理后，浏览器只访问 `/api/orchestrator/*`
- 再由 Next.js 转发到真正的 FastAPI

这样可以减少 CORS 和本机地址配置问题。

### 3.5 前端页面大致怎么工作

从新手角度，你可以把 `frontend/app/page.tsx` 拆成 5 个理解块：

1. 顶部配置区
   - 输入 `orgId`、`userId`
   - 显示健康状态、LLM 路由状态、ERP 模式

2. 左侧会话区
   - 展示历史聊天
   - 支持新建、切换、重命名、删除

3. 中间聊天区
   - 用户输入自然语言
   - 接收流式回复
   - 显示工具卡片

4. 任务详情区
   - 展示 ingestion 状态机
   - 展示抽取结果、缺失字段、预览数据、ERP 调用日志

5. 底部输入区
   - 发送文本
   - 选择文件或拖拽上传

## 4. 后端在做什么

### 4.1 后端结构

后端最核心的是 `backend/api/`。

这里可以再分成几层：

- `main.py`
  - API 进程入口
- `app/routes.py`
  - 所有 HTTP 路由入口
- `app/assistant_orchestrator.py`
  - 助手消息应该走哪个能力
- `app/chat_orchestrator.py`
  - `pdf_to_erp` 这个聊天工具怎么执行
- `app/store.py`
  - ingestion 数据存取、状态更新、幂等和草稿创建
- `app/workflow.py`
  - 文件处理状态机主流程
- `app/document_extract.py` / `app/structured_extract.py` / `app/llm_extract.py`
  - 文件抽取能力
- `app/erp_client.py`
  - ERP 适配层

### 4.2 API 入口文件

后端主入口是：

- `backend/api/main.py`

这个文件主要做 4 件事：

1. 初始化日志
2. 启动时初始化数据库
3. 配置 CORS
4. 注册路由和请求日志中间件

其中一个很重要的点是：

- 如果没有配置 `DATABASE_URL`，后端会退回“内存存储”
- 这样本地开发更容易启动
- 但重启进程后任务会丢失

### 4.3 路由入口

最核心的 API 文件是：

- `backend/api/app/routes.py`

它几乎就是整个后端能力清单。你可以把它当作“系统目录”来看。

这里提供了几类接口：

1. 系统和健康检查
   - `/`
   - `/health`

2. 上传与 ingestion
   - `/uploads`
   - `/uploads/binary`
   - `/ingestions`
   - `/ingestions/{id}`

3. 助手对话
   - `/assistant/messages`
   - `/assistant/messages/stream`
   - `/assistant/files`
   - `/assistant/sessions/{session_id}`

4. 传统聊天工具接口
   - `/chat/messages`
   - `/chat/files`
   - `/chat/erp-qa`

5. PDF 转 ERP 的关键动作
   - `/ingestions/{id}/resolve`
   - `/ingestions/{id}/confirm-preview`
   - `/ingestions/{id}/create-draft`
   - `/ingestions/{id}/cancel`

6. worker 内部调用接口
   - `/internal/ingestions/{id}/process`

对新手来说，读懂 `routes.py` 的价值非常大，因为它能告诉你：

- 前端能调用什么
- worker 会调什么
- 每个动作对应到哪个后端函数

## 5. “助手”是怎么决定走哪条能力的

最关键的文件是：

- `backend/api/app/assistant_orchestrator.py`

这个文件负责判断用户一句话要怎么处理。可以理解成一个“路由器”。

它大体分三种情况：

1. 普通聊天
   - 走 assistant 回复

2. ERP 查询
   - 比如包含“查供应商”“查物料”“查客户”等关键词
   - 路由到 `erp_qa`

3. PDF 转 ERP 任务
   - 比如跟上传、进度、补字段、确认、草稿有关
   - 路由到 `pdf_to_erp`

这里的设计思想是：

- 先尝试 LLM 路由
- 如果 LLM 不可用，再用规则关键词兜底

所以它并不是“所有话都无脑发给大模型”，而是更偏向“有工具就优先走工具”。

## 6. PDF 转 ERP 是怎么处理的

这是项目最重要的一条业务链路。

### 6.1 前端发起上传

前端调用：

- `postAssistantFile()` 或 `postUploadBinary()`

对应后端接口：

- `/assistant/files`
- `/uploads/binary`

后端会做这些事：

1. 读取上传的文件
2. 计算 SHA-256
3. 把二进制存到对象存储或本地目录
4. 创建一个 ingestion 任务
5. 把任务放进 Redis 队列

### 6.2 ingestion 是什么

你可以把 ingestion 理解成：

“一次文件处理任务的总记录”

它里面会存很多信息，例如：

- 上传文件是谁传的
- 文件哈希
- 当前处理状态
- 已抽取字段
- 缺失字段
- 订单预览
- ERP 草稿号
- 审计事件

整个项目很多逻辑，其实都是围绕 ingestion 在转。

### 6.3 worker 做什么

文件在：

- `backend/worker/worker.py`

worker 本身不直接改数据库，而是做这件事：

1. 从 Redis 队列取出 ingestion id
2. 调用 API 的内部接口 `/internal/ingestions/{id}/process`
3. 由 API 内部继续推进状态机

这样设计的好处是：

- 状态更新入口统一在 API
- 审计、日志、鉴权语义更集中
- worker 更像“调度者”，不是“业务核心”

### 6.4 真正推进任务的是谁

真正的核心处理逻辑在：

- `backend/api/app/workflow.py`

这里定义了 ingestion 的主流程，大致是：

1. `classify`
   - 判断单据类型

2. `parse`
   - 从文件里读文字
   - 可能用 PDF 文字层，也可能走 OCR

3. `extract`
   - 抽取结构化字段
   - 可能结合规则、启发式、LLM 预览

4. `map`
   - 去 ERP 里查供应商、物料、仓库、税码候选项

5. `build_preview`
   - 生成订单预览数据

6. `request_user_input`
   - 如果缺字段，就进入 `NEED_USER_INPUT`
   - 如果字段够了并校验通过，就进入 `VALIDATED`

状态流转大致是：

`UPLOADED -> CLASSIFIED -> PARSED -> EXTRACTED -> MAPPED -> NEED_USER_INPUT / VALIDATED -> DRAFT_CREATED`

### 6.5 store 层在这个流程里起什么作用

最关键的文件是：

- `backend/api/app/store.py`

这个文件负责“把流程结果真正存下来”。

它做的事情包括：

- 创建 ingestion
- 根据 `file_hash` 做幂等
- 追加审计事件
- 读取某个 ingestion
- 提交用户补字段
- 确认订单预览
- 创建 ERP 草稿

你可以把它理解为：

- `workflow.py` 更像“流程引擎”
- `store.py` 更像“状态和数据的总管”

### 6.6 用户补字段后发生什么

当前端调用：

- `/ingestions/{id}/resolve`

后端会在 `store.py` 里：

1. 合并之前已识别的字段和用户新填的字段
2. 调用 ERP 校验逻辑 `validate_draft`
3. 如果还缺字段，继续保持 `NEED_USER_INPUT`
4. 如果齐了，进入 `VALIDATED`

也就是说，用户补字段不是简单保存文本，而是立刻触发一次业务校验。

### 6.7 为什么还有“确认预览”

接口是：

- `/ingestions/{id}/confirm-preview`

这是因为：

- 自动抽取出来的订单信息不一定完全准确
- 前端会展示一个“订单预览编辑器”
- 用户确认或修改后，再由后端重新校验

所以这里其实是一个“人机协同”的步骤，而不是全自动黑盒。

### 6.8 创建 ERP 草稿

接口是：

- `/ingestions/{id}/create-draft`

主要逻辑也在 `store.py`。

它有两个你需要特别记住的点：

1. 只有字段齐全时才允许创建草稿
2. 它做了幂等处理

幂等的意思是：

- 同一个任务反复点击“创建草稿”
- 不应该在 ERP 里生成多个重复草稿

所以代码里会根据组织、文件哈希、单据类型去构造 `idempotency_key`。

## 7. ERP 查询能力是怎么做的

ERP 查询相关入口主要有：

- `backend/api/app/routes.py` 里的 `/chat/erp-qa`
- `backend/api/app/erp_qa.py`
- `backend/api/app/erp_client.py`

大概思路是：

1. 用户问“查物料 M001”“查供应商华为”
2. 后端识别这是 ERP 查询意图
3. 调用 `erp_client` 去查真实 ERP 或 mock ERP
4. 把结果整理成回复文本返回前端

也就是说，这部分更偏“工具查询”，不是重点依赖大模型自由发挥。

## 8. 数据存在哪里

这个项目的数据存储分几类：

### 8.1 ingestion 任务数据

- 有 `DATABASE_URL` 时，存到 Postgres
- 没有时，退回内存

### 8.2 上传的文件

- 优先写 MinIO 或兼容对象存储
- 如果没配对象存储，可退回本地目录

### 8.3 队列任务

- Redis

### 8.4 会话消息

- 前端本地会保存部分会话元信息
- 后端也有 `assistant_session_*` 相关模块处理会话消息

## 9. 前后端是怎么连起来的

如果你想建立完整认知，可以记住下面这条主链路：

### 9.1 聊天消息链路

1. 用户在 `frontend/app/page.tsx` 输入消息
2. 前端调用 `frontend/lib/api.ts` 里的 `streamAssistantMessage()`
3. 请求发到 Next.js 代理接口
4. 再转发给 FastAPI 的 `/assistant/messages/stream`
5. `assistant_orchestrator.py` 判断这是普通聊天、ERP 查询还是 PDF 转 ERP
6. 返回流式文本或工具结果
7. 前端把结果更新到聊天区和任务区

### 9.2 文件上传链路

1. 用户上传文件
2. 前端调用 `/assistant/files` 或 `/uploads/binary`
3. 后端创建 ingestion 并放入队列
4. worker 从 Redis 取任务
5. worker 回调 `/internal/ingestions/{id}/process`
6. `workflow.py` 推进状态机
7. 前端轮询 `/ingestions/{id}`
8. 页面根据状态显示“处理中 / 待补字段 / 可确认 / 草稿已创建”

### 9.3 创建草稿链路

1. 用户补齐字段
2. 用户确认订单预览
3. 前端调用 `/ingestions/{id}/create-draft`
4. 后端通过 `erp_client` 调 ERP 创建草稿
5. 成功后把草稿号和链接写回 ingestion
6. 前端展示草稿结果

## 10. 作为新手，建议先看哪些文件

如果你直接从头到尾看全仓库，会很累。建议按这个顺序：

### 第一轮：先建立地图

1. `README.md`
2. `frontend/app/page.tsx`
3. `frontend/lib/api.ts`
4. `backend/api/main.py`
5. `backend/api/app/routes.py`

这一轮的目标不是看懂所有逻辑，而是先知道：

- 页面从哪进
- 请求往哪发
- 后端有哪些接口

### 第二轮：看核心业务链

1. `backend/api/app/assistant_orchestrator.py`
2. `backend/api/app/chat_orchestrator.py`
3. `backend/api/app/store.py`
4. `backend/api/app/workflow.py`
5. `backend/worker/worker.py`

这一轮的目标是搞清楚：

- 助手怎么路由
- 文件任务怎么推进
- 状态为什么会变化

### 第三轮：按能力深挖

如果你对某个方向更感兴趣，再继续看：

- 文件解析：`document_extract.py`、`structured_extract.py`、`llm_extract.py`
- ERP 对接：`erp_client.py`
- 订单预览：`order_preview.py`
- 会话管理：`assistant_session_store.py`、`assistant_session_db.py`
- 数据结构：`schemas.py`

## 11. 新手最容易混淆的几个概念

### 11.1 `assistant` 和 `chat` 的区别

这个仓库同时保留了：

- `/assistant/*`
- `/chat/*`

从现在的代码看，`assistant` 更像统一入口，偏“真正给用户使用的助手通道”；
`chat` 更像较早或较底层的工具化接口，尤其是 `pdf_to_erp` 的对话操作。

你先重点看 `assistant` 这条线就够了。

### 11.2 为什么前端页面文件这么大

`frontend/app/page.tsx` 把很多功能都堆在了一个页面里，所以会显得很长。

这不代表它不可读，而是你需要换一种读法：

- 不要逐行细看
- 先按“聊天、上传、轮询、补全、预览、草稿”这几个功能块来分段理解

### 11.3 为什么 worker 不直接处理数据库

因为作者想把“状态推进和业务入口”收敛在 API 内部，这样：

- 逻辑更集中
- 审计更统一
- 将来替换 worker 或编排方式时更容易

### 11.4 为什么有时会看到“内存存储”

这是开发环境兜底方案。

好处是：

- 不连数据库也能跑

缺点是：

- 进程一重启，任务记录就没了

所以如果你本地调试时发现任务突然找不到，先确认是不是没有配置 `DATABASE_URL`。

## 12. 如果你只想快速记住这个项目

可以把它浓缩成下面这句话：

前端是一个聊天式 ERP 工作台，后端是一个带任务状态机的 FastAPI 服务，worker 负责异步推进文件解析流程，`store.py` 负责保存和推进任务状态，`workflow.py` 负责真正的文件处理步骤，`erp_client.py` 负责和 ERP 系统对接。

## 13. 下一步你可以怎么继续看

如果你准备继续自己读源码，建议这样做：

1. 先打开 `frontend/app/page.tsx`，只找“发送消息”“上传文件”“轮询状态”三个动作
2. 再打开 `backend/api/app/routes.py`，把这些动作对应到后端接口
3. 再打开 `assistant_orchestrator.py` 和 `workflow.py`，理解消息路由和文件任务主流程
4. 最后再看 `store.py`，理解状态如何真正落盘

如果你愿意，我下一步还可以继续帮你做两种更细的整理：

- 按“前端页面模块”给你画一份阅读路线
- 按“后端一次上传到生成草稿”的时序给你画一份文字版流程图

## 14. 一次“上传文件到生成草稿”的文字版时序图

这一节是给你建立“动态理解”的。

前面那些章节更像静态地图，这里则回答一个问题：

当用户真的上传了一个 PDF，并最终点了“上传 ERP / 生成草稿”，代码到底按什么顺序在跑？

建议你一边看这一节，一边对照这些文件：

- `frontend/app/page.tsx`
- `frontend/lib/api.ts`
- `backend/api/app/routes.py`
- `backend/api/app/store.py`
- `backend/api/app/workflow.py`
- `backend/worker/worker.py`

### 14.1 阶段 0：页面刚打开时发生什么

用户打开页面后，前端不会立刻处理文件，而是先做一些准备动作：

1. 调用 `getHealth()`
2. 读取本地保存的聊天会话
3. 如果有旧会话，尝试恢复历史消息和当前任务

这部分代码主要在：

- `frontend/app/page.tsx`
- `frontend/lib/api.ts`

这一步的目的是让页面知道：

- 后端活着没有
- LLM 路由是否启用
- ERP 当前是 `mock` 还是真实接口
- 当前有没有尚未完成的 ingestion 任务

### 14.2 阶段 1：用户上传文件

假设用户把一个 PDF 拖到聊天框里。

前端大致会这样走：

1. `page.tsx` 里的 `handleFiles()` 接到浏览器里的 `File`
2. 做一次前端预检查，比如扩展名、大小
3. 调用 `postAssistantFile()`
4. `postAssistantFile()` 用 `FormData` 发到 `/assistant/files`

这一层你可以把它想成：

“前端把真实文件交给后端，并告诉后端这是哪个组织、哪个用户上传的。”

### 14.3 阶段 2：API 接住文件并创建任务

后端接口是：

- `/assistant/files`

对应代码在：

- `backend/api/app/routes.py`

这个接口内部会调用一个公共函数 `_create_ingestion_from_upload_file()`，它会做这些事：

1. 读取上传文件的二进制内容
2. 检查文件大小是否超限
3. 计算文件哈希 `sha256`
4. 调用 `save_binary_file()` 保存文件
5. 组装 `UploadRequest`
6. 调用 `create_upload()` / `create_ingestion()` 创建 ingestion 记录
7. 调用 `enqueue_ingestion_job()` 把任务扔进 Redis 队列

这里最关键的产物有两个：

1. 文件本体被存起来了
2. ingestion 任务记录被创建出来了

这时前端会立刻拿到一个响应，其中包含：

- `ingestion_id`
- 当前状态
- 一个初始的工具卡片信息

所以用户不会傻等整个解析过程结束，而是会先看到“我已经收到文件，正在处理”的提示。

### 14.4 阶段 3：前端开始轮询任务状态

拿到 `ingestion_id` 后，前端会进入轮询模式。

在 `frontend/app/page.tsx` 里，你能看到它定时调用：

- `getIngestion(ingestionId)`

请求的接口是：

- `/ingestions/{id}`

轮询的作用是让前端持续看到任务状态变化，比如：

- `UPLOADED`
- `CLASSIFIED`
- `PARSED`
- `EXTRACTED`
- `MAPPED`
- `NEED_USER_INPUT`
- `VALIDATED`
- `DRAFT_CREATED`

你可以把前端轮询理解成：

“前端不停问后端：你处理到哪一步了？”

### 14.5 阶段 4：worker 从 Redis 里取任务

与此同时，后端的 worker 进程在另一边工作。

核心文件：

- `backend/worker/worker.py`

worker 的主循环会：

1. 用 `BLPOP` 阻塞读取 Redis 队列
2. 拿到队列里的 `ingestion_id`
3. 调用 API 内部接口 `/internal/ingestions/{id}/process`

这里要特别注意：

worker 并不直接“打开文件然后开始解析”，而是把处理动作重新交还给 API。

这样做的好处是：

- 所有状态更新逻辑集中在 API
- worker 更轻
- 数据一致性和审计更容易维护

### 14.6 阶段 5：API 进入文件处理工作流

worker 调到的内部接口是：

- `/internal/ingestions/{id}/process`

这个接口最后会调用：

- `store.py` 里的 `process_ingestion()`

而 `process_ingestion()` 又会调用：

- `workflow.py` 里的 `run_ingestion_processing_workflow()`

这一层是整个文件处理主线的核心。

### 14.7 阶段 6：工作流先做分类 `classify`

首先执行的是 `classify` 节点。

它大概会做：

1. 根据文件名猜测单据类型
2. 必要时结合配置强制某种单据类型
3. 把状态推进到 `CLASSIFIED`
4. 记录一条审计事件

这一步的目标不是“抽字段”，而是先回答：

“这份文件大概属于哪种业务单据？”

### 14.8 阶段 7：读取文本 `parse`

然后进入 `parse` 节点。

它大概会做：

1. 从对象存储里把文件字节取回来
2. 判断是 PDF、图片还是别的格式
3. 如果是 PDF，优先取文字层
4. 如果文字层太差，可能走 OCR
5. 把抽到的原始文本塞进内存状态
6. 记录字符数、预览文本、解析方式
7. 把状态推进到 `PARSED`

这一步产出的核心不是结构化字段，而是“原始文本”。

可以把它想成：

“先把机器能看懂的文字抠出来。”

### 14.9 阶段 8：抽结构化字段 `extract`

接下来是 `extract` 节点。

它会基于文本做进一步提取，例如：

- 供应商编码
- 单据日期
- 币别
- 物料编码
- 数量

这里会综合使用：

- 启发式规则
- 特定版式抽取
- 配置化 extraction profile
- 必要时 LLM 预览补强

这一步结束后，ingestion 里会逐步长出：

- `resolved_fields`
- `missing_fields`
- 相关抽取信息

并把状态推进到：

- `EXTRACTED`

### 14.10 阶段 9：去 ERP 查候选项 `map`

然后进入 `map` 节点。

这一步不是创建草稿，而是“对照 ERP 主数据做候选查找”，例如：

- 供应商候选
- 物料候选
- 仓库候选
- 税码候选

为什么要做这一步？

因为文档里写的名称，不一定是 ERP 里真正用的编码。

所以这里更像：

“把文档世界的词，试着映射到 ERP 世界的编码。”

这一步会把状态推进到：

- `MAPPED`

### 14.11 阶段 10：构建订单预览 `build_preview`

映射完成后，工作流会尝试构建一个更接近业务界面的订单预览。

这个预览会包含：

- 订单头信息
- 明细行
- 可编辑字段
- 问题列表

这一步的意义是：

- 前端不必直接展示一堆零散字段
- 可以把它们整合成“用户能看懂、能改”的订单草稿视图

### 14.12 阶段 11：决定是等用户补充还是自动校验通过

工作流最后一个关键节点是：

- `request_user_input`

这里会分成两种情况。

第一种，字段还缺：

1. 后端发现 `missing_fields` 还不为空
2. 状态推进到 `NEED_USER_INPUT`
3. 前端下次轮询时，就会展示补全表单或红色提示卡片

第二种，字段已经差不多齐了：

1. 后端调用 ERP 的 `validate_draft`
2. 如果 ERP 校验通过，状态推进到 `VALIDATED`
3. 前端就会显示“可以确认预览 / 可以上传 ERP”

所以 `VALIDATED` 不是单纯“前端填完了”，而是：

- 后端已经认为数据够了
- 并且校验逻辑已经通过了

### 14.13 阶段 12：前端看到 `NEED_USER_INPUT` 后会做什么

如果轮询结果变成 `NEED_USER_INPUT`，前端会：

1. 展示缺失字段表单
2. 展示候选项
3. 如果有预览数据，也可能展示订单预览编辑器

用户填写后，前端会发：

- `/assistant/messages`，动作为 `submit_missing_fields`

或者对应到更底层的逻辑，就是继续推动 `pdf_to_erp` 这个工具。

你可以把这一步理解成：

“用户现在在接管自动流程，把机器缺的最后那点信息补上。”

### 14.14 阶段 13：后端合并用户字段并再次校验

用户提交补全后，后端最终会走到：

- `store.py` 里的 `resolve_ingestion()`

它做的事不是简单保存，而是：

1. 合并旧字段和新字段
2. 刷新预览
3. 调 ERP 的 `validate_draft`
4. 决定结果是继续 `NEED_USER_INPUT`，还是进入 `VALIDATED`

也就是说：

- 这一步是“保存 + 校验”
- 不是“只保存，稍后再说”

### 14.15 阶段 14：用户确认订单预览

如果前端已经拿到预览数据，用户还可以手动修改预览里的订单内容。

确认时调用的是：

- `/ingestions/{id}/confirm-preview`

后端在 `confirm_preview_for_ingestion()` 中会做：

1. 把预览里的字段回写到 `resolved_fields`
2. 重新检查预览必填项
3. 再次调用 `validate_draft`
4. 如果通过，保持或推进到 `VALIDATED`
5. 如果不通过，退回 `NEED_USER_INPUT` 或失败

所以“确认预览”本质上是在做最终的人机对账。

### 14.16 阶段 15：用户点击“上传 ERP / 生成草稿”

当前端看到状态是 `VALIDATED`，就允许用户点生成草稿。

前端会调用：

- `/assistant/messages`，动作 `create_draft`

或者更底层最终对应到：

- `/ingestions/{id}/create-draft`

后端在 `store.py` 里的 `create_draft_for_ingestion()` 中会：

1. 检查是否还有缺失字段
2. 计算幂等键 `idempotency_key`
3. 如果之前已经建过草稿，直接复用旧结果
4. 否则调用 `erp_client.create_draft()`
5. 成功后把 `draft_no`、`draft_url` 写回 ingestion
6. 把状态推进到 `DRAFT_CREATED`

这里的业务关键点是幂等。

因为用户可能会重复点击按钮，而系统不能在 ERP 里生成多张重复草稿单。

### 14.17 阶段 16：前端显示最终结果

当前端轮询到或直接收到：

- `DRAFT_CREATED`

就会展示最终结果卡片，例如：

- 草稿号
- 草稿链接
- 已完成的状态标签

到这里，一次“上传 PDF 到 ERP 草稿”的主流程才算完整结束。

## 15. 你可以用这条最短路径追代码

如果你现在就想自己顺着源码跑一遍，我建议你按下面这个顺序点开：

1. `frontend/app/page.tsx` 里的 `handleFiles()`
2. `frontend/lib/api.ts` 里的 `postAssistantFile()`
3. `backend/api/app/routes.py` 里的 `assistant_files_route()`
4. `backend/api/app/routes.py` 里的 `_create_ingestion_from_upload_file()`
5. `backend/api/app/store.py` 里的 `create_ingestion()` 和 `process_ingestion()`
6. `backend/worker/worker.py` 里的 `process_job()`
7. `backend/api/app/workflow.py` 里的 `run_ingestion_processing_workflow()`
8. `backend/api/app/store.py` 里的 `resolve_ingestion()`、`confirm_preview_for_ingestion()`、`create_draft_for_ingestion()`

如果你能把这 8 个点顺下来，基本就已经抓住这个项目最重要的一条业务主线了。

## 16. `frontend/app/page.tsx` 怎么读才不乱

这个文件很长，但你不要把它当成“一个大组件”，而要把它拆成几个层次来看。

最实用的阅读方法是：

1. 先看顶部的小工具函数
2. 再看 `HomePage()` 里的状态和 `useEffect`
3. 再看几个最关键的动作函数
4. 最后再看 JSX 渲染区

也就是说：

- 先看“数据和动作”
- 后看“页面长什么样”

这样比从上到下硬啃 JSX 要轻松很多。

### 16.1 第一层：文件开头的大量辅助函数，不是页面本体

在 `HomePage()` 之前，有很多普通函数，比如：

- 会话相关：`createAssistantSessionId()`、`createSessionMeta()`、`parseChatSessionMetas()`
- 状态相关：`pdfToErpProgressPercent()`、`statusIndex()`、`displayIngestionStatus()`
- UI 组装相关：`buildPdfToErpProgressUi()`、`buildPdfToErpToolUi()`
- 文案相关：`ingestionStatusLabelZh()`、`pdfToErpWorkflowCardText()`
- 数据辅助相关：`mergeDraftCreatedState()`、`applyClientDraftState()`

你可以把这些函数统一理解成：

“为了让页面逻辑更清楚，提前抽出来的小工具。”

新手第一次看时，不需要逐个背下来，只要先知道它们属于哪一类。

### 16.2 第二层：真正的页面入口是 `HomePage()`

真正的页面主体从这里开始：

- `export default function HomePage()`

你一进入这个函数，会先看到大量 `useState`、`useRef`、`useMemo`、`useCallback`。

这很正常，因为这个页面本身承担了很多角色：

- 聊天页
- 上传页
- 任务监控页
- 补全表单页
- 订单预览页
- 草稿结果页

所以状态会特别多。

不要被 `useState` 数量吓到，可以先按用途分组。

### 16.3 第三层：先只看状态分组，不看细节

`HomePage()` 里的状态大概能分成这些组。

第一组，身份和环境：

- `orgId`
- `userId`
- `healthInfo`
- `extractionProfileId`

这一组控制的是：

- 当前组织是谁
- 当前用户是谁
- 后端健康状态怎么样
- 解析规则编号是什么

第二组，聊天会话：

- `assistantSessionId`
- `chatMessages`
- `chatSessions`
- `renamingSessionId`
- `renameDraft`

这一组控制的是：

- 当前聊天是哪一个
- 聊天消息列表
- 左侧历史会话列表

第三组，任务本体：

- `ingestion`
- `ingestionId`
- `ingestionHistory`

这一组控制的是：

- 当前文件任务详情
- 当前任务编号
- 本会话里上传过的任务历史

第四组，上传和动作中的状态：

- `isDragging`
- `isUploading`
- `isResolving`
- `isConfirmingPreview`
- `isCreatingDraft`
- `isChatSending`

这一组控制的是：

- 现在是不是在拖拽上传
- 是不是正在提交请求
- 某些按钮是不是要禁用

第五组，补全和预览：

- `resolveFields`
- `previewDraft`

这一组控制的是：

- 用户补了哪些字段
- 当前正在编辑的订单预览数据

如果你先把这 5 组记住，后面读动作函数就容易很多。

### 16.4 第四层：这个页面最关键的不是 JSX，而是几个动作函数

这个文件里真正值得你重点盯住的，是几个 `useCallback` 动作函数。

最关键的有这些：

- `activateAssistantSession()`
- `appendToolResponse()`
- `onSendChat()`
- `handleFiles()`
- `onResolve()`
- `onConfirmPreview()`
- `onCreateDraft()`

你几乎可以把它们理解成这个页面的“控制器层”。

下面我按作用分别解释。

### 16.5 `activateAssistantSession()`：切换会话时发生什么

这个函数的作用是：

1. 切换当前会话 id
2. 从本地和后端恢复该会话消息
3. 尝试恢复这个会话关联的 ingestion 任务

这就是为什么你切换历史聊天时，不只是左侧高亮变化，而是右侧聊天和任务也会跟着恢复。

所以它解决的是：

“从一个会话切到另一个会话，页面如何恢复上下文？”

### 16.6 `appendToolResponse()`：把后端结果统一落到前端状态里

这是页面中非常关键的一个函数。

它的作用是把后端返回的结果统一写入前端状态，包括：

- 更新聊天消息
- 更新 `session_id`
- 更新当前 ingestion
- 更新工具卡片
- 更新草稿结果

为什么它重要？

因为后端返回的有时是普通文本，有时是工具卡片，有时带 ingestion，有时带 draft。

如果每个地方都自己处理一次，页面会很乱。

所以这里相当于做了一个统一收口：

“不管后端回什么，先交给这个函数做归并。”

### 16.7 `onSendChat()`：用户发送一条消息时发生什么

这是聊天主入口之一。

它大概做这些事：

1. 把用户输入先追加到聊天区
2. 创建一个“助手正在思考”的占位消息
3. 调用 `streamAssistantMessage()`
4. 边收流式 `delta` 边更新这条助手消息
5. 收到 `final` 事件后，再把工具结果交给 `appendToolResponse()`

所以这里你能看到这个项目的一个特点：

- 普通回答走流式文本
- 工具结果在最后统一落到页面状态

### 16.8 `handleFiles()`：文件上传主入口

这是前端文件处理最重要的入口。

它会：

1. 接收浏览器传来的 `FileList`
2. 做前端校验
3. 归档上一个 ingestion 到历史列表
4. 调用 `postAssistantFile()`
5. 把返回结果交给 `appendToolResponse()`
6. 设置新的 `ingestionId`
7. 触发后续轮询

如果你只想抓前端与后端交互的主线，这个函数一定要看懂。

因为从“用户拖入文件”开始，到“页面知道当前正在处理哪个任务”，基本都从这里起。

### 16.9 `onResolve()`：补全字段时发生什么

这个函数负责提交用户手填的字段。

它大概会：

1. 从 `resolveFields` 里收集要提交的数据
2. 调用 `/assistant/messages`，动作是 `submit_missing_fields`
3. 把结果交给 `appendToolResponse()`
4. 更新当前 ingestion
5. 在聊天区补一条系统提示

所以它不是直接请求 `/resolve` 的原始接口，而是沿着助手统一通道继续推动工具任务。

你可以把它理解成：

“用户在页面表单里填字段，但页面还是把这个动作包装成一次助手任务操作。”

### 16.10 `onConfirmPreview()`：确认订单预览时发生什么

这个函数负责把编辑后的订单预览提交给后端。

它大概会：

1. 取当前 `previewDraft`
2. 调用 `/assistant/messages`，动作是 `confirm_preview`
3. 更新 ingestion
4. 更新工作流卡片

这个动作的意义不是“仅保存界面表格”，而是：

- 把预览数据回写到后端任务里
- 让后端重新校验

### 16.11 `onCreateDraft()`：生成草稿时发生什么

这是前端最后一步关键动作。

它会先做前端保护：

1. 如果还有未确认预览修改，不让继续
2. 如果当前状态不是 `VALIDATED`，不让继续
3. 再调用 `/assistant/messages`，动作 `create_draft`

成功后它会：

- 更新草稿号
- 更新 ingestion 状态
- 停止轮询

所以这里的逻辑体现出一个思路：

- 前端先做一层用户体验检查
- 真正的业务校验仍以后端为准

### 16.12 第五层：`useEffect` 是这个页面的“自动反应系统”

这个页面里有很多 `useEffect`，第一次看很容易晕。

建议你不要把它们看成杂乱副作用，而要理解成“自动反应规则”。

常见几类如下。

第一类，初始化：

- 页面打开时获取健康状态
- 从本地存储恢复会话
- 从服务端恢复会话消息

第二类，同步：

- ingestion 改了，就同步补全表单
- ingestion 改了，就同步预览数据
- 会话改了，就写回本地存储

第三类，轮询：

- 只要有 `ingestionId`
- 就开始定时 `getIngestion()`
- 到终态后停止

第四类，滚动和交互细节：

- 新消息出现时自动滚到底部
- 用户手动滚动后暂停自动跟随

所以更简单的理解是：

`useEffect` 在这里主要不是做业务，而是在做“自动同步和自动监听”。

### 16.13 第六层：页面渲染区其实也能按区域分开看

最后才是 JSX 渲染区。

这个区域你也不要一整坨看，可以拆成 6 块。

第一块，顶部栏：

- 标题
- 组织 / 用户输入框
- 高级选项
- 日志按钮

第二块，左侧历史会话栏：

- 新建聊天
- 历史聊天列表
- 重命名 / 删除 / 置顶

第三块，中间聊天区：

- 展示聊天消息
- 展示工具卡片
- 空态提示

第四块，任务详情区：

- 当前任务编号
- 状态流水线
- 解析结果 JSON 接口
- 文件哈希
- 审计事件
- ERP 调用日志

第五块，补全 / 预览 / 草稿操作区：

- 缺失字段表单
- 候选项列表
- 订单预览编辑器
- 生成草稿按钮

第六块，底部输入区：

- 文件上传按钮
- 输入框
- 发送按钮

所以从页面结构上看，它并不是只有“聊天框”，而是：

“聊天 + 任务面板 + 操作面板”的混合工作台。

### 16.14 新手第一次精读 `page.tsx` 的推荐顺序

如果你现在马上要读源码，建议按下面顺序。

第一轮，只看入口和主动作：

1. `HomePage()`
2. `handleFiles()`
3. `onSendChat()`
4. `onResolve()`
5. `onConfirmPreview()`
6. `onCreateDraft()`

第二轮，再看状态怎么自动变化：

1. 恢复会话相关的 `useEffect`
2. 轮询 ingestion 的 `useEffect`
3. 同步表单 / 预览的 `useEffect`

第三轮，最后才看渲染：

1. 顶部栏
2. 左侧会话栏
3. 聊天消息区
4. 任务详情区
5. 底部输入区

这三轮看下来，你会比直接逐行阅读快很多。

### 16.15 读这个文件时最容易掉进去的坑

第一个坑：被 JSX 体积吓住。

解决方法：

- 先找动作函数，不要先啃渲染

第二个坑：把所有 `useState` 当成平级信息。

解决方法：

- 按“会话 / 任务 / 上传 / 预览 / 动作状态”分组记忆

第三个坑：搞不清“聊天消息”和“任务状态”的关系。

解决方法：

- 聊天消息是用户看见的对话记录
- ingestion 是文件任务的真实业务状态
- 工具卡片是把 ingestion 状态投影到聊天区的 UI 表现

第四个坑：以为前端决定了业务状态。

其实不是。

真正的业务状态还是以后端 ingestion 为准，前端主要负责：

- 展示
- 发动作
- 轮询同步

## 17. 你接下来最适合怎么继续

到这一步，如果你想继续深入，我推荐两种路线。

第一种，前端路线：

1. 先按本节把 `page.tsx` 读懂
2. 再回到 `frontend/lib/api.ts`
3. 最后去对照 `routes.py`

第二种，后端路线：

1. 从 `routes.py` 看接口入口
2. 接着看 `assistant_orchestrator.py`
3. 再看 `store.py`
4. 最后看 `workflow.py`

如果你愿意，我下一步可以直接带你做：

- `page.tsx` 逐段精读版

我会按“这一段是干什么的、输入是什么、输出是什么、和后端哪条接口相连”这种方式继续讲。 




最可能慢的 5 个位置

- PDF/图片解析与 OCR
  
  - 位置： document_extract.py 和 workflow.py
  - 具体入口： _node_parse() 会调用 extract_text_from_bytes(raw, name)
  - 为什么慢：这里可能要读对象存储、解析 PDF 文字层、必要时再走 OCR；如果是扫描件、多页 PDF、图片质量差，耗时会明显上升
  - 你能看到的迹象：任务会在 UPLOADED 或 PARSED 前后停比较久，日志里会出现 pypdf 、 pymupdf 、OCR 相关信息
  - 典型耗时来源：
    - pypdf / pymupdf 提取文字层
    - 扫描 PDF 转图片
    - Tesseract / Paddle / 阿里云 OCR
- 抽取结构化字段，尤其是带 LLM 补强时
  
  - 位置： workflow.py:L257-L312
  - 具体入口： _node_extract()
  - 为什么慢：这里会做规则抽取、版式抽取、字段别名处理，还可能调用 try_apply_llm_preview(...)
  - 如果走到 LLM，这一步就不再是纯本地计算，而会受模型响应时间影响
  - 你能看到的迹象：任务停在 EXTRACTED 前比较久，或者同一个文件有时快有时慢
- ERP 主数据映射查询
  
  - 位置： workflow.py:L378-L434
  - 具体入口： _node_map()
  - 为什么慢：这一步会查多个 ERP 接口：
    - search_vendors
    - search_materials
    - search_warehouses
    - search_tax_codes
  - 这些通常都是真实 HTTP 调用，网络慢、ERP 慢、接口超时都会直接拖长处理时间
  - 更关键的是：现在这几次查询看起来是顺序做的，不是并发做的，所以总耗时可能累加
  - 对应后端接口定义在： erp_client.py
- 草稿校验与创建草稿
  
  - 位置：
    - 校验： resolve_ingestion() 、 confirm_preview_for_ingestion() 在 store.py
    - 创建： create_draft_for_ingestion() 在 store.py
    - ERP 适配接口在 erp_client.py
  - 为什么慢：这里会调用 ERP 的 validate_draft() 和 create_draft()
  - 如果是 mock 模式通常很快；如果是真实 ERP，这一步可能是明显慢点
  - 你能看到的迹象：
    - 点“确认预览”后等得久
    - 点“上传 ERP/生成草稿”后等得久
    - 有时还会受 ERP 权限、网络、上游超时影响
- LLM 路由和流式回答
  
  - 位置： assistant_llm_router.py
  - 具体入口：
    - decide_with_llm() assistant_llm_router.py:L238-L278
    - answer_assistant_with_llm() assistant_llm_router.py:L302-L311
    - stream_assistant_answer_with_llm() assistant_llm_router.py:L314-L321
  - 为什么慢：只要走到 LLM，不管是“决定该调用哪个工具”，还是“普通聊天生成答案”，都要等模型返回
  - 这部分常见问题不是 CPU 慢，而是外部模型接口延迟高、超时、网络波动
  - 不过它通常影响的是“聊天响应速度”，不一定影响后台 ingestion 整体处理速度
额外一个容易忽略的点

- 重试机制 会让“失败场景更慢”
  - 位置： workflow.py:L315-L376
  - 为什么： parse 、 extract 、 map 节点都有重试和 backoff
  - 这不是坏事，目的是提高稳定性
  - 但如果某个外部依赖不稳定，就会出现“不是直接失败，而是慢很久后才失败”
反过来说，不太会是瓶颈的地方

- tools/registry.py
- tools/pdf_to_erp.py
- tools/erp_qa.py
这些层更多是“封装和分发”，本身开销非常小。真正慢的，还是它们后面调用的 OCR、LLM、ERP、对象存储和数据库逻辑。

如果按“最可能拖慢用户体验”的排序

- 第 1 名：OCR / PDF 解析
- 第 2 名：ERP 主数据映射查询
- 第 3 名：ERP 校验与创建草稿
- 第 4 名：LLM 路由和大模型回答
- 第 5 名：失败后的重试等待
你接下来最值得看的代码

- 解析慢：看 document_extract.py
- 工作流慢：看 workflow.py
- ERP 慢：看 erp_client.py
- 草稿慢：看 store.py
如果你愿意，我下一步可以继续帮你做一版“性能视角的代码导读”，直接告诉你：

- 哪些地方是本地 CPU 耗时
- 哪些地方是网络 I/O 耗时
- 哪些地方是可以优化的点