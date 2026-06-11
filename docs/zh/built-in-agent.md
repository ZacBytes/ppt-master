# 内置 OpenRouter Agent

PPT Master 现在可以不依赖 Claude Code、Codex、Cursor 等外部编程 Agent，直接通过
OpenRouter 驱动本地工作流。模型调用走 OpenRouter，文件处理、SVG、预览和 PPTX
导出都在本机执行。

## 1. 配置

在仓库根目录创建不会提交到 Git 的 `.env`：

```dotenv
OPENROUTER_API_KEY=your-new-openrouter-key
PPT_MASTER_AGENT_MODEL=google/gemma-4-31b-it
PPT_MASTER_IMAGE_GENERATION=disabled
```

已经粘贴到聊天、Issue 或提交记录中的 Key 必须先撤销，再创建新 Key。

图片生成模式：

| 模式 | 行为 |
|---|---|
| `disabled` | 默认；只用可编辑 SVG 图形、图表、图标、文字和本地素材 |
| `prompts-only` | 只生成图片提示词，不调用生图 API |
| `enabled` | 允许调用 `image_gen.py`，生图后端需要单独配置 |

## 2. 启动

浏览器 Studio：

```bash
python skills/ppt-master/scripts/ppt_agent_web.py
```

默认打开 `http://127.0.0.1:5080`，支持本地文件上传、项目切换、Agent 对话、
八项确认、工作流进度、幻灯片预览、实时编辑器和导出下载。

终端界面：

```bash
python skills/ppt-master/scripts/ppt_agent.py
```

继续已有项目：

```bash
python skills/ppt-master/scripts/ppt_agent.py \
  --project projects/<project_name>
```

常用命令：

| 命令 | 作用 |
|---|---|
| `/status` | 查看项目、阶段、模型、图片模式和 Token 用量 |
| `/open <project>` | 打开已有项目和保存的 Agent 状态 |
| `/new <request>` | 开始新的演示文稿需求 |
| `/resume` | 从当前工作流阶段继续 |
| `/preview` | 启动本地实时预览 |
| `/export` | 继续执行质量检查和导出 |
| `/clear` | 清空对话历史，但保留工作流状态 |
| `/help` | 查看本地运行说明 |
| `/quit` | 退出 |

## 3. 无联网检索

内置 Agent 不提供网页搜索、网页抓取、URL 下载或网络图片搜索。

请提供以下任意一种输入：

- 本地源文件
- 直接粘贴的文字材料
- 已经包含事实和要求的完整 Brief

单独提供 URL 不会触发抓取，需要先下载内容或把正文粘贴到对话中。

## 4. 工作流约束

运行时会强制执行：

- 串行阶段切换
- 八项确认必须由用户明确批准
- SVG 必须逐页、连续编号生成
- 每一页 SVG 写入前重新读取 `spec_lock.md`
- 禁止脚本批量生成 SVG
- 后处理命令固定顺序执行
- 根据图片模式决定是否允许调用生图
- 文件路径限制在仓库和当前项目内
- 会话记录自动脱敏

项目会话保存在：

```text
projects/<project>/.agent/
```

选择项目之前的临时会话保存在 `.ppt-master-agent/`。两者都不会提交到 Git。
